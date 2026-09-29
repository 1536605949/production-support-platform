# -*- coding: utf-8 -*-
"""Chat endpoints — SSE streaming and blocking, over the deterministic trunk.

The route only does transport work: resolve identity, enforce limits, hand the
turn to the engine, serialise the result. Routing, retrieval, governance and
persistence all live in ``core/``.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import deps
from ..config import get_settings
from ..core import observability
from ..core.engine import TurnRequest, TurnResult
from ..db import get_db
from ..dify_client import DifyError
from ..schemas import ChatRequest, ChatResponse, HitlSubmitRequest
from ..security import Principal, principal_dependency, require_reviewer

logger = logging.getLogger('gateway.chat')
router = APIRouter(prefix='/api', tags=['chat'])


def _sse(event: str, payload: dict) -> str:
    body = json.dumps({'event': event, **payload}, ensure_ascii=False, default=str)
    return f'data: {body}\n\n'


def _to_response(result: TurnResult, request_id: str, trace_id: str) -> ChatResponse:
    return ChatResponse(
        request_id=request_id,
        trace_id=trace_id,
        answer=result.answer,
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        intent=result.intent,
        intent_label=result.intent_label,
        confidence=result.confidence,
        intent_source=result.intent_source,
        handoff_level=result.handoff_level,
        suggestions=result.suggestions,
        sources=result.sources,
        tool=result.tool,
        approval_id=result.approval_id,
        workflow_run_id=result.workflow_run_id,
        degraded=result.degraded,
        plan=result.plan,
        metadata={
            'workflow_status': result.workflow_status,
            'tokens_in': result.tokens_in,
            'tokens_out': result.tokens_out,
            'cost_usd': result.cost_usd,
            'latency_ms': result.latency_ms,
            'ttft_ms': result.ttft_ms,
        },
    )


def _turn_request(principal: Principal, body: ChatRequest, request: Request) -> TurnRequest:
    return TurnRequest(
        tenant_id=principal.tenant_id,
        store_id=principal.store_id,
        user_id=principal.user_id,
        # Tool arguments must use the upstream customer id, which is a different
        # identity space from the platform's user id.
        business_customer_id=principal.business_ref,
        message=body.message,
        conversation_id=body.conversation_id,
        idempotency_key=body.idempotency_key,
        trace_id=getattr(request.state, 'trace_id', ''),
        actor_roles=principal.roles,
    )


@router.post('/chat', response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(principal_dependency),
):
    settings = get_settings()
    await deps.enforce_limits(principal, db)

    if settings.engine_mode == 'dify':
        return await _legacy_dify_chat(body, request, principal)

    observability.bind(tenant_id=principal.tenant_id, user_id=principal.user_id,
                       conversation_id=body.conversation_id)
    result = await deps.engine.handle_turn(db, _turn_request(principal, body, request))
    return _to_response(result, request.state.request_id, request.state.trace_id)


@router.post('/chat/stream')
async def chat_stream(
    body: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(principal_dependency),
):
    settings = get_settings()
    await deps.enforce_limits(principal, db)

    request_id = request.state.request_id
    trace_id = request.state.trace_id

    if settings.engine_mode == 'dify':
        return await _legacy_dify_stream(body, principal)

    observability.bind(tenant_id=principal.tenant_id, user_id=principal.user_id,
                       conversation_id=body.conversation_id)
    turn = _turn_request(principal, body, request)

    async def generate():
        yield _sse('start', {'request_id': request_id, 'trace_id': trace_id})
        try:
            async for event, payload in deps.engine.stream_turn(db, turn):
                if event == 'delta':
                    yield _sse('delta', {'text': payload})
                elif event == 'replace':
                    yield _sse('replace', {'text': payload})
                elif event == 'final':
                    final = _to_response(payload, request_id, trace_id)
                    yield _sse('final', final.model_dump())
        except Exception as exc:  # noqa: BLE001 - the stream is already committed to 200
            logger.exception('chat stream failed')
            yield _sse('error', {'message': f'{type(exc).__name__}: {exc}'[:300]})
        yield 'data: [DONE]\n\n'

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'X-Request-ID': request_id,
            'X-Trace-ID': trace_id,
        },
    )


# --------------------------------------------------------------------------- #
# Legacy Dify proxy (ENGINE_MODE=dify)
# --------------------------------------------------------------------------- #
async def _legacy_dify_chat(body: ChatRequest, request: Request, principal: Principal):
    client = deps.get_dify_client()
    try:
        result = await client.chat_blocking(
            query=body.message, user=principal.user_id,
            conversation_id=body.conversation_id, inputs=body.inputs,
        )
    except DifyError as exc:
        mapped = 504 if exc.status_code in (408, 504) else 502
        raise HTTPException(status_code=mapped, detail=str(exc)) from exc
    return ChatResponse(
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
        answer=result.get('answer', ''),
        conversation_id=result.get('conversation_id', ''),
        message_id=result.get('message_id', ''),
        metadata=result.get('metadata') or {},
    )


async def _legacy_dify_stream(body: ChatRequest, principal: Principal):
    client = deps.get_dify_client()

    async def generate():
        try:
            async for line in client.chat_stream(
                query=body.message, user=principal.user_id,
                conversation_id=body.conversation_id, inputs=body.inputs,
            ):
                yield line + '\n\n'
        except DifyError as exc:
            yield _sse('gateway_error', {'message': str(exc)})

    return StreamingResponse(generate(), media_type='text/event-stream')


@router.post('/hitl/{form_token}')
async def submit_hitl(
    form_token: str,
    body: HitlSubmitRequest,
    principal: Principal = Depends(require_reviewer),
):
    """Legacy Dify Human-Input bridge. The builtin engine uses /api/approvals."""
    client = deps.get_dify_client()
    try:
        return await client.submit_human_input(
            form_token=form_token, user=principal.user_id,
            inputs=body.inputs, action=body.action,
        )
    except DifyError as exc:
        mapped = 504 if exc.status_code in (408, 504) else 502
        raise HTTPException(status_code=mapped, detail=str(exc)) from exc
