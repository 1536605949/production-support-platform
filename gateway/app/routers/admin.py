# -*- coding: utf-8 -*-
"""Tenant-scoped operations console.

Everything here is derived from the same tables the trunk writes, so the
numbers cannot drift from the behaviour: completion rate counts stored
assistant messages, the handoff rate counts conversations that actually went
to ``waiting_human``, and TTFT percentiles come from the persisted per-message
``ttft_ms``.

Deliberately *not* using Prometheus for these: tenant-scoped numbers would
require an unbounded label. Metrics answer "is the service healthy"; this
endpoint answers "how is this merchant doing".
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core import conversation as conv
from ..core import knowledge, tenant as tenant_mod
from ..db import get_db
from ..models import Approval, Conversation, KnowledgeDocument, Message, ToolInvocation
from ..security import Principal, principal_dependency, require_role

logger = logging.getLogger('gateway.admin')
router = APIRouter(prefix='/api/admin', tags=['admin'])


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * len(ordered) + 0.5)) - 1))
    return round(ordered[index], 2)


@router.get('/overview')
def overview(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    principal: Principal = Depends(principal_dependency),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    tenant_id = principal.tenant_id

    # --- conversation aggregates stay in the database --------------------- #
    # The console used to load every Conversation in the window as an ORM
    # object and count them in Python. Both numbers are pure aggregates, so
    # they are pushed down: this is one indexed query returning two integers
    # instead of N hydrated rows, and it no longer grows with traffic.
    # ``case`` rather than two round trips — it is portable across SQLite
    # (tests) and PostgreSQL (deployment).
    conversation_total, handed_off = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((Conversation.handoff_level > 0, 1), else_=0)), 0),
        )
        .select_from(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.created_at >= since)
    ).one()

    # --- messages: fetch narrow columns only ------------------------------ #
    # Per-row data is still needed for the JSON-derived breakdowns
    # (``meta.degraded`` / ``intent_source`` / ``compose_source`` /
    # ``cache_hit_tokens``) and for the daily buckets, and those live in JSON
    # that no portable SQL expression can reduce. What *can* be avoided is
    # reading the wide columns: ``content`` (the full answer text) and
    # ``sources`` (a list of every retrieved passage, the widest column in the
    # table). Selecting explicit columns instead of entities also skips the
    # ORM identity map, which for a reporting query is pure overhead.
    rows = db.execute(
        select(
            Message.role, Message.intent, Message.meta, Message.ttft_ms,
            Message.latency_ms, Message.tokens_in, Message.tokens_out,
            Message.cost_usd, Message.created_at,
        ).where(Message.tenant_id == tenant_id, Message.created_at >= since)
    ).all()

    assistant = [r for r in rows if r.role == 'assistant']
    total_answers = len(assistant)

    ttfts = [r.ttft_ms for r in assistant if r.ttft_ms and r.ttft_ms > 0]
    latencies = [r.latency_ms for r in assistant if r.latency_ms and r.latency_ms > 0]
    degraded = sum(1 for r in assistant if (r.meta or {}).get('degraded'))

    intent_counts: dict[str, int] = {}
    intent_source_counts: dict[str, int] = {}
    compose_source_counts: dict[str, int] = {}
    for message in assistant:
        if message.intent:
            intent_counts[message.intent] = intent_counts.get(message.intent, 0) + 1
        meta = message.meta or {}
        source = meta.get('intent_source') or 'unknown'
        intent_source_counts[source] = intent_source_counts.get(source, 0) + 1
        composed = meta.get('compose_source') or 'unknown'
        compose_source_counts[composed] = compose_source_counts.get(composed, 0) + 1

    tool_rows = list(
        db.execute(
            select(ToolInvocation.status, func.count())
            .where(ToolInvocation.tenant_id == tenant_id, ToolInvocation.created_at >= since)
            .group_by(ToolInvocation.status)
        ).all()
    )
    approval_rows = list(
        db.execute(
            select(Approval.status, func.count())
            .where(Approval.tenant_id == tenant_id, Approval.created_at >= since)
            .group_by(Approval.status)
        ).all()
    )

    total_answers = len(assistant)
    tokens_in = sum(m.tokens_in or 0 for m in assistant)
    # ``cache_hit_tokens`` is a *subset* of ``tokens_in``, recorded per message
    # by the engine. DeepSeek bills a cache hit ~50x cheaper than a miss, so a
    # rate near zero means the fixed system prompt is being re-bought at full
    # price on every turn — a cost problem with no other visible symptom.
    cache_hit_tokens = sum(int((m.meta or {}).get('cache_hit_tokens') or 0) for m in assistant)

    # Daily buckets. The range is filled in explicitly: a sparse series would
    # make a quiet day disappear from the chart and the x-axis would compress,
    # which reads as "traffic was steady" when it was actually zero.
    buckets: dict[str, dict] = {}
    cursor = since.date()
    today = datetime.now(timezone.utc).date()
    while cursor <= today:
        buckets[cursor.isoformat()] = {'messages': 0, 'answers': 0, 'degraded': 0, 'cost_usd': 0.0}
        cursor += timedelta(days=1)
    for message in rows:
        day = message.created_at.date().isoformat() if message.created_at else None
        if day not in buckets:
            continue
        bucket = buckets[day]
        bucket['messages'] += 1
        if message.role == 'assistant':
            bucket['answers'] += 1
            if (message.meta or {}).get('degraded'):
                bucket['degraded'] += 1
            bucket['cost_usd'] = round(bucket['cost_usd'] + (message.cost_usd or 0.0), 6)
    daily = [dict(day=day, **values) for day, values in sorted(buckets.items())]

    return {
        'tenant_id': tenant_id,
        'window_days': days,
        'conversations': conversation_total,
        'messages': len(rows),
        'answers': total_answers,
        # completion rate = answers that were not degraded / total answers
        'completion_rate': round(1 - (degraded / total_answers), 4) if total_answers else 1.0,
        'degraded_answers': degraded,
        'degraded_rate': round(degraded / total_answers, 4) if total_answers else 0.0,
        'handoff_conversations': handed_off,
        'handoff_rate': round(handed_off / conversation_total, 4) if conversation_total else 0.0,
        'ttft_ms_p50': _percentile(ttfts, 50),
        'ttft_ms_p95': _percentile(ttfts, 95),
        'latency_ms_p50': _percentile(latencies, 50),
        'latency_ms_p95': _percentile(latencies, 95),
        'tokens_in': tokens_in,
        'tokens_out': sum(m.tokens_out or 0 for m in assistant),
        'cache_hit_tokens': cache_hit_tokens,
        'cache_hit_rate': round(cache_hit_tokens / tokens_in, 4) if tokens_in else 0.0,
        'cost_usd': round(sum(m.cost_usd or 0.0 for m in assistant), 6),
        'intent_distribution': intent_counts,
        # How the intent was decided, and who wrote the answer. Both answer
        # "was the model actually involved" — the number this platform is built
        # to keep as low as honestly possible.
        'intent_source': intent_source_counts,
        'compose_source': compose_source_counts,
        'tool_status': {status: count for status, count in tool_rows},
        'approval_status': {status: count for status, count in approval_rows},
        'daily': daily,
        'quota': tenant_mod.quota_snapshot(db, tenant_id),
    }


@router.get('/quota')
def quota(db: Session = Depends(get_db), principal: Principal = Depends(principal_dependency)):
    return tenant_mod.quota_snapshot(db, principal.tenant_id)


@router.get('/traces/{trace_id}')
def trace(trace_id: str, db: Session = Depends(get_db),
          principal: Principal = Depends(principal_dependency)):
    """Everything the trunk persisted under one trace id."""
    messages = list(
        db.execute(
            select(Message).where(Message.tenant_id == principal.tenant_id,
                                  Message.trace_id == trace_id).order_by(Message.seq)
        ).scalars()
    )
    invocations = list(
        db.execute(
            select(ToolInvocation).where(ToolInvocation.tenant_id == principal.tenant_id,
                                         ToolInvocation.trace_id == trace_id)
        ).scalars()
    )
    if not messages and not invocations:
        raise HTTPException(status_code=404, detail='trace not found')
    return {
        'trace_id': trace_id,
        'messages': [
            {'id': m.id, 'role': m.role, 'seq': m.seq, 'intent': m.intent,
             'confidence': m.confidence, 'ttft_ms': m.ttft_ms, 'latency_ms': m.latency_ms,
             'tokens_in': m.tokens_in, 'tokens_out': m.tokens_out, 'cost_usd': m.cost_usd,
             'meta': m.meta, 'sources': m.sources, 'created_at': m.created_at.isoformat()}
            for m in messages
        ],
        'tool_invocations': [
            {'id': i.id, 'tool': i.tool_name, 'risk': i.risk_level, 'status': i.status,
             'arguments': i.arguments, 'result': i.result, 'error': i.error}
            for i in invocations
        ],
    }


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #
class KnowledgeIn(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    content: str = Field(min_length=2, max_length=8000)
    category: str = Field(default='general', max_length=64)
    keywords: str = Field(default='', max_length=500)
    store_id: str = Field(default='', max_length=64)
    #: '' (default) = visible to every tenant; otherwise tenant-private
    scope: str = Field(default='tenant', pattern='^(tenant|global)$')


@router.get('/knowledge')
def list_knowledge(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role('admin', 'agent')),
):
    rows = db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.tenant_id.in_([principal.tenant_id, '']))
        .order_by(KnowledgeDocument.updated_at.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            'id': d.id, 'tenant_id': d.tenant_id, 'store_id': d.store_id,
            'category': d.category, 'title': d.title, 'content': d.content,
            'keywords': d.keywords, 'enabled': d.enabled,
            'scope': 'global' if d.tenant_id == '' else 'tenant',
            'has_embedding': bool(d.embedding),
        }
        for d in rows
    ]


@router.post('/knowledge', status_code=201)
def create_knowledge(
    body: KnowledgeIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role('admin', 'agent')),
):
    document = KnowledgeDocument(
        id=f'KB-{uuid.uuid4().hex[:12]}',
        tenant_id='' if body.scope == 'global' else principal.tenant_id,
        store_id=body.store_id,
        category=body.category,
        title=body.title,
        content=body.content,
        keywords=body.keywords,
        enabled=True,
    )
    db.add(document)
    db.commit()
    knowledge.invalidate_cache()
    return {'id': document.id, 'title': document.title}


@router.delete('/knowledge/{document_id}', status_code=204, response_model=None)
def delete_knowledge(
    document_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role('admin')),
):
    document = db.get(KnowledgeDocument, document_id)
    if document is None or document.tenant_id != principal.tenant_id:
        # A tenant may not delete a global document, nor another tenant's.
        raise HTTPException(status_code=404, detail='knowledge document not found')
    db.delete(document)
    db.commit()
    knowledge.invalidate_cache()
    return None
