# -*- coding: utf-8 -*-
"""Governance layer for tool use.

Four independent gates stand between "the model decided to do something" and
"the business state changed":

1. **Whitelist** — only tools registered in :data:`TOOL_REGISTRY` can be called.
   An unknown name is denied, never passed through.
2. **Argument contract** — arguments are parsed by a Pydantic model. The model
   never supplies a value that is trusted as-is; ownership and status are
   re-checked by the business service.
3. **Risk gate** — every tool carries a risk level. High-risk tools
   (``execute_refund``) always enter ``waiting_approval``; the agent can create
   the request but can never approve it.
4. **Result contract** — the tool's response is validated before it is shown to
   the user, so a malformed upstream response degrades to an error message
   instead of leaking raw internals into the conversation.

Idempotency is a table, not a convention: a retried call with the same key
returns the recorded response instead of executing the side effect twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Approval, IdempotencyKey, ToolInvocation, now
from .observability import log_event

logger = logging.getLogger('gateway.governance')

RISK_ORDER = {'low': 0, 'medium': 1, 'high': 2}


class ToolDenied(Exception):
    def __init__(self, reason: str, detail: str = ''):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class IllegalTransition(Exception):
    def __init__(self, entity: str, from_status: str, to_status: str):
        super().__init__(f'{entity}: {from_status} -> {to_status} is not allowed')
        self.entity = entity
        self.from_status = from_status
        self.to_status = to_status


# --------------------------------------------------------------------------- #
# State machines
# --------------------------------------------------------------------------- #
INVOCATION_TRANSITIONS: dict[str, set[str]] = {
    'proposed': {'waiting_approval', 'executed', 'failed'},
    'waiting_approval': {'approved', 'rejected', 'failed'},
    'approved': {'executed', 'failed'},
    'rejected': set(),
    'executed': set(),
    'failed': set(),
}

APPROVAL_TRANSITIONS: dict[str, set[str]] = {
    'pending': {'approved', 'rejected', 'expired'},
    'approved': set(),
    'rejected': set(),
    'expired': set(),
}


def assert_transition(entity: str, transitions: dict[str, set[str]], from_status: str, to_status: str) -> None:
    """Reject an illegal state transition and record the attempt as a metric."""
    from .. import metrics

    if to_status not in transitions.get(from_status, set()):
        metrics.STATE_MACHINE_VIOLATIONS.labels(entity, from_status, to_status).inc()
        log_event(logger, 'illegal_transition', level=logging.WARNING,
                  entity=entity, from_status=from_status, to_status=to_status)
        raise IllegalTransition(entity, from_status, to_status)


# --------------------------------------------------------------------------- #
# Tool contracts
# --------------------------------------------------------------------------- #
class GetOrderArgs(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=3, max_length=64)


class GetAccountArgs(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)


class CreateTicketArgs(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    issue: str = Field(min_length=2, max_length=2000)


class CreateRefundProposalArgs(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=3, max_length=64)
    reason: str = Field(min_length=2, max_length=500)


class ExecuteRefundArgs(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=3, max_length=64)
    #: Which proposal to execute. Carried explicitly rather than re-derived from
    #: the order, so that "the proposal a human approved" and "the proposal this
    #: call consumes" are provably the same row. It also gives the plan a real
    #: cross-step type dependency: this value comes from
    #: ``create_refund_proposal.proposal_id`` and the verifier checks that it
    #: does — a ``str``-to-``str`` assignment would have checked nothing.
    proposal_id: str = Field(min_length=3, max_length=64)


class GetSkuArgs(BaseModel):
    sku_id: str = Field(min_length=3, max_length=64)


class AftersaleEligibilityArgs(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=3, max_length=64)
    rule_type: str = Field(default='return_no_reason', max_length=32)


class ToolResult(BaseModel):
    """Loose envelope every business response must satisfy."""

    model_config = {'extra': 'allow'}

    status: str = 'ok'


@dataclass
class ToolSpec:
    name: str
    description: str
    risk: str
    args_model: type[BaseModel]
    method: str = 'POST'
    path: str = ''
    #: Path on the business service that must record the human decision before
    #: this tool may run. The business service keeps its *own* review state
    #: machine for the underlying resource; the gateway's ``approvals`` table is
    #: not that state machine. Relaying the decision is what turns "a human said
    #: yes here" into "the order system is allowed to move money". Empty means
    #: the tool has no second state machine to satisfy.
    review_path: str = ''
    #: tools above this risk always need a human approval before execution
    requires_approval: bool = False
    #: roles allowed to call it (empty = any authenticated principal)
    allowed_roles: frozenset[str] = frozenset()
    #: which side effects it has, surfaced to the user in the approval card
    summary_fields: tuple[str, ...] = field(default_factory=tuple)
    #: What this tool returns, field by field.
    #:
    #: Declared here because ``ToolResult`` (the envelope) says only
    #: ``status: str`` and lets everything else through. That is right for
    #: *runtime* validation — a slightly richer upstream payload must not break
    #: a working call — but it leaves the planner with no way to know that
    #: ``create_refund_proposal`` yields a ``proposal_id``, so a cross-step
    #: reference could not be checked at all. These contracts are what
    #: :func:`gateway.app.core.plan.verify_plan` reads, and they are transcribed
    #: from the business service's actual response bodies.
    outputs: tuple['OutputField', ...] = ()
    #: Tools that must appear *earlier in the same plan* before this one runs.
    #:
    #: Distinct from the whitelist: the whitelist says a tool may be called, this
    #: says the plan must have established its precondition first. Without it,
    #: ``execute_refund`` on its own is a legal plan — it would just be executing
    #: against a proposal this turn never created.
    requires_predecessor: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutputField:
    """One field of a tool's response.

    ``type`` is the runtime type (``str`` / ``int`` / ``float`` / ``bool``);
    semantic identity is attached by field name in
    :data:`gateway.app.core.plan.SEMANTIC_IDS`, so that ``order_id`` and
    ``proposal_id`` are not interchangeable just because both are strings.
    """

    name: str
    type: type
    #: ``False`` when the business service omits the field on some success
    #: paths (the eligibility verdict only carries ``deadline`` once a rule was
    #: found). Referencing a sometimes-absent field is legal — the requirement
    #: semantics treat "absent" as "no answer", not as "no".
    always: bool = True


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> ToolSpec:
    TOOL_REGISTRY[spec.name] = spec
    return spec


def _register_default_tools() -> None:
    register_tool(ToolSpec(
        name='get_order',
        description='查询订单状态、金额与是否可退款',
        risk='low',
        args_model=GetOrderArgs,
        method='GET',
        path='/orders/{order_id}',
        summary_fields=('order_id',),
        # Transcribed from business_service/app/main.py::get_order.
        outputs=(
            OutputField('order_id', str),
            OutputField('user_id', str),
            OutputField('status', str),
            OutputField('amount', float),
            OutputField('refund_available', bool),
        ),
    ))
    register_tool(ToolSpec(
        name='get_account',
        description='查询账户等级与状态',
        risk='low',
        args_model=GetAccountArgs,
        method='GET',
        path='/accounts/{user_id}',
        summary_fields=('user_id',),
        outputs=(
            OutputField('user_id', str),
            OutputField('level', str),
            OutputField('status', str),
        ),
    ))
    register_tool(ToolSpec(
        name='get_sku',
        description='查询商品规格、实时库存与价格',
        risk='low',
        args_model=GetSkuArgs,
        method='GET',
        path='/skus/{sku_id}',
        summary_fields=('sku_id',),
        outputs=(
            OutputField('sku_id', str),
            OutputField('name', str),
            OutputField('spec', str),
            OutputField('category', str),
            OutputField('stock', int),
            OutputField('price', float),
            OutputField('status', str),
            OutputField('in_stock', bool),
        ),
    ))
    register_tool(ToolSpec(
        name='check_aftersale_eligibility',
        description='按结构化售后规则表校验这一单是否符合无理由退货条件',
        risk='low',
        args_model=AftersaleEligibilityArgs,
        method='POST',
        path='/aftersale/eligibility',
        # Read-only, so it never parks for approval — but it is the *reason* a
        # write tool gets skipped, which is why it is a registered tool with an
        # argument contract and an audit row rather than an inline HTTP call.
        # "Why was this refund refused" has to be answerable from the trail.
        summary_fields=('order_id',),
        # The verdict's own shape, from service.check_aftersale_eligibility.
        # ``eligible`` is declared ``bool`` and the *value* may be ``None``:
        # three-valued is a runtime property of this endpoint, which is exactly
        # why it is referenced from a ``requires`` predicate (where absent/None
        # does not block) rather than passed as an argument.
        outputs=(
            OutputField('order_id', str),
            OutputField('sku_id', str),
            OutputField('eligible', bool),
            OutputField('reason', str),
            OutputField('sku_name', str, always=False),
            OutputField('category', str, always=False),
            OutputField('rule_id', str, always=False),
            OutputField('policy', str, always=False),
            OutputField('deadline', str, always=False),
        ),
    ))
    register_tool(ToolSpec(
        name='create_ticket',
        description='创建售后工单',
        risk='medium',
        args_model=CreateTicketArgs,
        method='POST',
        path='/tickets',
        summary_fields=('issue',),
        outputs=(
            OutputField('ticket_id', str),
            OutputField('user_id', str),
            OutputField('status', str),
        ),
    ))
    register_tool(ToolSpec(
        name='create_refund_proposal',
        description='创建退款申请（仅提交，不执行）',
        risk='medium',
        args_model=CreateRefundProposalArgs,
        method='POST',
        path='/refunds/proposals',
        summary_fields=('order_id', 'reason'),
        outputs=(
            OutputField('proposal_id', str),
            OutputField('user_id', str),
            OutputField('order_id', str),
            OutputField('reason', str),
            OutputField('status', str),
        ),
        # A refund may not be *proposed* before the rule table has been asked.
        # The check used to be an unconditional pre-gate; making it a
        # predecessor turns "the gate ran" into a property of the plan that can
        # be checked, and keeps it visible in the trace as an ordinary step.
        requires_predecessor=('check_aftersale_eligibility',),
    ))
    register_tool(ToolSpec(
        name='execute_refund',
        description='执行已审批通过的退款（高危，必须人工审批）',
        risk='high',
        args_model=ExecuteRefundArgs,
        method='POST',
        path='/refunds/execute',
        # The order system refuses to move money until *its* proposal is marked
        # approved. The gateway's approval row alone is not enough, so the
        # decision is relayed to this path with the reviewer credential first.
        review_path='/refunds/review',
        # Any authenticated user may *request* it — a customer asking for their
        # own money back is the normal case. What they cannot do is approve it:
        # that is the risk gate below, not a role check.
        requires_approval=True,
        summary_fields=('order_id',),
        outputs=(
            OutputField('proposal_id', str),
            OutputField('order_id', str),
            OutputField('status', str),
            OutputField('message', str),
        ),
        # The money path may only run against a proposal this same plan
        # established. The whitelist alone would happily accept a plan of one
        # ``execute_refund`` step, which is precisely the plan that must never
        # be executable.
        requires_predecessor=('create_refund_proposal',),
    ))


_register_default_tools()


def is_whitelisted(name: str) -> bool:
    return name in TOOL_REGISTRY


def risk_at_least(risk: str, threshold: str) -> bool:
    return RISK_ORDER.get(risk, 0) >= RISK_ORDER.get(threshold, 2)


def validate_arguments(spec: ToolSpec, arguments: dict) -> tuple[dict | None, str]:
    """Parse arguments through the tool's Pydantic contract."""
    try:
        parsed = spec.args_model.model_validate(arguments or {})
    except ValidationError as exc:
        return None, json.dumps(exc.errors(), ensure_ascii=False)[:500]
    return parsed.model_dump(), ''


