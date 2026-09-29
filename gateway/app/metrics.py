# -*- coding: utf-8 -*-
"""Prometheus metrics for the support platform.

Label cardinality is deliberately bounded: nothing here is labelled by
``tenant_id`` or ``user_id``, because those are unbounded in a real
deployment and would blow up the scrape payload. Per-tenant numbers are
queried from PostgreSQL instead (see ``routers/admin.py``).
"""
from prometheus_client import Counter, Gauge, Histogram

# -- transport (kept from v1.7 so existing dashboards keep working) --------- #
REQUESTS = Counter('gateway_requests_total', 'Gateway requests', ['path', 'status'])
DIFY_CALLS = Counter('gateway_dify_calls_total', 'Dify calls', ['mode', 'status'])
DIFY_LATENCY = Histogram('gateway_dify_latency_seconds', 'Dify call latency', ['mode'])

# -- conversation quality --------------------------------------------------- #
# TTFT: time from accepting the request to the first streamed token. This is
# the number users actually feel, so it gets its own histogram.
TTFT = Histogram(
    'gateway_ttft_seconds',
    'Time to first token',
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)
ANSWER_LATENCY = Histogram(
    'gateway_answer_latency_seconds',
    'Full answer latency',
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)
# completion rate = answered / (answered + failed). Both sides are counted.
OUTCOMES = Counter(
    'gateway_chat_outcomes_total',
    'Chat outcomes',
    ['outcome'],  # answered | failed | refused | empty
)
HANDOFFS = Counter(
    'gateway_handoffs_total',
    'Human handoffs',
    ['level'],  # 1=suggested 2=auto 3=explicit
)
CONVERSATIONS = Gauge(
    'gateway_conversations',
    'Conversations by status',
    ['status'],
)

# -- intent routing --------------------------------------------------------- #
INTENT = Counter(
    'gateway_intent_total',
    'Intent decisions',
    ['intent', 'source'],  # source: deterministic | llm | fallback | handoff
)
INTENT_CONFIDENCE = Histogram(
    'gateway_intent_confidence',
    'Confidence of the winning intent',
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# -- knowledge base --------------------------------------------------------- #
KB_HITS = Counter(
    'gateway_knowledge_hits_total',
    'Knowledge retrievals',
    ['outcome'],  # hit | miss
)
KB_LATENCY = Histogram('gateway_knowledge_latency_seconds', 'Retrieval latency')

# -- LLM cost --------------------------------------------------------------- #
LLM_CALLS = Counter('gateway_llm_calls_total', 'LLM calls', ['purpose', 'status'])
LLM_TOKENS = Counter(
    'gateway_llm_tokens_total',
    'LLM tokens',
    # ``in_cached`` is a *subset* of ``in``, not a third direction: cache hits are
    # prompt tokens too, they are just billed ~50x cheaper. Splitting them out is
    # what makes the cache actually observable — without it the cost panel shows
    # a number with no explanation for why it moved.
    ['direction'],  # in | out | in_cached
)
LLM_COST = Counter('gateway_llm_cost_usd_total', 'Estimated LLM cost in USD')
LLM_LATENCY = Histogram('gateway_llm_latency_seconds', 'LLM latency', ['purpose'])

# -- governance ------------------------------------------------------------- #
TOOL_CALLS = Counter(
    'gateway_tool_calls_total',
    'Governed tool calls',
    ['tool', 'risk', 'status'],  # status: executed | failed | waiting_approval | rejected | denied
)
APPROVALS = Counter('gateway_approvals_total', 'Approval decisions', ['decision'])
STATE_MACHINE_VIOLATIONS = Counter(
    'gateway_state_machine_violations_total',
    'Rejected illegal state transitions',
    ['entity', 'from_status', 'to_status'],
)
IDEMPOTENT_REPLAYS = Counter(
    'gateway_idempotent_replays_total',
    'Requests served from the idempotency store',
    ['scope'],
)

# -- plan verification ------------------------------------------------------ #
# "Was this turn's tool sequence legal" is a different question from "did the
# tool call succeed", and it has to be answerable without reading logs: a
# planner regression shows up here as a rejection *reason*, whereas in the
# governance counters it would just look like a tool that never ran.
PLAN_VALIDATIONS = Counter(
    'gateway_plan_validations_total',
    'Plans verified before execution',
    ['status'],  # valid | rejected
)
PLAN_REJECTIONS = Counter(
    'gateway_plan_rejections_total',
    'Plans refused before any tool ran, by reason',
    ['reason'],  # see gateway.app.core.plan.REASONS
)
PLAN_STEPS = Histogram(
    'gateway_plan_steps',
    'Steps per verified plan',
    buckets=(0, 1, 2, 3, 4, 6, 8),
)
PLAN_VALIDATION_LATENCY = Histogram(
    'gateway_plan_validation_duration_seconds',
    'Time spent verifying a plan',
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1),
)

# -- tenancy ---------------------------------------------------------------- #
QUOTA_REJECTIONS = Counter(
    'gateway_quota_rejections_total',
    'Requests rejected by tenant quota or rate limit',
    ['kind'],  # rate | quota
)
AUTH_FAILURES = Counter('gateway_auth_failures_total', 'Authentication failures', ['reason'])
