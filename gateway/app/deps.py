# -*- coding: utf-8 -*-
"""Process-wide singletons shared by the routers.

Kept in its own module so routers can depend on them without importing
``main`` (which would be a circular import).
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import metrics
from .config import get_settings
from .core import tenant as tenant_mod
from .core.engine import ConversationEngine
from .core.governance import BusinessClient
from .core.llm import LLMClient
from .db import get_db
from .rate_limit import RateLimiter
from .security import Principal

logger = logging.getLogger('gateway.deps')

settings = get_settings()
llm_client = LLMClient(settings)
business_client = BusinessClient(settings)
engine = ConversationEngine(settings, llm_client, business_client)
limiter = RateLimiter(settings.redis_url, settings.rate_limit_per_minute)


def _build_dify_client():
    from .dify_client import DifyClient

    return DifyClient(
        settings.dify_base_url, settings.dify_api_key,
        settings.dify_timeout_seconds, mock=settings.dify_mock,
    )


# Only built when the legacy engine mode is actually selected.
dify_client = _build_dify_client() if settings.engine_mode == 'dify' else None


def get_engine() -> ConversationEngine:
    return engine


def get_business_client() -> BusinessClient:
    return business_client


def get_dify_client():
    global dify_client
    if dify_client is None:
        dify_client = _build_dify_client()
    return dify_client


async def enforce_limits(principal: Principal, db: Session) -> None:
    """Per-tenant rate limit + daily quota.

    The minute-level limiter protects the process; the daily quota protects the
    contract. Both are checked before any work happens.
    """
    per_minute = principal_tenant_rate_limit(principal)
    if not await limiter.allow(f'{principal.tenant_id}:{principal.user_id}', limit=per_minute):
        metrics.QUOTA_REJECTIONS.labels('rate').inc()
        raise HTTPException(status_code=429, detail='rate limit exceeded')

    try:
        tenant_mod.check_quota_available(db, principal.tenant_id)
    except tenant_mod.QuotaExceeded as exc:
        metrics.QUOTA_REJECTIONS.labels('quota').inc()
        raise HTTPException(status_code=403, detail=exc.detail or exc.kind) from exc


def principal_tenant_rate_limit(principal: Principal) -> int:
    from .core.tenant import get_tenant
    from .db import SessionLocal

    with SessionLocal() as db:
        tenant = get_tenant(db, principal.tenant_id)
        return tenant.rate_limit_per_minute if tenant else settings.rate_limit_per_minute


def request_trace_ids(request: Request) -> tuple[str, str]:
    return (
        getattr(request.state, 'request_id', ''),
        getattr(request.state, 'trace_id', ''),
    )