def validate_result(spec: ToolSpec, payload: Any) -> tuple[dict | None, str]:
    """Validate a tool response before it reaches the user."""
    if not isinstance(payload, dict):
        return None, 'tool result is not a JSON object'
    try:
        ToolResult.model_validate(payload)
    except ValidationError as exc:
        return None, json.dumps(exc.errors(), ensure_ascii=False)[:300]
    return payload, ''


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def request_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


def make_idempotency_key(*parts: str) -> str:
    return hashlib.sha256('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:40]


class IdempotencyStore:
    """Replay store for write operations."""

    def __init__(self, db: Session):
        self.db = db

    def lookup(self, tenant_id: str, scope: str, key: str) -> IdempotencyKey | None:
        if not key:
            return None
        row = (
            self.db.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == tenant_id,
                    IdempotencyKey.scope == scope,
                    IdempotencyKey.key == key,
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            from .. import metrics

            metrics.IDEMPOTENT_REPLAYS.labels(scope).inc()
            log_event(logger, 'idempotent_replay', scope=scope, key=key)
        return row

    def record(self, tenant_id: str, scope: str, key: str, *, request_hash: str = '',
               status_code: int = 200, response: dict | None = None) -> IdempotencyKey:
        row = IdempotencyKey(
            tenant_id=tenant_id,
            scope=scope,
            key=key,
            request_hash=request_hash,
            status_code=status_code,
            response=response or {},
        )
        self.db.add(row)
        try:
            self.db.commit()
        except Exception:  # noqa: BLE001 - concurrent insert of the same key
            self.db.rollback()
        return row


# --------------------------------------------------------------------------- #
# Business service client
# --------------------------------------------------------------------------- #
class BusinessClient:
    """Thin HTTP client for the deterministic business service."""

    def __init__(self, settings: Settings):
        self.base_url = settings.business_service_url.rstrip('/')
        self.tool_key = settings.tool_api_key
        self.reviewer_key = settings.reviewer_api_key
        self.reviewer_id = settings.reviewer_id
        self.timeout = settings.tool_call_timeout_seconds

    def _headers(self, trace_id: str = '', *, reviewer: bool = False) -> dict:
        headers = {
            'X-Trace-ID': trace_id,
            'Content-Type': 'application/json',
        }
        if reviewer:
            headers['X-Reviewer-API-Key'] = self.reviewer_key
        else:
            headers['X-Tool-API-Key'] = self.tool_key
        return headers

    async def call(self, spec: ToolSpec, arguments: dict, trace_id: str = '') -> dict:
        """Invoke the tool's own endpoint.

        Arguments that fill a ``{placeholder}`` in the path go into the path;
        for GET, everything else becomes a query parameter. Dropping those
        leftovers silently turns an ownership check into a 422 — the business
        service refuses to answer an order lookup that does not say who is
        asking.
        """
        path_args = {k: v for k, v in arguments.items() if '{' + k + '}' in spec.path}
        url = f'{self.base_url}{spec.path.format(**path_args) if path_args else spec.path}'
        headers = self._headers(trace_id)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if spec.method == 'GET':
                query = {k: v for k, v in arguments.items() if '{' + k + '}' not in spec.path}
                resp = await client.get(url, headers=headers, params=query)
            else:
                resp = await client.post(url, headers=headers, json=arguments)
        if resp.status_code >= 400:
            raise ToolDenied('upstream_error', f'{resp.status_code}: {resp.text[:300]}')
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ToolDenied('upstream_invalid_json', str(exc)) from exc

    async def review(
        self, spec: ToolSpec, arguments: dict, *, decision: str,
        comment: str = '', trace_id: str = '',
    ) -> dict:
        """Relay a human decision into the business service's review state.

        Uses the reviewer credential, never the tool credential: the whole point
        of the split is that the agent-facing key cannot approve anything. A
        missing credential is a hard error rather than a skipped step, because
        skipping it would leave the order system permanently unable to execute.
        """
        if not self.reviewer_key:
            raise ToolDenied(
                'reviewer_credential_missing',
                'REVIEWER_API_KEY is not configured; the approval cannot be relayed '
                'to the business service',
            )
        url = f'{self.base_url}{spec.review_path}'
        payload = {
            'user_id': arguments.get('user_id', ''),
            'order_id': arguments.get('order_id', ''),
            'decision': 'approve' if decision == 'approve' else 'reject',
            'review_comment': comment or f'decided by {self.reviewer_id}',
        }
        headers = self._headers(trace_id, reviewer=True)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ToolDenied('review_relay_failed', f'{resp.status_code}: {resp.text[:300]}')
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ToolDenied('review_relay_invalid_json', str(exc)) from exc


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class ToolCallOutcome:
    invocation_id: str
    tool: str
    risk: str
    status: str
    result: dict = field(default_factory=dict)
    error: str = ''
    approval_id: str = ''
    replayed: bool = False

    @property
    def executed(self) -> bool:
        return self.status == 'executed'

    @property
    def waiting_approval(self) -> bool:
        return self.status == 'waiting_approval'


def _new_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:12].upper()}'


