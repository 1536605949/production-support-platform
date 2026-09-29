# -*- coding: utf-8 -*-
"""Conversation history endpoints.

This is what makes "refresh the page and the history is still there" true: the
list and the messages are read from PostgreSQL, always filtered by the
caller's tenant *and* user.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core import conversation as conv
from ..db import get_db
from ..schemas import ConversationDetail, ConversationOut, MessageOut
from ..security import Principal, principal_dependency

router = APIRouter(prefix='/api/conversations', tags=['conversations'])


def _conversation_out(c) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        title=c.title or '',
        status=c.status,
        last_intent=c.last_intent or '',
        handoff_level=c.handoff_level or 0,
        message_count=c.message_count or 0,
        last_message_at=c.last_message_at.isoformat() if c.last_message_at else '',
        created_at=c.created_at.isoformat() if c.created_at else '',
    )


def _message_out(m) -> MessageOut:
    # ``intent_source`` / ``compose_source`` / ``degraded`` are stored inside the
    # ``meta`` JSON blob, so they have to be lifted out here rather than read
    # straight off a column.
    meta = m.meta or {}
    return MessageOut(
        id=m.id,
        seq=m.seq,
        role=m.role,
        content=m.content,
        intent=m.intent or '',
        confidence=m.confidence or 0.0,
        sources=m.sources or [],
        created_at=m.created_at.isoformat() if m.created_at else '',
        ttft_ms=m.ttft_ms or 0.0,
        latency_ms=m.latency_ms or 0.0,
        intent_source=meta.get('intent_source') or '',
        compose_source=meta.get('compose_source') or '',
        degraded=bool(meta.get('degraded')),
        tokens_in=m.tokens_in or 0,
        tokens_out=m.tokens_out or 0,
        cost_usd=m.cost_usd or 0.0,
        plan=meta.get('plan'),
    )


@router.get('', response_model=list[ConversationOut])
def list_conversations(
    status: str = Query(default='', max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(principal_dependency),
):
    rows = conv.list_conversations(
        db, tenant_id=principal.tenant_id, user_id=principal.user_id,
        status=status, limit=limit, offset=offset,
    )
    return [_conversation_out(c) for c in rows]


@router.get('/{conversation_id}', response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(principal_dependency),
):
    conversation = conv.get_conversation(
        db, conversation_id, tenant_id=principal.tenant_id, user_id=principal.user_id
    )
    if conversation is None:
        # 404 rather than 403: another tenant's id must not be confirmed to exist.
        raise HTTPException(status_code=404, detail='conversation not found')
    messages = conv.conversation_messages(
        db, conversation.id, tenant_id=principal.tenant_id
    )
    return ConversationDetail(
        conversation=_conversation_out(conversation),
        messages=[_message_out(m) for m in messages],
    )


@router.post('/{conversation_id}/close', response_model=ConversationOut)
def close_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(principal_dependency),
):
    conversation = conv.get_conversation(
        db, conversation_id, tenant_id=principal.tenant_id, user_id=principal.user_id
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail='conversation not found')
    return _conversation_out(conv.close_conversation(db, conversation))
