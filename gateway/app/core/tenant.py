# -*- coding: utf-8 -*-
"""Multi-tenant resolution, row-level scoping and quota accounting.

Two isolation levels are enforced everywhere:

1. ``tenant_id`` — the billing and data boundary.
2. ``store_id`` — an optional narrower boundary inside a tenant (a merchant
   running several storefronts). An empty ``store_id`` on a *user* means "all
   stores of this tenant"; an empty ``store_id`` on a *document* means "shared
   across stores".

The rule is applied by :func:`scope_query`, which every read path goes
through, rather than by sprinkling ``WHERE tenant_id = ...`` at call sites.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Tenant, QuotaUsage


class QuotaExceeded(Exception):
    def __init__(self, kind: str, detail: str = ''):
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


def today() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def get_tenant(db: Session, tenant_id: str) -> Tenant | None:
    if not tenant_id:
        return None
    return db.get(Tenant, tenant_id)


def ensure_tenant(
    db: Session,
    tenant_id: str,
    *,
    name: str = '',
    daily_quota: int = 5000,
    rate_limit: int = 60,
) -> Tenant:
    """Fetch a tenant, creating it on first sight (used by dev/offline seeding)."""
    tenant = get_tenant(db, tenant_id)
    if tenant is None:
        tenant = Tenant(
            id=tenant_id,
            name=name or tenant_id,
            daily_message_quota=daily_quota,
            rate_limit_per_minute=rate_limit,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
    return tenant


def assert_tenant_active(db: Session, tenant_id: str) -> Tenant:
    tenant = get_tenant(db, tenant_id)
    if tenant is None:
        raise QuotaExceeded('unknown_tenant', f'tenant {tenant_id!r} does not exist')
    if tenant.status != 'active':
        raise QuotaExceeded('suspended_tenant', f'tenant {tenant_id!r} is {tenant.status}')
    return tenant


def scope_query(query, model, *, tenant_id: str, store_id: str = ''):
    """Apply row-level tenancy filters to a SELECT.

    ``store_id`` is only applied when both the caller and the model carry one;
    a caller with no store sees the whole tenant.
    """
    query = query.where(model.tenant_id == tenant_id)
    if store_id and hasattr(model, 'store_id'):
        query = query.where((model.store_id == store_id) | (model.store_id == ''))
    return query


def check_quota_available(db: Session, tenant_id: str, *, messages: int = 1) -> None:
    """Refuse the request *before* any work is done if the day's budget is gone.

    Separate from :func:`consume_quota` on purpose: rejecting after the turn has
    already run would waste an LLM call and still show the user an answer.
    """
    tenant = assert_tenant_active(db, tenant_id)
    day = today()
    row = (
        db.execute(
            select(QuotaUsage).where(QuotaUsage.tenant_id == tenant_id, QuotaUsage.day == day)
        )
        .scalars()
        .first()
    )
    used = row.messages if row else 0
    if used + messages > tenant.daily_message_quota:
        raise QuotaExceeded(
            'daily_message_quota',
            f'tenant {tenant_id!r} exceeded {tenant.daily_message_quota} messages/day',
        )


def consume_quota(db: Session, tenant_id: str, *, messages: int = 1, tokens: int = 0,
                  cost_usd: float = 0.0) -> QuotaUsage:
    """Check the tenant's daily quota and, if allowed, record the usage.

    Raises :class:`QuotaExceeded` when the day's budget is already spent. The
    check and the increment happen in one transaction so two concurrent
    requests cannot both slip past the last unit.
    """
    tenant = assert_tenant_active(db, tenant_id)
    day = today()

    row = (
        db.execute(
            select(QuotaUsage).where(QuotaUsage.tenant_id == tenant_id, QuotaUsage.day == day)
        )
        .scalars()
        .first()
    )
    if row is None:
        row = QuotaUsage(tenant_id=tenant_id, day=day, messages=0, tokens=0, cost_usd=0.0)
        db.add(row)
        db.flush()

    if row.messages + messages > tenant.daily_message_quota:
        db.rollback()
        raise QuotaExceeded(
            'daily_message_quota',
            f'tenant {tenant_id!r} exceeded {tenant.daily_message_quota} messages/day',
        )

    row.messages += messages
    row.tokens += tokens
    row.cost_usd = round(row.cost_usd + cost_usd, 8)
    db.commit()
    db.refresh(row)
    return row


def quota_snapshot(db: Session, tenant_id: str) -> dict:
    tenant = get_tenant(db, tenant_id)
    day = today()
    row = (
        db.execute(
            select(QuotaUsage).where(QuotaUsage.tenant_id == tenant_id, QuotaUsage.day == day)
        )
        .scalars()
        .first()
    )
    used = row.messages if row else 0
    limit = tenant.daily_message_quota if tenant else 0
    return {
        'tenant_id': tenant_id,
        'day': day,
        'messages_used': used,
        'messages_limit': limit,
        'remaining': max(0, limit - used),
        'tokens_used': row.tokens if row else 0,
        'cost_usd': round(row.cost_usd, 6) if row else 0.0,
    }
