# -*- coding: utf-8 -*-
"""Authentication and authorization.

Three modes, all resolving to the same :class:`Principal`:

* ``jwt`` — production. Bearer token issued by ``/api/auth/login``; carries
  ``tenant_id``, ``store_id`` and ``roles``. The tenant in the token is the
  only tenant the request can touch — it is never taken from a query string.
* ``dev`` — local demo / offline runs. Identity comes from ``X-Demo-*``
  headers with safe defaults, so the UI works without a login screen.
* ``none`` — anonymous, for tests only.

``require_role`` is a dependency factory, so a route declares its own
authorization instead of checking roles inside the handler body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from . import metrics
from .config import get_settings
from .core import auth as auth_mod
from .core import tenant as tenant_mod
from .db import get_db
from .models import User

logger = logging.getLogger('gateway.security')


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str = ''
    store_id: str = ''
    roles: frozenset[str] = field(default_factory=frozenset)
    email: str = ''
    auth_mode: str = 'dev'
    #: Id used when calling the upstream order/business service. Resolved from
    #: ``users.business_customer_id``; empty means the two id spaces coincide.
    business_customer_id: str = ''

    @property
    def business_ref(self) -> str:
        """The customer id the business service knows this principal by.

        Kept separate from :attr:`user_id` because the platform and the order
        system are distinct systems: conflating their ids happens to work only
        while both are seeded from the same string, and fails the moment they
        are not.
        """
        return self.business_customer_id or self.user_id

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


def _reject(reason: str, status: int = 401, detail: str = '') -> HTTPException:
    metrics.AUTH_FAILURES.labels(reason).inc()
    return HTTPException(status_code=status, detail=detail or reason)


def principal_dependency(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_demo_user: str | None = Header(default=None),
    x_demo_tenant: str | None = Header(default=None),
    x_demo_store: str | None = Header(default=None),
    x_demo_roles: str | None = Header(default=None),
) -> Principal:
    s = get_settings()

    if s.auth_mode == 'none':
        return Principal(
            user_id=x_demo_user or 'anonymous',
            tenant_id=x_demo_tenant or s.dev_tenant_id,
            store_id=x_demo_store or s.dev_store_id,
            roles=auth_mod.roles_from_claim(x_demo_roles),
            auth_mode='none',
        )

    if s.auth_mode == 'dev':
        tenant_id = x_demo_tenant or s.dev_tenant_id
        # Keep the offline demo self-provisioning: the first request creates the
        # tenant, so a fresh database is usable without a seeding step.
        tenant_mod.ensure_tenant(
            db, tenant_id, name=f'Demo tenant {tenant_id}',
            daily_quota=s.tenant_daily_message_quota,
            rate_limit=s.rate_limit_per_minute,
        )
        return Principal(
            user_id=x_demo_user or 'user001',
            tenant_id=tenant_id,
            store_id=x_demo_store or s.dev_store_id,
            roles=auth_mod.roles_from_claim(x_demo_roles) or frozenset({'customer'}),
            auth_mode='dev',
        )

    # -- jwt --------------------------------------------------------------- #
    if not authorization or not authorization.startswith('Bearer '):
        raise _reject('missing_bearer_token')
    token = authorization.removeprefix('Bearer ').strip()
    try:
        payload = auth_mod.decode_token(token, s.jwt_secret, s.jwt_issuer)
    except auth_mod.TokenError as exc:
        raise _reject('invalid_token', detail=str(exc)) from exc

    tenant_id = str(payload.get('tenant_id') or '')
    if not tenant_id:
        raise _reject('token_missing_tenant')
    if tenant_id != (x_demo_tenant or tenant_id):
        # A client cannot widen its scope by sending a header.
        raise _reject('tenant_mismatch', status=403)

    try:
        tenant_mod.assert_tenant_active(db, tenant_id)
    except tenant_mod.QuotaExceeded as exc:
        raise _reject(exc.kind, status=403, detail=exc.detail) from exc

    roles = auth_mod.roles_from_claim(payload.get('roles'))
    if not roles and payload.get('role'):
        roles = auth_mod.roles_from_claim(payload.get('role'))

    user_id = str(payload['sub'])
    # Resolve the upstream customer id. Scoped by tenant so a token cannot be
    # used to read another tenant's mapping.
    business_customer_id = ''
    user_row = db.get(User, user_id)
    if user_row is not None and user_row.tenant_id == tenant_id:
        business_customer_id = user_row.business_customer_id or ''

    principal = Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        store_id=str(payload.get('store_id') or ''),
        roles=roles,
        email=str(payload.get('email') or ''),
        auth_mode='jwt',
        business_customer_id=business_customer_id,
    )
    request.state.principal = principal
    return principal


def require_role(*required: str):
    """Dependency factory: 403 unless the principal holds one of ``required``."""

    def _dependency(principal: Principal = Depends(principal_dependency)) -> Principal:
        if not principal.has_role(*required):
            raise _reject('role_required', status=403,
                          detail=f'one of {sorted(required)} required')
        return principal

    return _dependency


require_reviewer = require_role('reviewer', 'admin')
require_admin = require_role('admin')
