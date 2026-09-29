# -*- coding: utf-8 -*-
"""Human approval queue for high-risk tool calls.

The agent can *create* a request; only a reviewer can *decide* it. Two separate
credentials and two separate roles enforce that, and the execution path reads
the decision from the database rather than trusting anything in the request
body. After an approval the parked conversation turn is resumed so the user
sees the outcome without sending another message.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import deps
from ..config import get_settings
from ..core import governance
from ..core.observability import log_event
from ..db import get_db
from ..models import Approval, Conversation, ToolInvocation, WorkflowRun
from ..schemas import ApprovalDecision, ApprovalOut
from ..security import Principal, require_reviewer

logger = logging.getLogger('gateway.approvals')
router = APIRouter(prefix='/api/approvals', tags=['approvals'])


def _approval_out(db: Session, approval: Approval) -> ApprovalOut:
    invocation = db.get(ToolInvocation, approval.invocation_id)
    return ApprovalOut(
        id=approval.id,
        tool_name=approval.tool_name,
        risk_level=approval.risk_level,
        status=approval.status,
        reason=approval.reason or '',
        conversation_id=approval.conversation_id or '',
        invocation_id=approval.invocation_id,
        reviewer_id=approval.reviewer_id or '',
        comment=approval.comment or '',
        arguments=dict(invocation.arguments or {}) if invocation else {},
        created_at=approval.created_at.isoformat() if approval.created_at else '',
        expires_at=approval.expires_at.isoformat() if approval.expires_at else '',
    )


@router.get('', response_model=list[ApprovalOut])
def list_approvals(
    status: str = Query(default='pending', max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_reviewer),
):
    stmt = select(Approval).where(Approval.tenant_id == principal.tenant_id)
    if status:
        stmt = stmt.where(Approval.status == status)
    stmt = stmt.order_by(Approval.created_at.desc()).limit(limit)
    return [_approval_out(db, a) for a in db.execute(stmt).scalars()]


@router.get('/{approval_id}', response_model=ApprovalOut)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_reviewer),
):
    approval = db.get(Approval, approval_id)
    if approval is None or approval.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail='approval not found')
    return _approval_out(db, approval)


@router.post('/{approval_id}/decide', response_model=ApprovalOut)
async def decide(
    approval_id: str,
    body: ApprovalDecision,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_reviewer),
):
    settings = get_settings()

    approval, error = governance.decide_approval(
        db, settings,
        approval_id=approval_id,
        tenant_id=principal.tenant_id,
        decision=body.decision,
        reviewer_id=principal.user_id,
        comment=body.comment,
    )
    if approval is None:
        raise HTTPException(status_code=409, detail=error)

    execution: dict | None = None
    if body.decision == 'approve':
        try:
            outcome = await governance.execute_approved_invocation(
                db, deps.business_client,
                invocation_id=approval.invocation_id,
                tenant_id=principal.tenant_id,
                trace_id=getattr(approval, 'trace_id', '') or '',
            )
            execution = {
                'status': outcome.status,
                'result': outcome.result,
                'error': outcome.error,
                'tool': outcome.tool,
                'risk': outcome.risk,
            }
        except governance.ToolDenied as exc:
            execution = {'status': 'failed', 'error': exc.reason, 'tool': approval.tool_name}
            log_event(logger, 'approval_execution_denied', level=logging.ERROR,
                      approval_id=approval_id, reason=exc.reason, detail=exc.detail[:200])
    else:
        execution = {'status': 'rejected', 'tool': approval.tool_name}
        # Relay the rejection as well. Skipping it would leave the business
        # service holding a proposal in ``pending`` for a request this side has
        # already refused — a queue that only ever grows.
        spec = governance.TOOL_REGISTRY.get(approval.tool_name)
        invocation = db.get(ToolInvocation, approval.invocation_id)
        if spec is not None and spec.review_path and invocation is not None:
            try:
                await deps.business_client.review(
                    spec, dict(invocation.arguments or {}),
                    decision='reject', comment=body.comment or '',
                    trace_id=getattr(approval, 'trace_id', '') or '',
                )
            except governance.ToolDenied as exc:
                log_event(logger, 'approval_reject_relay_failed', level=logging.WARNING,
                          approval_id=approval_id, reason=exc.reason, detail=exc.detail[:200])

    await _resume_parked_turn(db, approval, execution, principal)
    db.refresh(approval)
    return _approval_out(db, approval)


async def _resume_parked_turn(db: Session, approval: Approval, execution: dict | None,
                              principal: Principal) -> None:
    """Feed the decision back into the conversation turn that parked."""
    run = (
        db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.tenant_id == approval.tenant_id,
                WorkflowRun.conversation_id == approval.conversation_id,
                WorkflowRun.status == 'waiting',
            )
            .order_by(WorkflowRun.created_at.desc())
        )
        .scalars()
        .first()
    )
    if run is None:
        log_event(logger, 'approval_resume_skipped', level=logging.WARNING,
                  approval_id=approval.id, reason='no_waiting_run')
        return

    state = dict(run.state or {})
    tool = dict(state.get('tool') or {})
    if execution:
        tool['status'] = execution.get('status', 'failed')
        tool['result'] = execution.get('result') or {}
        tool['error'] = execution.get('error', '')
    state['tool'] = tool
    state['approval_decision'] = approval.status
    state['approval_reviewer'] = approval.reviewer_id
    run.state = state
    db.commit()

    conversation = db.get(Conversation, approval.conversation_id)
    if conversation is not None and conversation.status in ('waiting_approval', 'waiting_human'):
        conversation.status = 'active'
        db.commit()

    try:
        result = await deps.engine.resume(db, run)
        log_event(logger, 'approval_resumed_turn', approval_id=approval.id, run_id=run.id,
                  message_id=result.message_id)
    except Exception as exc:  # noqa: BLE001 - the decision is already recorded
        log_event(logger, 'approval_resume_failed', level=logging.ERROR,
                  approval_id=approval.id, run_id=run.id,
                  error=type(exc).__name__, message=str(exc)[:300])
