# -*- coding: utf-8 -*-
"""Registration, login and identity endpoints.

Sign-up creates the tenant on first use, which is what makes the deployment
self-service: a new merchant registers, gets its own ``tenant_id``, and every
subsequent query is scoped to it automatically.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import metrics
from ..config import get_settings
from ..core import auth as auth_mod
from ..core import tenant as tenant_mod
from ..core.observability import log_event
from ..db import get_db
from ..models import User
from ..schemas import LoginRequest, PrincipalOut, RegisterRequest, TokenResponse
from ..security import Principal, principal_dependency

logger = logging.getLogger('gateway.auth')
router = APIRouter(prefix='/api/auth', tags=['auth'])


def _issue(user: User, settings) -> TokenResponse:
    token = auth_mod.issue_token(
        subject=user.id,
        secret=settings.jwt_secret,
        issuer=settings.jwt_issuer,
        ttl_minutes=settings.access_token_ttl_minutes,
        tenant_id=user.tenant_id,
        store_id=user.store_id,
        roles=[user.role],
        extra={'email': user.email},
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
        tenant_id=user.tenant_id,
        user_id=user.id,
        roles=[user.role],
    )


@router.post('/register', response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    tenant_mod.ensure_tenant(
        db, body.tenant_id, name=body.tenant_name or body.tenant_id,
        daily_quota=settings.tenant_daily_message_quota,
        rate_limit=settings.rate_limit_per_minute,
    )

    existing = (
        db.execute(
            select(User).where(User.tenant_id == body.tenant_id, User.email == body.email.lower())
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail='email already registered for this tenant')

    # The first user of a tenant becomes its admin; everyone else is a customer.
    user_count = len(
        db.execute(select(User.id).where(User.tenant_id == body.tenant_id)).all()
    )
    role = 'admin' if user_count == 0 else 'customer'

    # Deterministic id: ``hash()`` is salted per process and would produce a
    # different id on every worker restart.
    suffix = hashlib.sha256(f'{body.tenant_id}:{body.email.lower()}'.encode()).hexdigest()[:10]
    user = User(
        id=f'U-{suffix}',
        tenant_id=body.tenant_id,
        store_id=body.store_id,
        email=body.email.lower(),
        password_hash=auth_mod.hash_password(body.password),
        display_name=body.display_name or body.email.split('@')[0],
        role=role,
        status='active',
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    log_event(logger, 'user_registered', user_id=user.id, tenant_id=user.tenant_id, role=role)
    return _issue(user, settings)


@router.post('/login', response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    user = (
        db.execute(
            select(User).where(User.tenant_id == body.tenant_id, User.email == body.email.lower())
        )
        .scalars()
        .first()
    )
    if user is None or not auth_mod.verify_password(body.password, user.password_hash):
        metrics.AUTH_FAILURES.labels('bad_credentials').inc()
        # Same message for "no such user" and "wrong password" — do not help
        # an attacker enumerate accounts.
        raise HTTPException(status_code=401, detail='invalid credentials')
    if user.status != 'active':
        metrics.AUTH_FAILURES.labels('inactive_user').inc()
        raise HTTPException(status_code=403, detail=f'user is {user.status}')

    log_event(logger, 'user_login', user_id=user.id, tenant_id=user.tenant_id)
    return _issue(user, settings)


@router.get('/me', response_model=PrincipalOut)
def me(principal: Principal = Depends(principal_dependency)):
    return PrincipalOut(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        store_id=principal.store_id,
        email=principal.email,
        roles=sorted(principal.roles),
        auth_mode=principal.auth_mode,
    )
