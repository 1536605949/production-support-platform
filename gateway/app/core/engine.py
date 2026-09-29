# -*- coding: utf-8 -*-
"""The deterministic trunk.

One user turn is one durable workflow::

    ingest → route → retrieve → plan_tools → compose → persist

* **ingest** writes the user message before anything else can fail.
* **route** is deterministic first; the LLM is consulted *only* when the
  weighted-keyword score is below ``INTENT_LLM_THRESHOLD``.
* **retrieve** is permission-filtered hybrid retrieval (BM25 + optional vector).
* **plan_tools** builds a *plan* — a typed, acyclic dependency graph — verifies
  it, and only then executes it. Low/medium risk steps run inline; a high-risk
  step parks the run in ``waiting_approval``. An invalid plan executes nothing
  at all: see :mod:`gateway.app.core.plan`.
* **compose** produces the answer. With a model configured it is a grounded,
  streamed completion; without one it is a deterministic template built from
  the tool result and retrieved evidence. No model, no bluffing.
* **persist** stores the assistant message, consumes quota and updates the
  conversation status.

Each step is checkpointed, so a crashed process can resume a turn instead of
losing it — and the same step functions could be lifted onto Temporal
activities without changing their bodies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from . import conversation as conv
from . import governance, intent as intent_mod, knowledge
from . import plan as plan_mod
from .llm import LLMClient
from .observability import log_event
from .workflow import StepContext, StepOutcome, WorkflowEngine, define_workflow, step

logger = logging.getLogger('gateway.engine')

WORKFLOW_TYPE = 'conversation_turn'
# ``await_approval`` is the parking step: it is reached only after the interim
# message has already been composed and stored, so a user waiting on a human
# reviewer still sees a reply. ``notify`` runs when the run is resumed.
STEPS = ['ingest', 'route', 'retrieve', 'plan_tools', 'compose', 'persist',
         'await_approval', 'notify']
define_workflow(WORKFLOW_TYPE, STEPS)

STREAM_END = object()

# --- tool planning rules ---------------------------------------------------- #
_CONFIRM_EXECUTE = (
    '确认退款', '确认执行', '立即退款', '同意退款', '执行退款', '马上退', '现在就退', '直接退给我',
)
_TICKET_HINTS = ('建工单', '创建工单', '提交工单', '开个工单', '报修', '反馈问题', '登记一下', '登记问题')

HANDOFF_MESSAGES = {
    1: '这个问题我把握不大。如果需要，可以回复「转人工」由人工客服跟进。',
    2: '这个问题超出了我的能力范围，已经为你转接人工客服，请稍候。',
    3: '好的，正在为你转接人工客服，请稍候。',
}

GREETING = '你好，我是智能客服助手。我可以帮你查订单物流、处理退款申请、排查常见故障，也可以查询发票、优惠券等规则。'


@dataclass
class TurnRequest:
    tenant_id: str
    user_id: str
    message: str
    store_id: str = ''
    conversation_id: str = ''
    idempotency_key: str = ''
    trace_id: str = ''
    channel: str = 'web'
    actor_roles: frozenset[str] = frozenset()
    #: Customer id in the upstream order system. Falls back to ``user_id`` when
    #: the two identity spaces coincide (the offline demo).
    business_customer_id: str = ''

    @property
    def business_ref(self) -> str:
        return self.business_customer_id or self.user_id


@dataclass
class TurnResult:
    conversation_id: str = ''
    message_id: str = ''
    answer: str = ''
    intent: str = ''
    intent_label: str = ''
    confidence: float = 0.0
    intent_source: str = ''
    sources: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    handoff_level: int = 0
    tool: dict | None = None
    approval_id: str = ''
    workflow_run_id: str = ''
    workflow_status: str = ''
    #: The plan this turn ran plus the ledger of every verification gate it
    #: passed. Carried on the response (not only in the database) because
    #: "why was this AI action allowed?" is a question asked at the moment the
    #: answer is read, and requiring a second query to answer it is how a
    #: decision record stops being read at all.
    plan: dict | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    ttft_ms: float = 0.0
    degraded: bool = False
    error: str = ''


# --------------------------------------------------------------------------- #
# Step: ingest
# --------------------------------------------------------------------------- #
@step(WORKFLOW_TYPE, 'ingest')
async def _ingest(ctx: StepContext) -> StepOutcome:
    request: TurnRequest = ctx.services['request']
    conversation = conv.get_or_create_conversation(
        ctx.db,
        tenant_id=request.tenant_id,
        store_id=request.store_id,
        user_id=request.user_id,
        conversation_id=request.conversation_id,
        title=request.message,
        channel=request.channel,
    )
    user_message = conv.append_message(
        ctx.db, conversation=conversation, role='user', content=request.message,
        trace_id=request.trace_id,
    )
    # The request may have arrived without a conversation id (first turn). Bind
    # the real one onto the run so the approval queue can find it later.
    if ctx.run.conversation_id != conversation.id:
        ctx.run.conversation_id = conversation.id
        ctx.db.commit()
    history = conv.conversation_history(
        ctx.db, conversation.id, tenant_id=conversation.tenant_id, limit=10
    )
    return StepOutcome.proceed(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        history=[
            {'role': m.role, 'content': m.content, 'intent': m.intent}
            for m in history if m.role in ('user', 'assistant')
        ],
    )


# --------------------------------------------------------------------------- #
# Step: route
# --------------------------------------------------------------------------- #
@step(WORKFLOW_TYPE, 'route')
async def _route(ctx: StepContext) -> StepOutcome:
    from .. import metrics

    request: TurnRequest = ctx.services['request']
    llm: LLMClient = ctx.services['llm']
    settings: Settings = ctx.services['settings']

    # Multi-turn context: the topic of the previous *assistant* turn lets a
    # short follow-up ("那能退多少？") inherit the earlier subject.
    history = ctx.state.get('history') or []
    previous_intent = ''
    for message in reversed(history[:-1]):
        if message.get('role') == 'assistant' and message.get('intent'):
            previous_intent = message['intent']
            break

    low_streak = int(ctx.state.get('low_confidence_streak', 0))

    decision = intent_mod.route(
        request.message,
        previous_intent=previous_intent,
        llm_threshold=settings.intent_llm_threshold,
        handoff_threshold=settings.intent_handoff_threshold,
        low_confidence_streak=low_streak,
        handoff_streak_limit=settings.handoff_frustration_turns,
        llm_available=llm.available,
    )

    result = decision.result
    source = decision.source
    handoff_level = decision.handoff_level
    tokens_in = tokens_out = 0
    cache_hit = 0
    cost = 0.0

    # The LLM node: only reached when the deterministic score is unconvincing.
    if decision.need_llm:
        candidates = [i.value for i in intent_mod.Intent if i.value != intent_mod.Intent.HUMAN_HANDOFF.value]
        llm_intent, llm_confidence, llm_result = await llm.classify_intent(request.message, candidates)
        tokens_in += llm_result.tokens_in
        tokens_out += llm_result.tokens_out
        # Cache hits are a subset of the prompt tokens, billed ~50x cheaper.
        # Carried through to the persisted message so the console can answer
        # "are we paying full price for a system prompt that never changes".
        cache_hit += llm_result.cache_hit_tokens
        cost += llm_result.cost_usd
        if llm_intent and llm_confidence >= settings.intent_handoff_threshold:
            source = 'llm'
            result = intent_mod.IntentResult(
                llm_intent, llm_confidence, scores=result.scores,
                matched=result.matched, slots=result.slots, frustration=result.frustration,
            )
            if llm_confidence < settings.intent_llm_threshold:
                handoff_level = max(handoff_level, 1)
        else:
            # The model could not rescue it either — that is a handoff, not a guess.
            source = 'fallback'
            handoff_level = max(handoff_level, 2)

    metrics.INTENT.labels(result.intent, source).inc()
    metrics.INTENT_CONFIDENCE.observe(result.confidence)
    if handoff_level:
        metrics.HANDOFFS.labels(str(handoff_level)).inc()

    streak = low_streak + 1 if result.confidence < settings.intent_llm_threshold else 0

    return StepOutcome.proceed(
        intent=result.intent,
        intent_label=result.label,
        confidence=result.confidence,
        intent_source=source,
        slots=result.slots,
        smalltalk=result.smalltalk,
        handoff_level=handoff_level,
        low_confidence_streak=streak,
        route_tokens_in=tokens_in,
        route_tokens_out=tokens_out,
        route_cache_hit=cache_hit,
        route_cost=cost,
    )


# --------------------------------------------------------------------------- #
# Step: retrieve
# --------------------------------------------------------------------------- #
@step(WORKFLOW_TYPE, 'retrieve')
async def _retrieve(ctx: StepContext) -> StepOutcome:
    from .. import metrics

    request: TurnRequest = ctx.services['request']
    settings: Settings = ctx.services['settings']
    embedder = ctx.services.get('embedder')

    if ctx.state.get('handoff_level', 0) >= 2 or ctx.state.get('smalltalk'):
        return StepOutcome.proceed(sources=[], context_blocks=[])

    started = time.perf_counter()
    hits = await knowledge.retrieve(
        ctx.db,
        request.message,
        tenant_id=request.tenant_id,
        store_id=request.store_id,
        top_k=settings.knowledge_top_k,
        min_score=settings.knowledge_min_score,
        embedder=embedder,
        rrf_k=settings.rrf_k,
    )
    metrics.KB_LATENCY.observe(time.perf_counter() - started)
    return StepOutcome.proceed(
        sources=[h.as_source() for h in hits],
        context_blocks=knowledge.build_context_blocks(hits),
    )


# --------------------------------------------------------------------------- #
# Step: plan_tools
# --------------------------------------------------------------------------- #
def plan_tools(state: dict, message: str, plan_id: str = '') -> plan_mod.Plan:
    """Map (intent, slots, phrasing) onto a plan the verifier can check.

    Deterministic and inspectable: the plan is built by plain code, not by the
    model. The LLM never chooses a tool, never orders steps and never picks a
    dependency. That is what keeps the write path auditable — and it is also
    what makes verifying the plan worth doing, because a plan produced by a
    model would move the trust boundary without adding a check.

    The refund branch is why this returns a graph instead of a list. It asks the
    rule table, proposes against the order, and then executes *that proposal* —
    the third step consumes the second's ``proposal_id``. As a flat list there
    was nothing to check: step 3's arguments were written by hand and could name
    any proposal in the system.
    """
    intent = state.get('intent', '')
    slots = state.get('slots') or {}
    order_id = slots.get('order_id', '')
    sku_id = slots.get('sku_id', '')
    # Tool arguments carry the *upstream* customer id, not the platform user id.
    # The business service does its own ownership check against this value, so
    # getting it wrong turns every read into a 403 rather than a wrong answer.
    customer_ref = state.get('business_customer_id') or state['user_id']

    steps: list[plan_mod.PlanStep] = []

    def add(tool: str, arguments: dict, inputs: dict | None = None,
            requires: tuple = ()) -> str:
        step_id = f'step-{len(steps) + 1}'
        steps.append(plan_mod.PlanStep(step_id, tool, arguments, inputs or {}, requires))
        return step_id

    if intent == intent_mod.Intent.PRODUCT.value and sku_id:
        add('get_sku', {'sku_id': sku_id})

    if intent == intent_mod.Intent.ORDER_QUERY.value and order_id:
        add('get_order', {'user_id': customer_ref, 'order_id': order_id})

    if intent == intent_mod.Intent.REFUND.value and order_id:
        # Step 1 is the rule table, and it is now a *step* rather than a
        # pre-gate. The verdict is three-valued, so what it carries into step 2
        # is a requirement rather than an argument: only a definite ``False``
        # stops the chain, and "could not tell" passes through. The old
        # pre-gate could express that rule but not make it checkable.
        eligibility = add('check_aftersale_eligibility',
                          {'user_id': customer_ref, 'order_id': order_id})
        proposal = add(
            'create_refund_proposal',
            {'user_id': customer_ref, 'order_id': order_id,
             'reason': (message or '用户申请退款')[:200]},
            requires=(plan_mod.Requirement(eligibility, 'eligible', False),),
        )
        if any(phrase in message for phrase in _CONFIRM_EXECUTE):
            # High risk: this step parks in ``waiting_approval``. It is still
            # part of the plan, so the whole money path — including the
            # proposal_id link — is verified before the first tool of the turn
            # runs, not after two of them already had side effects.
            add('execute_refund',
                {'user_id': customer_ref, 'order_id': order_id},
                inputs={'proposal_id': plan_mod.InputRef(proposal, 'proposal_id')})

    if intent == intent_mod.Intent.TECH_SUPPORT.value and any(p in message for p in _TICKET_HINTS):
        add('create_ticket', {'user_id': customer_ref, 'issue': message[:500]})

    return plan_mod.Plan(
        plan_id=plan_id or f'plan-{uuid.uuid4().hex[:12]}',
        steps=tuple(steps),
        intent=intent,
    )


def _workflow_input(request: TurnRequest) -> dict:
    """The durable snapshot a run carries across a park/resume cycle.

    ``business_customer_id`` has to survive the park: the approval path rebuilds
    a :class:`TurnRequest` from this dict, and losing the mapping there would
    silently downgrade the identity to the platform user id.
    """
    return {
        'message': request.message,
        'user_id': request.user_id,
        'business_customer_id': request.business_customer_id,
    }


async def _execute_plan(ctx: StepContext, request: TurnRequest,
                        plan: plan_mod.Plan) -> dict | None:
    """Run a verified plan, resolving references between steps as it goes.

    Returns the outcome of the last step that ran — that is what the answer gets
    composed from.

    Three behaviours are deliberate and each replaces a special case that used
    to live outside the plan:

    * **A violated ``requires`` stops the chain** and reports the *source*
      step's outcome, so "the rule table said no" becomes the answer rather than
      a refund request. The old code did this with a pre-gate that ran before
      the plan existed; now it is a property of step 2 that the verifier can
      see.
    * **A failed step does not stop the chain.** Its outputs are simply absent,
      and a requirement over an absent field is *not* a violation. That is the
      three-valued discipline the rules themselves use: an unreachable rule
      service is not a refusal, and refusing a legitimate return because a read
      timed out is the failure that looks like strictness.
    * **A step that parks ends the turn's execution** but not the plan's
      validity — the remaining steps were already checked before step 1 ran.
    """
    business: governance.BusinessClient = ctx.services['business']
    settings: Settings = ctx.services['settings']
    outcomes: dict[str, dict] = {}
    results: dict[str, dict] = {}
    last: dict | None = None

    for step in plan.steps:
        # Requirements before invocation: a blocked step must not be called.
        blocked_by = None
        for req in step.requires:
            source = results.get(req.step_id)
            present = bool(source) and req.field in source
            if req.violated(source.get(req.field) if source else None, present):
                blocked_by = req
                break
        if blocked_by is not None:
            source_outcome = outcomes.get(blocked_by.step_id) or {}
            source_result = results.get(blocked_by.step_id, {})
            log_event(logger, 'plan_step_blocked', step_id=step.step_id, tool=step.tool,
                      requirement=f'{blocked_by.step_id}.{blocked_by.field}',
                      rule_id=source_result.get('rule_id', ''),
                      reason=source_result.get('reason', ''))
            return {
                'tool': source_outcome.get('tool', ''),
                'risk': source_outcome.get('risk', ''),
                'status': 'blocked',
                'result': source_result,
                'approval_id': '',
                'invocation_id': source_outcome.get('invocation_id', ''),
                'error': '',
                'replayed': source_outcome.get('replayed', False),
                # Which edge stopped it. Without this the trail says "blocked"
                # but not by what, and the dependency graph is unreadable.
                'blocked_by': f'{blocked_by.step_id}.{blocked_by.field}',
            }

        arguments = dict(step.arguments)
        unresolved = ''
        for arg_name, ref in step.inputs.items():
            source = results.get(ref.step_id)
            if not source or ref.field not in source:
                unresolved = ref.describe()
                break
            arguments[arg_name] = source[ref.field]
        if unresolved:
            # The upstream step produced nothing (it failed, or was denied), so
            # this step's argument has no value. Invoking it anyway would pass
            # ``None`` and get refused by the argument contract — which reads in
            # the log like a malformed request rather than "the step it depends
            # on never ran". Skipping says what actually happened.
            log_event(logger, 'plan_step_skipped', level=logging.WARNING,
                      step_id=step.step_id, tool=step.tool, missing=unresolved)
            last = {'tool': step.tool, 'status': 'unresolved_input',
                    'reason': 'upstream_step_produced_no_output',
                    'detail': f'{unresolved} 未产生输出'}
            continue

        try:
            outcome = await governance.invoke(
                ctx.db, settings, business,
                tool_name=step.tool,
                arguments=arguments,
                tenant_id=request.tenant_id,
                store_id=request.store_id,
                conversation_id=ctx.state.get('conversation_id', ''),
                message_id=ctx.state.get('user_message_id', ''),
                user_id=request.user_id,
                actor_roles=request.actor_roles,
                idempotency_key=request.idempotency_key,
                trace_id=request.trace_id,
            )
        except governance.ToolDenied as exc:
            log_event(logger, 'plan_step_denied', level=logging.WARNING,
                      step_id=step.step_id, tool=step.tool, reason=exc.reason)
            last = {'tool': step.tool, 'status': 'denied', 'reason': exc.reason,
                    'detail': exc.detail[:200]}
            continue

        results[step.step_id] = dict(outcome.result or {})
        outcomes[step.step_id] = {
            'tool': outcome.tool, 'risk': outcome.risk,
            'invocation_id': outcome.invocation_id, 'replayed': outcome.replayed,
        }
        last = {
            'tool': outcome.tool,
            'risk': outcome.risk,
            'status': outcome.status,
            'result': outcome.result,
            'approval_id': outcome.approval_id,
            'invocation_id': outcome.invocation_id,
            'error': outcome.error,
            'replayed': outcome.replayed,
        }
        if outcome.waiting_approval:
            # Do not park yet: the interim answer still has to be composed and
            # persisted, otherwise the user gets an empty bubble while they wait.
            break

    return last


@step(WORKFLOW_TYPE, 'plan_tools')
async def _plan_tools(ctx: StepContext) -> StepOutcome:
    from .. import metrics

    request: TurnRequest = ctx.services['request']

    if ctx.state.get('handoff_level', 0) >= 2:
        return StepOutcome.proceed(tool=None)

    ctx.state['user_id'] = request.user_id
    ctx.state['business_customer_id'] = request.business_ref

    plan = plan_tools(ctx.state, request.message,
                      plan_id=f'plan-{request.trace_id[:16]}' if request.trace_id else '')

    # Verify *before* the first tool runs. A plan that fails any check must
    # produce no business calls at all — not a partially executed prefix, which
    # is the failure mode a per-step check cannot catch: governance validates
    # each call in isolation, so it would happily run step 1 and 2 and only
    # discover step 3 was incoherent after the side effects.
    started = time.perf_counter()
    verification = plan_mod.verify_plan(plan, governance.TOOL_REGISTRY)
    metrics.PLAN_VALIDATION_LATENCY.observe(time.perf_counter() - started)
    metrics.PLAN_STEPS.observe(len(plan.steps))
    metrics.PLAN_VALIDATIONS.labels('valid' if verification.ok else 'rejected').inc()

    ctx.state['plan'] = {
        'plan_id': plan.plan_id,
        'intent': plan.intent,
        'steps': [
            {'step_id': s.step_id, 'tool': s.tool,
             'depends_on': list(s.dependencies),
             'arguments': dict(s.arguments),
             'inputs': {k: v.describe() for k, v in s.inputs.items()}}
            for s in plan.steps
        ],
        'validation': {
            'ok': verification.ok,
            'reason': verification.reason,
            'detail': verification.detail,
            'checks': verification.ledger(),
        },
    }
    log_event(logger, 'plan_validation', plan_id=plan.plan_id, intent=plan.intent,
              steps=len(plan.steps),
              status='valid' if verification.ok else 'rejected',
              reason=verification.reason or '-')

    if not verification.ok:
        metrics.PLAN_REJECTIONS.labels(verification.reason).inc()
        log_event(logger, 'plan_rejected', level=logging.ERROR, plan_id=plan.plan_id,
                  reason=verification.reason, detail=verification.detail)
        # Fail closed. Nothing was called, so the answer is a safe refusal that
        # names the reason for the operator without leaking the plan itself.
        return StepOutcome.proceed(tool={
            'tool': '',
            'status': 'plan_rejected',
            'reason': verification.reason,
            'detail': verification.detail[:200],
        })

    tool_outcome = await _execute_plan(ctx, request, plan)
    return StepOutcome.proceed(tool=tool_outcome)


# --------------------------------------------------------------------------- #
# Step: compose
# --------------------------------------------------------------------------- #
def _fmt_yuan(amount: Any) -> str:
    """Format an amount as the business service states it: in yuan.

    ``get_order`` returns ``amount`` and ``get_sku`` returns ``price``, both
    already divided by 100. Reading ``amount_cents`` instead does not raise —
    ``int(None)`` is caught below — so the mistake surfaced as the literal text
    "金额 None" in the answer, and only on the path that runs when the model is
    unavailable or has just failed. The fallback is an em dash rather than the
    word "None", so a missing value never reads as a value.
    """
    try:
        return f'¥{float(amount):.2f}'
    except (TypeError, ValueError):
        return '—'


def deterministic_answer(state: dict) -> str:
    """Template answer used when no model is configured or the model failed."""
    intent = state.get('intent', '')
    sources = state.get('sources') or []
    tool = state.get('tool') or {}
    handoff_level = state.get('handoff_level', 0)

    if state.get('smalltalk'):
        return GREETING

    if handoff_level >= 2:
        return HANDOFF_MESSAGES[2 if handoff_level == 2 else 3]

    tool_status = tool.get('status')
    result = tool.get('result') or {}

    if tool_status == 'waiting_approval':
        return (
            f'这笔退款需要人工审批后才能执行。已生成审批单 {tool.get("approval_id", "")}，'
            '审批通过后系统会自动执行，你无需重复提交。'
        )

    if tool_status == 'plan_rejected':
        # The plan failed verification, so nothing was called — not even the
        # read steps. The user gets a plain "try again / ask a human" because
        # the reason is a *planner* defect, not something they can act on; the
        # reason code goes to the log and the trace for the operator.
        return (
            '这次的操作没有通过安全校验，系统没有执行任何操作。'
            '可以换一种说法再试一次，或者回复「转人工」由人工客服处理。'
        )

    if tool_status == 'blocked':
        # The rule table said no. The reason is quoted from the rule row rather
        # than paraphrased, so the user is told the actual policy that applies
        # to their product category instead of a generic refusal.
        return (
            f'这一单暂时不符合无理由退货条件：{result.get("reason") or "规则未通过"}'
            '如果情况特殊，可以回复「转人工」，由人工客服复核处理。'
        )

    if tool_status == 'rejected':
        return '这笔退款申请没有通过人工审核，订单未发生任何变更。如有疑问可以回复「转人工」进一步沟通。'

    if tool_status == 'denied':
        return f'这个操作当前不被允许（{tool.get("reason", "denied")}），已记录，稍后可由人工客服协助。'

    if tool_status == 'unresolved_input':
        # The step never ran because what it depends on never produced a value.
        # Saying "操作没有成功" would be true but useless; naming the missing
        # piece is what makes the turn diagnosable from the user's report.
        return (
            '这一步依赖的前序操作没有返回结果，所以没有执行，订单未发生任何变更。'
            '可以稍后重试，或者回复「转人工」由人工客服处理。'
        )

    if tool_status == 'failed':
        return '操作没有成功，我已经记录下来了。可以稍后重试，或者回复「转人工」由人工客服处理。'

    if tool.get('tool') == 'get_order' and result:
        status_map = {
            'shipped': '已发货', 'processing': '处理中', 'delivered': '已签收',
            'refund_processing': '退款处理中', 'cancelled': '已取消',
        }
        status = status_map.get(str(result.get('status')), str(result.get('status', '未知')))
        refundable = '可申请退款' if result.get('refund_available') else '当前不可退款'
        return f'订单 {result.get("order_id", "")} 当前状态：{status}，金额 {_fmt_yuan(result.get("amount"))}，{refundable}。'

    if tool.get('tool') == 'get_sku' and result:
        name = result.get('name', '')
        spec = result.get('spec', '')
        label = f'{name}（{spec}）' if spec else name
        if result.get('status') == 'off_shelf':
            return f'{label} 已经下架了，可以看看同类在售商品。'
        if not result.get('in_stock'):
            return f'{label} 当前无货，补货后可以在商品页下单。'
        return (
            f'{label} 当前有货，库存 {result.get("stock")} 件，'
            f'售价 {_fmt_yuan(result.get("price"))}。'
        )

    if tool.get('tool') == 'create_refund_proposal' and result:
        proposal_id = result.get('proposal_id') or result.get('id') or ''
        return (
            f'已为你提交退款申请 {proposal_id}。申请会先经过人工审核，'
            '审核通过后系统自动执行退款，到账时间以支付渠道为准。'
        )

    if tool.get('tool') == 'create_ticket' and result:
        return f'已创建工单 {result.get("ticket_id", "")}，客服会在 24 小时内跟进。'

    if tool.get('tool') == 'execute_refund' and result:
        proposal_id = result.get('proposal_id') or result.get('id') or ''
        return (
            f'退款已执行（申请单 {proposal_id}），订单状态更新为退款处理中。'
            '到账时间以支付渠道为准，通常 1-3 个工作日。'
        )

    if sources:
        top = sources[0]
        return f'{top.get("title", "")}：{_source_content(state, top)}'

    if handoff_level == 1:
        return HANDOFF_MESSAGES[1]

    if intent == intent_mod.Intent.REFUND.value:
        return '我可以帮你申请退款。请提供订单号（例如 ORD-1001），我先确认这笔订单是否在可退范围内。'
    if intent == intent_mod.Intent.ORDER_QUERY.value:
        return '请提供订单号（例如 ORD-1001），我帮你查询当前状态和物流进度。'
    if intent == intent_mod.Intent.TECH_SUPPORT.value:
        return '请描述一下具体现象（例如「App 打不开」「收不到验证码」），我按排查步骤帮你定位。'
    if intent == intent_mod.Intent.PRODUCT.value:
        return '请提供商品编号（例如 SKU-1001），我帮你查实时库存和价格。'

    return '抱歉，我在知识库里没有找到相关依据。你可以换个说法，或者回复「转人工」由人工客服跟进。'


def _source_content(state: dict, source: dict) -> str:
    blocks = state.get('context_blocks') or []
    if blocks:
        text = blocks[0]
        return text.split('：', 1)[-1] if '：' in text else text
    return ''


async def _stream_text(text: str, queue: asyncio.Queue | None, chunk: int = 12) -> float:
    """Push ``text`` to the SSE queue in small chunks. Returns TTFT in ms."""
    if queue is None or not text:
        return 0.0
    started = time.perf_counter()
    ttft = 0.0
    for index in range(0, len(text), chunk):
        await queue.put(('delta', text[index:index + chunk]))
        if ttft == 0.0:
            ttft = (time.perf_counter() - started) * 1000
    return ttft


@step(WORKFLOW_TYPE, 'compose')
async def _compose(ctx: StepContext) -> StepOutcome:
    from .. import metrics

    request: TurnRequest = ctx.services['request']
    settings: Settings = ctx.services['settings']
    llm: LLMClient = ctx.services['llm']
    queue: asyncio.Queue | None = ctx.services.get('stream_queue')

    state = ctx.state
    handoff_level = state.get('handoff_level', 0)
    tool = state.get('tool') or {}

    # A parked turn, and a rule-based refusal, both produce their message with
    # no model call at all. The refusal especially: it is a business fact read
    # out of the rule table, and letting the model phrase it would give the user
    # a turn in which to argue it into a different answer.
    if tool.get('status') in ('waiting_approval', 'blocked', 'plan_rejected',
                              'unresolved_input'):
        answer = deterministic_answer(state)
        ttft = await _stream_text(answer, queue)
        metrics.OUTCOMES.labels('answered').inc()
        return StepOutcome.proceed(answer=answer, ttft_ms=ttft, compose_source='deterministic')

    template = deterministic_answer(state)

    use_llm = (
        llm.available
        and handoff_level < 2
        and not state.get('smalltalk')
        and tool.get('status') not in ('denied', 'failed')
    )

    if not use_llm:
        ttft = await _stream_text(template, queue)
        metrics.OUTCOMES.labels('answered').inc()
        return StepOutcome.proceed(answer=template, ttft_ms=ttft, compose_source='deterministic')

    facts: list[str] = []
    if tool.get('tool') and tool.get('result'):
        facts.append(f'工具 {tool["tool"]} 返回：{json.dumps(tool["result"], ensure_ascii=False)[:600]}')
    context_blocks = list(state.get('context_blocks') or []) + facts

    extra = ''
    if handoff_level == 1:
        extra = '如果你不确定，请明确说明不确定，并建议用户回复「转人工」。'

    messages = llm.build_answer_messages(
        request.message, context_blocks,
        history=state.get('history') or [],
        extra_instruction=extra,
    )

    answer = ''
    ttft = 0.0
    tokens_in = tokens_out = 0
    cache_hit = 0
    cost = 0.0
    started = time.perf_counter()
    failed = False

    if queue is not None:
        async for kind, payload in llm.chat_stream(messages, purpose='answer'):
            if kind == 'delta':
                if ttft == 0.0:
                    ttft = (time.perf_counter() - started) * 1000
                answer += payload
                await queue.put(('delta', payload))
            elif kind == 'done':
                tokens_in = payload.tokens_in
                tokens_out = payload.tokens_out
                cache_hit = payload.cache_hit_tokens
                cost = payload.cost_usd
            else:
                failed = True
    else:
        result = await llm.chat(messages, purpose='answer', temperature=0.3)
        if result.ok:
            answer = result.text
            tokens_in, tokens_out = result.tokens_in, result.tokens_out
            cache_hit, cost = result.cache_hit_tokens, result.cost_usd
            ttft = result.latency_ms
        else:
            failed = True

    if failed or not answer.strip():
        # Degrade to the template rather than returning an empty bubble.
        answer = template
        if queue is not None:
            await queue.put(('replace', answer))
        metrics.OUTCOMES.labels('failed').inc()
        # The failed call may still have burned tokens (a rejected request does
        # not, a truncated one does), so whatever was metered is reported rather
        # than zeroed — otherwise the console under-reports the cost of failures.
        return StepOutcome.proceed(answer=answer, ttft_ms=ttft, compose_source='degraded',
                                   compose_tokens_in=tokens_in, compose_tokens_out=tokens_out,
                                   compose_cache_hit=cache_hit,
                                   compose_cost=cost, degraded=True)

    metrics.OUTCOMES.labels('answered').inc()
    return StepOutcome.proceed(
        answer=answer, ttft_ms=ttft, compose_source='llm',
        compose_tokens_in=tokens_in, compose_tokens_out=tokens_out,
        compose_cache_hit=cache_hit, compose_cost=cost,
    )


# --------------------------------------------------------------------------- #
# Step: persist
# --------------------------------------------------------------------------- #
@step(WORKFLOW_TYPE, 'persist')
async def _persist(ctx: StepContext) -> StepOutcome:
    from .. import metrics
    from . import tenant as tenant_mod

    request: TurnRequest = ctx.services['request']
    state = ctx.state
    conversation = ctx.db.get(conv.Conversation, state['conversation_id'])

    answer = state.get('answer') or deterministic_answer(state)
    tokens_in = int(state.get('route_tokens_in', 0)) + int(state.get('compose_tokens_in', 0))
    tokens_out = int(state.get('route_tokens_out', 0)) + int(state.get('compose_tokens_out', 0))
    cache_hit = int(state.get('route_cache_hit', 0)) + int(state.get('compose_cache_hit', 0))
    cost = float(state.get('route_cost', 0.0)) + float(state.get('compose_cost', 0.0))
    started = float(ctx.services.get('started') or 0.0)
    latency_ms = round((time.perf_counter() - started) * 1000, 2) if started else 0.0

    message = conv.append_message(
        ctx.db,
        conversation=conversation,
        role='assistant',
        content=answer,
        intent=state.get('intent', ''),
        confidence=float(state.get('confidence', 0.0)),
        trace_id=request.trace_id,
        latency_ms=latency_ms,
        ttft_ms=float(state.get('ttft_ms', 0.0)),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        sources=state.get('sources') or [],
        meta={
            'intent_source': state.get('intent_source', ''),
            'compose_source': state.get('compose_source', ''),
            'tool': state.get('tool'),
            'handoff_level': state.get('handoff_level', 0),
            'degraded': state.get('degraded', False),
            # The plan and its verification ledger, stored with the message so
            # "why was this allowed to run" is answerable from the conversation
            # record alone. Keeping it only in the workflow state would mean the
            # answer disappears as soon as the run is collected, and the trace
            # would show the tool call without the reasoning that permitted it.
            'plan': state.get('plan'),
            # A subset of ``tokens_in``, not an extra bucket: cache-hit prompt
            # tokens are billed ~50x cheaper, so storing them separately is what
            # lets the console show whether the cache is doing anything.
            'cache_hit_tokens': cache_hit,
        },
    )

    handoff_level = int(state.get('handoff_level', 0))
    if handoff_level:
        conv.mark_handoff(ctx.db, conversation, handoff_level)

    if (state.get('tool') or {}).get('status') == 'waiting_approval':
        conversation.status = 'waiting_approval'
        ctx.db.commit()

    try:
        tenant_mod.consume_quota(ctx.db, request.tenant_id, messages=1,
                                 tokens=tokens_in + tokens_out, cost_usd=cost)
    except tenant_mod.QuotaExceeded as exc:
        log_event(logger, 'quota_exceeded_after_turn', level=logging.WARNING,
                  tenant_id=request.tenant_id, kind=exc.kind)

    metrics.ANSWER_LATENCY.observe(max(latency_ms, 0.0) / 1000.0)
    if float(state.get('ttft_ms', 0.0)) > 0:
        metrics.TTFT.observe(float(state['ttft_ms']) / 1000.0)

    return StepOutcome.proceed(
        assistant_message_id=message.id,
        final_answer=answer,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
    )


# --------------------------------------------------------------------------- #
# Step: await_approval (park) + notify (resume)
# --------------------------------------------------------------------------- #
@step(WORKFLOW_TYPE, 'await_approval')
async def _await_approval(ctx: StepContext) -> StepOutcome:
    tool = ctx.state.get('tool') or {}
    if tool.get('status') != 'waiting_approval':
        return StepOutcome.proceed()

    # Everything the user needed to see is already stored. Now the run parks
    # until a reviewer decides; the decision signals and resumes it.
    return StepOutcome.wait(
        'waiting_approval',
        approval_id=tool.get('approval_id', ''),
        invocation_id=tool.get('invocation_id', ''),
    )


@step(WORKFLOW_TYPE, 'notify')
async def _notify(ctx: StepContext) -> StepOutcome:
    """Post the outcome of a human decision as a follow-up assistant message."""
    from .. import metrics

    decision = ctx.state.get('approval_decision')
    if not decision:
        return StepOutcome.done()

    request: TurnRequest = ctx.services['request']
    state = ctx.state
    conversation = ctx.db.get(conv.Conversation, state['conversation_id'])
    if conversation is None:
        return StepOutcome.done()

    answer = deterministic_answer(state)
    message = conv.append_message(
        ctx.db,
        conversation=conversation,
        role='assistant',
        content=answer,
        intent=state.get('intent', ''),
        confidence=float(state.get('confidence', 0.0)),
        trace_id=request.trace_id,
        sources=state.get('sources') or [],
        meta={
            'approval_decision': decision,
            'approval_reviewer': state.get('approval_reviewer', ''),
            'tool': state.get('tool'),
            'follow_up': True,
        },
    )
    metrics.OUTCOMES.labels('answered').inc()
    return StepOutcome.done(final_answer=answer, assistant_message_id=message.id,
                            notified=True)


# --------------------------------------------------------------------------- #
# Engine facade
# --------------------------------------------------------------------------- #
class ConversationEngine:
    """Entry point used by the API layer."""

    def __init__(self, settings: Settings, llm: LLMClient,
                 business: governance.BusinessClient, embedder=None):
        self.settings = settings
        self.llm = llm
        self.business = business
        self.embedder = embedder

    def _services(self, request: TurnRequest, stream_queue=None, started: float = 0.0) -> dict:
        return {
            'settings': self.settings,
            'llm': self.llm,
            'business': self.business,
            'embedder': self.embedder,
            'request': request,
            'stream_queue': stream_queue,
            'started': started,
        }

    @staticmethod
    def _to_result(run, request: TurnRequest, started: float, ttft_ms: float = 0.0) -> TurnResult:
        state = run.state or {}
        result = run.result or {}
        tool = state.get('tool') or result.get('tool') or {}
        intent_value = state.get('intent', '')
        return TurnResult(
            conversation_id=state.get('conversation_id', request.conversation_id),
            message_id=result.get('assistant_message_id', ''),
            answer=result.get('final_answer') or state.get('answer', ''),
            intent=intent_value,
            intent_label=state.get('intent_label') or intent_mod.INTENT_LABELS.get(intent_value, intent_value),
            confidence=float(state.get('confidence', 0.0)),
            intent_source=state.get('intent_source', ''),
            sources=state.get('sources') or [],
            suggestions=intent_mod.INTENT_SUGGESTIONS.get(intent_value, []),
            handoff_level=int(state.get('handoff_level', 0)),
            tool=tool or None,
            approval_id=tool.get('approval_id', ''),
            workflow_run_id=run.id,
            workflow_status=run.status,
            plan=state.get('plan') or None,
            tokens_in=int(result.get('tokens_in', 0)),
            tokens_out=int(result.get('tokens_out', 0)),
            cost_usd=float(result.get('cost_usd', 0.0)),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            ttft_ms=float(state.get('ttft_ms', ttft_ms)),
            degraded=bool(state.get('degraded', False)),
            error=run.error,
        )

    async def handle_turn(self, db: Session, request: TurnRequest) -> TurnResult:
        started = time.perf_counter()
        engine = WorkflowEngine(db, self._services(request, started=started))
        run = engine.start(
            WORKFLOW_TYPE,
            tenant_id=request.tenant_id,
            store_id=request.store_id,
            conversation_id=request.conversation_id,
            workflow_input=_workflow_input(request),
            idempotency_key=request.idempotency_key,
            trace_id=request.trace_id,
        )
        run = await engine.run(run)
        return self._to_result(run, request, started)

    async def resume(self, db: Session, run) -> TurnResult:
        """Continue a run that was parked on human approval.

        The approval decision has already been written to ``run.state['tool']``
        by the caller, so the resumed ``compose`` step simply renders the
        outcome — it cannot re-issue the tool call.
        """
        started = time.perf_counter()
        workflow_input = run.input or {}
        request = TurnRequest(
            tenant_id=run.tenant_id,
            store_id=run.store_id or '',
            user_id=workflow_input.get('user_id', ''),
            business_customer_id=workflow_input.get('business_customer_id', ''),
            message=workflow_input.get('message', ''),
            conversation_id=run.conversation_id or '',
            trace_id=run.trace_id or '',
        )
        engine = WorkflowEngine(db, self._services(request, started=started))
        # Un-park the run: the workflow engine refuses to advance a waiting run
        # that has received no external signal, by design.
        if not (run.state or {}).get('_signals'):
            engine.signal(run.id, 'approval_decided',
                          {'decision': (run.state or {}).get('approval_decision', '')})
        run = await engine.run(run)
        return self._to_result(run, request, started)

    async def stream_turn(self, db: Session, request: TurnRequest):
        """Yield ``(event, payload)`` tuples for the SSE endpoint."""
        started = time.perf_counter()
        queue: asyncio.Queue = asyncio.Queue()
        engine = WorkflowEngine(db, self._services(request, stream_queue=queue, started=started))
        run = engine.start(
            WORKFLOW_TYPE,
            tenant_id=request.tenant_id,
            store_id=request.store_id,
            conversation_id=request.conversation_id,
            workflow_input=_workflow_input(request),
            idempotency_key=request.idempotency_key,
            trace_id=request.trace_id,
        )

        async def _drive() -> None:
            """Run the workflow; always unblock the consumer, even on failure."""
            try:
                await engine.run(run)
            except Exception as exc:  # noqa: BLE001 - surfaced to the client below
                log_event(logger, 'workflow_run_failed', level=logging.ERROR,
                          run_id=run.id, error=type(exc).__name__, message=str(exc)[:300])
                run.error = f'{type(exc).__name__}: {exc}'[:500]
            finally:
                queue.put_nowait(STREAM_END)

        task = asyncio.create_task(_drive())

        emitted_any = False
        while True:
            item = await queue.get()
            if item is STREAM_END:
                break
            kind, payload = item
            if kind == 'delta':
                emitted_any = True
                yield 'delta', payload
            elif kind == 'replace':
                # The model failed mid-stream: replace the bubble wholesale.
                emitted_any = False
                yield 'replace', payload

        await task
        result = self._to_result(run, request, started)
        if not emitted_any and result.answer:
            yield 'replace', result.answer
        yield 'final', result