async def invoke(
    db: Session,
    settings: Settings,
    business: BusinessClient,
    *,
    tool_name: str,
    arguments: dict,
    tenant_id: str,
    store_id: str = '',
    conversation_id: str = '',
    message_id: str = '',
    user_id: str = '',
    actor_roles: frozenset[str] = frozenset(),
    idempotency_key: str = '',
    trace_id: str = '',
    execute_approved: bool = False,
) -> ToolCallOutcome:
    """Run one governed tool call.

    ``execute_approved`` is only ever set by the approval-execution path after
    the database has confirmed an ``approved`` row — never from model output.
    """
    from .. import metrics

    started = time.perf_counter()

    # Gate 1: whitelist.
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        metrics.TOOL_CALLS.labels(tool_name or 'unknown', 'unknown', 'denied').inc()
        raise ToolDenied('tool_not_whitelisted', f'{tool_name!r} is not a registered tool')

    # Gate 2: argument contract.
    parsed, arg_error = validate_arguments(spec, arguments)
    if parsed is None:
        metrics.TOOL_CALLS.labels(tool_name, spec.risk, 'denied').inc()
        raise ToolDenied('invalid_arguments', arg_error)

    if spec.allowed_roles and actor_roles and not (spec.allowed_roles & actor_roles):
        metrics.TOOL_CALLS.labels(tool_name, spec.risk, 'denied').inc()
        raise ToolDenied('role_not_allowed', f'{tool_name!r} requires one of {sorted(spec.allowed_roles)}')

    # Idempotency: a replay returns the original outcome untouched.
    #
    # The default key is scoped to the *turn* (conversation + user message), not
    # just to the arguments. Two separate turns asking to refund the same order
    # are two legitimate requests; a retry of one turn is not.
    key = idempotency_key or make_idempotency_key(
        tenant_id, tool_name, request_fingerprint(parsed), conversation_id, message_id
    )
    store = IdempotencyStore(db)
    existing = store.lookup(tenant_id, f'tool:{tool_name}', key)
    if existing is not None:
        recorded = existing.response or {}
        return ToolCallOutcome(
            invocation_id=recorded.get('invocation_id', ''),
            tool=tool_name,
            risk=spec.risk,
            status=recorded.get('status', 'executed'),
            result=recorded.get('result', {}),
            approval_id=recorded.get('approval_id', ''),
            replayed=True,
        )

    invocation = ToolInvocation(
        id=_new_id('TI'),
        tenant_id=tenant_id,
        store_id=store_id,
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=user_id,
        tool_name=tool_name,
        risk_level=spec.risk,
        arguments=parsed,
        status='proposed',
        idempotency_key=key,
        trace_id=trace_id,
    )
    db.add(invocation)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request won the race for this idempotency key. Re-read
        # the winner instead of surfacing a 500.
        db.rollback()
        existing = (
            db.execute(
                select(ToolInvocation).where(ToolInvocation.idempotency_key == key)
            )
            .scalars()
            .first()
        )
        if existing is None:
            raise
        return ToolCallOutcome(
            invocation_id=existing.id, tool=existing.tool_name, risk=existing.risk_level,
            status=existing.status, result=dict(existing.result or {}), replayed=True,
        )
    db.refresh(invocation)

    needs_approval = spec.requires_approval and not execute_approved

    # Gate 3: risk.
    if needs_approval:
        assert_transition('tool_invocation', INVOCATION_TRANSITIONS, invocation.status, 'waiting_approval')
        invocation.status = 'waiting_approval'
        approval = Approval(
            id=_new_id('AP'),
            tenant_id=tenant_id,
            store_id=store_id,
            invocation_id=invocation.id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            risk_level=spec.risk,
            reason=f'{spec.description}；参数：{json.dumps(parsed, ensure_ascii=False)[:300]}',
            status='pending',
            expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.approval_ttl_hours),
        )
        db.add(approval)
        db.commit()
        db.refresh(invocation)
        db.refresh(approval)

        metrics.TOOL_CALLS.labels(tool_name, spec.risk, 'waiting_approval').inc()
        log_event(logger, 'tool_waiting_approval', tool=tool_name, risk=spec.risk,
                  invocation_id=invocation.id, approval_id=approval.id)
        store.record(tenant_id, f'tool:{tool_name}', key, status_code=202,
                     response={'invocation_id': invocation.id, 'status': 'waiting_approval',
                               'approval_id': approval.id})
        return ToolCallOutcome(
            invocation_id=invocation.id, tool=tool_name, risk=spec.risk,
            status='waiting_approval', approval_id=approval.id,
        )

    # Execute.
    try:
        raw = await business.call(spec, parsed, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        assert_transition('tool_invocation', INVOCATION_TRANSITIONS, invocation.status, 'failed')
        invocation.status = 'failed'
        invocation.error = str(exc)[:500]
        db.commit()
        metrics.TOOL_CALLS.labels(tool_name, spec.risk, 'failed').inc()
        log_event(logger, 'tool_failed', level=logging.WARNING, tool=tool_name,
                  error=type(exc).__name__, message=str(exc)[:300])
        return ToolCallOutcome(invocation_id=invocation.id, tool=tool_name, risk=spec.risk,
                               status='failed', error=str(exc)[:300])

    # Gate 4: result contract.
    validated, result_error = validate_result(spec, raw)
    if validated is None:
        assert_transition('tool_invocation', INVOCATION_TRANSITIONS, invocation.status, 'failed')
        invocation.status = 'failed'
        invocation.error = f'result_contract_violation: {result_error}'
        db.commit()
        metrics.TOOL_CALLS.labels(tool_name, spec.risk, 'failed').inc()
        log_event(logger, 'tool_result_invalid', level=logging.WARNING, tool=tool_name,
                  error=result_error)
        return ToolCallOutcome(invocation_id=invocation.id, tool=tool_name, risk=spec.risk,
                               status='failed', error=result_error)

    assert_transition('tool_invocation', INVOCATION_TRANSITIONS, invocation.status, 'executed')
    invocation.status = 'executed'
    invocation.result = validated
    db.commit()
    db.refresh(invocation)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    metrics.TOOL_CALLS.labels(tool_name, spec.risk, 'executed').inc()
    log_event(logger, 'tool_executed', tool=tool_name, risk=spec.risk,
              invocation_id=invocation.id, elapsed_ms=elapsed_ms)

    store.record(tenant_id, f'tool:{tool_name}', key, status_code=200,
                 response={'invocation_id': invocation.id, 'status': 'executed',
                           'result': validated})

    return ToolCallOutcome(invocation_id=invocation.id, tool=tool_name, risk=spec.risk,
                           status='executed', result=validated)


# --------------------------------------------------------------------------- #
# Approval decisions
# --------------------------------------------------------------------------- #
def decide_approval(
    db: Session,
    settings: Settings,
    *,
    approval_id: str,
    tenant_id: str,
    decision: str,
    reviewer_id: str,
    comment: str = '',
) -> tuple[Approval | None, str]:
    """Record a human decision. Only a ``pending`` approval can be decided."""
    from .. import metrics

    approval = db.get(Approval, approval_id)
    if approval is None or approval.tenant_id != tenant_id:
        return None, 'approval_not_found'
    if approval.status != 'pending':
        return None, f'approval_already_{approval.status}'

    invocation = db.get(ToolInvocation, approval.invocation_id)
    if invocation is None:
        return None, 'invocation_not_found'

    target = 'approved' if decision == 'approve' else 'rejected'
    try:
        assert_transition('approval', APPROVAL_TRANSITIONS, approval.status, target)
        assert_transition('tool_invocation', INVOCATION_TRANSITIONS, invocation.status, target)
    except IllegalTransition as exc:
        return None, str(exc)

    approval.status = target
    approval.reviewer_id = reviewer_id
    approval.comment = comment
    approval.decided_at = now()
    invocation.status = target
    db.commit()
    db.refresh(approval)

    metrics.APPROVALS.labels(target).inc()
    log_event(logger, 'approval_decided', approval_id=approval_id, decision=target,
              reviewer_id=reviewer_id, tool=approval.tool_name)
    return approval, ''


async def execute_approved_invocation(
    db: Session,
    business: BusinessClient,
    *,
    invocation_id: str,
    tenant_id: str,
    trace_id: str = '',
) -> ToolCallOutcome:
    """Execute a tool call that a human has already approved.

    This is the *only* path that can run a high-risk tool. The status is read
    back from the database — a caller cannot pass ``approval_action=approve``
    or any other parameter to shortcut it. The conditional transition
    ``approved -> executed`` guarantees the approval is consumed exactly once,
    even under concurrent retries.
    """
    from .. import metrics

    invocation = db.get(ToolInvocation, invocation_id)
    if invocation is None or invocation.tenant_id != tenant_id:
        raise ToolDenied('invocation_not_found', invocation_id)
    if invocation.status != 'approved':
        metrics.STATE_MACHINE_VIOLATIONS.labels('tool_invocation', invocation.status, 'executed').inc()
        raise ToolDenied('approval_required', f'invocation is {invocation.status}, not approved')

    spec = TOOL_REGISTRY.get(invocation.tool_name)
    if spec is None:
        raise ToolDenied('tool_not_whitelisted', invocation.tool_name)

    # Claim the approval first. If another worker already consumed it, the
    # conditional UPDATE affects 0 rows and we bail out without side effects.
    claimed = (
        db.query(ToolInvocation)
        .filter(ToolInvocation.id == invocation_id, ToolInvocation.status == 'approved')
        .update({'status': 'executed'}, synchronize_session=False)
    )
    db.commit()
    if claimed != 1:
        db.rollback()
        metrics.STATE_MACHINE_VIOLATIONS.labels('tool_invocation', 'approved', 'executed').inc()
        raise ToolDenied('approval_already_consumed', invocation_id)

    try:
        arguments = dict(invocation.arguments or {})
        if spec.review_path:
            # The business service keeps its own review state for the resource.
            # Relaying the human decision first is what makes the execute call
            # legal over there — without it the order system answers
            # ``approval_required`` and the money never moves, even though this
            # side recorded a perfectly good approval.
            approval = (
                db.execute(select(Approval).where(Approval.invocation_id == invocation_id))
                .scalars()
                .first()
            )
            await business.review(
                spec, arguments,
                decision='approve',
                comment=(approval.comment if approval else '') or '',
                trace_id=trace_id,
            )
        raw = await business.call(spec, arguments, trace_id=trace_id)
        validated, result_error = validate_result(spec, raw)
        if validated is None:
            raise ToolDenied('result_contract_violation', result_error)
    except Exception as exc:  # noqa: BLE001
        # The approval is already consumed: a failed execution must not be
        # silently retryable without a fresh human decision.
        invocation = db.get(ToolInvocation, invocation_id)
        invocation.status = 'failed'
        invocation.error = str(exc)[:500]
        db.commit()
        metrics.TOOL_CALLS.labels(spec.name, spec.risk, 'failed').inc()
        log_event(logger, 'approved_tool_failed', level=logging.ERROR, tool=spec.name,
                  invocation_id=invocation_id, message=str(exc)[:300])
        return ToolCallOutcome(invocation_id=invocation_id, tool=spec.name, risk=spec.risk,
                               status='failed', error=str(exc)[:300])

    invocation = db.get(ToolInvocation, invocation_id)
    invocation.result = validated
    db.commit()

    metrics.TOOL_CALLS.labels(spec.name, spec.risk, 'executed').inc()
    log_event(logger, 'approved_tool_executed', tool=spec.name, risk=spec.risk,
              invocation_id=invocation_id)
    return ToolCallOutcome(invocation_id=invocation_id, tool=spec.name, risk=spec.risk,
                           status='executed', result=validated)


def pending_approvals(db: Session, tenant_id: str, *, limit: int = 50) -> list[Approval]:
    return list(
        db.execute(
            select(Approval)
            .where(Approval.tenant_id == tenant_id, Approval.status == 'pending')
            .order_by(Approval.created_at.desc())
            .limit(limit)
        ).scalars()
    )
