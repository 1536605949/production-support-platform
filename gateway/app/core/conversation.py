# -*- coding: utf-8 -*-
"""Conversation and message persistence.

Everything the user sees survives a page refresh because it is written to
PostgreSQL, not held in a session. Two invariants matter:

* ``(conversation_id, seq)`` is unique, so message order is stable even when
  several turns are in flight for the same conversation.
* Every read is scoped by ``tenant_id`` **and** ``user_id``. A user can never
  read another user's conversation, and a tenant can never read another
  tenant's — that is the row-level permission rule from the architecture.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Conversation, Message
from .observability import log_event

logger = logging.getLogger('gateway.conversation')

HANDOFF_STATUS = 'waiting_human'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:16]}'


@dataclass
class TurnRecord:
    conversation: Conversation
    user_message: Message
    assistant_message: Message
    sources: list = field(default_factory=list)


def get_conversation(db: Session, conversation_id: str, *, tenant_id: str, user_id: str) -> Conversation | None:
    if not conversation_id:
        return None
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        return None
    # Row-level permission: both the tenant and the owning user must match.
    if conversation.tenant_id != tenant_id or conversation.user_id != user_id:
        log_event(logger, 'conversation_access_denied', level=logging.WARNING,
                  conversation_id=conversation_id, tenant_id=tenant_id, user_id=user_id)
        return None
    return conversation


def get_or_create_conversation(
    db: Session,
    *,
    tenant_id: str,
    store_id: str,
    user_id: str,
    conversation_id: str = '',
    title: str = '',
    channel: str = 'web',
) -> Conversation:
    if conversation_id:
        existing = get_conversation(db, conversation_id, tenant_id=tenant_id, user_id=user_id)
        if existing is not None:
            return existing

    conversation = Conversation(
        id=conversation_id or new_id('CV'),
        tenant_id=tenant_id,
        store_id=store_id,
        user_id=user_id,
        title=(title or '新会话')[:255],
        channel=channel,
        status='active',
        last_message_at=_now(),
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    log_event(logger, 'conversation_created', conversation_id=conversation.id)
    return conversation


def next_seq(db: Session, conversation_id: str) -> int:
    current = (
        db.execute(
            select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
        ).scalar()
    )
    return int(current or 0) + 1


def append_message(
    db: Session,
    *,
    conversation: Conversation,
    role: str,
    content: str,
    intent: str = '',
    confidence: float = 0.0,
    trace_id: str = '',
    latency_ms: float = 0.0,
    ttft_ms: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    sources: list | None = None,
    meta: dict | None = None,
) -> Message:
    message = Message(
        id=new_id('MSG'),
        conversation_id=conversation.id,
        tenant_id=conversation.tenant_id,
        seq=next_seq(db, conversation.id),
        role=role,
        content=content or '',
        intent=intent,
        confidence=confidence,
        trace_id=trace_id,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        sources=sources or [],
        meta=meta or {},
    )
    db.add(message)
    conversation.message_count = (conversation.message_count or 0) + 1
    conversation.last_message_at = _now()
    if role == 'assistant' and intent:
        conversation.last_intent = intent
        conversation.last_confidence = confidence
    if not conversation.title or conversation.title == '新会话':
        if role == 'user' and content:
            conversation.title = content[:60]
    db.commit()
    db.refresh(message)
    return message


def conversation_history(
    db: Session, conversation_id: str, *, tenant_id: str, limit: int = 20
) -> list[Message]:
    """Most recent ``limit`` messages, returned oldest-first."""
    rows = list(
        db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.tenant_id == tenant_id)
            .order_by(Message.seq.desc())
            .limit(limit)
        ).scalars()
    )
    rows.reverse()
    return rows


def list_conversations(
    db: Session, *, tenant_id: str, user_id: str, status: str = '', limit: int = 50, offset: int = 0
) -> list[Conversation]:
    stmt = select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.user_id == user_id
    )
    if status:
        stmt = stmt.where(Conversation.status == status)
    stmt = stmt.order_by(Conversation.last_message_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def conversation_messages(
    db: Session, conversation_id: str, *, tenant_id: str, limit: int = 200
) -> list[Message]:
    return list(
        db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.tenant_id == tenant_id)
            .order_by(Message.seq.asc())
            .limit(limit)
        ).scalars()
    )


def mark_handoff(db: Session, conversation: Conversation, level: int) -> None:
    conversation.handoff_level = max(conversation.handoff_level or 0, level)
    if level >= 2:
        conversation.status = HANDOFF_STATUS
    db.commit()


def close_conversation(db: Session, conversation: Conversation) -> Conversation:
    conversation.status = 'closed'
    db.commit()
    db.refresh(conversation)
    return conversation


def history_as_messages(history: list[Message], *, limit: int = 8) -> list[dict]:
    """Convert stored history into chat messages for the LLM node."""
    out: list[dict] = []
    for message in history[-limit:]:
        if message.role in ('user', 'assistant') and message.content:
            out.append({'role': message.role, 'content': message.content[:800]})
    return out
