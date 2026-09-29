# -*- coding: utf-8 -*-
"""Gateway / platform settings.

The service used to be a thin proxy in front of Dify. It is now the
deterministic trunk described in the target architecture, with Dify kept as an
*optional* engine (``ENGINE_MODE=dify``) so existing deployments and the DSL
templates in ``dify/`` keep working.
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    # -- runtime ---------------------------------------------------------- #
    environment: str = 'development'
    log_level: str = 'INFO'
    #: Reported by ``/api/healthz`` and ``/api/readyz``. Must equal the repo
    #: root ``VERSION`` file, the changelog's newest heading, both published
    #: OpenAPI documents, and the business service's own version — see
    #: ``tests/test_version_consistency.py``, which fails on any drift.
    version: str = '2.6.0'

    # ``builtin`` runs the deterministic trunk in-process (default).
    # ``dify`` keeps the legacy proxy behaviour and requires DIFY_API_KEY.
    engine_mode: Literal['builtin', 'dify'] = 'builtin'

    # -- persistence ------------------------------------------------------ #
    database_url: str = 'sqlite:///./data/gateway.db'
    auto_create_schema: bool = True
    auto_seed: bool = True

    # Connection pool. Sizing is not cosmetic here: ``/api/chat/stream`` holds a
    # request-scoped session for the *entire* SSE response, so the number of
    # concurrent streams a single worker can serve is bounded by the pool.
    # The default 5+10 was inherited, not chosen — the arithmetic is written
    # down in docs/OPERATIONS.md so the next person can re-derive it.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Recycle before the server / an idle NAT would drop the socket silently.
    # ``pool_pre_ping`` also catches dead connections, but it costs a round trip
    # on every checkout; recycling makes that the exception rather than the rule.
    db_pool_recycle_seconds: int = 1800
    db_pool_timeout_seconds: float = 30.0
    # PostgreSQL guardrail: a transaction left open by a bug (or a client that
    # vanished mid-stream) holds a snapshot and blocks vacuum from reclaiming
    # anything newer. Observed in practice — see docs/OPERATIONS.md. Letting the
    # server kill it converts an unbounded leak into a bounded, logged error.
    db_idle_tx_timeout_ms: int = 300_000

    # -- auth ------------------------------------------------------------- #
    auth_mode: Literal['none', 'dev', 'jwt'] = 'dev'
    jwt_secret: str = 'change-me'
    jwt_issuer: str = 'dify-support-platform'
    access_token_ttl_minutes: int = 720
    # Dev-mode fallback tenant so the offline demo works without a login.
    dev_tenant_id: str = 'tenant-demo'
    dev_store_id: str = 'store-demo'

    # -- quota / rate limit ----------------------------------------------- #
    rate_limit_per_minute: int = 60
    tenant_daily_message_quota: int = 5000
    redis_url: str = 'redis://redis:6379/0'

    # -- intent routing --------------------------------------------------- #
    # Below this confidence the deterministic scorer hands over to the LLM.
    intent_llm_threshold: float = 0.55
    # Below this confidence even the LLM is not trusted -> suggest handoff.
    intent_handoff_threshold: float = 0.35
    # Auto-escalate to a human after this many consecutive low-confidence turns.
    handoff_frustration_turns: int = 3

    # -- knowledge base --------------------------------------------------- #
    knowledge_top_k: int = 4
    knowledge_min_score: float = 0.8
    # Phase 2: semantic recall via pgvector. Requires an embedding endpoint.
    embedding_enabled: bool = False
    embedding_base_url: str = ''
    embedding_api_key: str = ''
    embedding_model: str = 'text-embedding-3-small'
    embedding_dim: int = 1536
    rrf_k: int = 60

    # -- LLM (OpenAI-compatible) ------------------------------------------ #
    llm_enabled: bool = False
    #: DeepSeek's documented OpenAI-compatible base. The client appends
    #: ``/chat/completions``, so this must NOT carry a trailing ``/v1`` —
    #: the current docs list ``https://api.deepseek.com`` and the ``/v1`` form
    #: is only a legacy alias.
    llm_base_url: str = 'https://api.deepseek.com'
    llm_api_key: str = ''
    #: ``deepseek-flash`` (DeepSeek-V4.1-Flash) is the current documented name.
    #: ``deepseek-chat`` still resolves to the same model, but it is an
    #: undocumented alias that can be retired without notice, so it is not the
    #: default here.
    llm_model: str = 'deepseek-flash'
    llm_timeout_seconds: float = 30.0
    llm_max_tokens: int = 800
    #: DeepSeek V4 defaults to *thinking* mode, which spends the completion
    #: budget on ``reasoning_content`` before emitting any answer. Both nodes
    #: this platform uses are short and structured (a one-label classification,
    #: a grounded rewrite), so thinking buys latency and tokens and nothing
    #: else — and at ``max_tokens=80`` it can consume the entire budget and
    #: return an empty ``content``, which would silently break intent fallback.
    #: ``auto`` sends no thinking key at all, for non-DeepSeek endpoints that
    #: would reject an unknown body field.
    llm_thinking: Literal['auto', 'enabled', 'disabled'] = 'disabled'
    #: Peak-hour list prices, USD per 1K tokens — the conservative choice, since
    #: DeepSeek charges half of these off-peak (01:00-04:00 and 06:00-10:00 UTC,
    #: Mon-Fri). Cache hits are ~50x cheaper on input and are priced apart;
    #: billing every prompt token at the miss rate would overstate cost badly
    #: once the system prompt is warm.
    llm_price_in_per_1k: float = 0.0003
    llm_price_in_cached_per_1k: float = 0.000006
    llm_price_out_per_1k: float = 0.0012

    # -- governance ------------------------------------------------------- #
    # High-risk tools always go through waiting_approval before execution.
    approval_required_risk: Literal['low', 'medium', 'high'] = 'high'
    approval_ttl_hours: int = 72
    tool_call_timeout_seconds: float = 20.0

    # -- upstream services ------------------------------------------------ #
    business_service_url: str = 'http://business-service:9000'
    tool_api_key: str = 'dev-tool-key'
    # The reviewer credential is *separate* from the tool credential on purpose.
    # The agent-facing tool key can raise a refund request but can never approve
    # one; only this key can, and it is used exactly once per approval — when the
    # gateway relays the human decision back to the order system's own review
    # state machine. Without it an approval would be recorded here and never
    # reach the business service, so the refund would silently never execute.
    reviewer_api_key: str = ''
    reviewer_id: str = 'human-reviewer-01'

    # -- legacy Dify proxy ------------------------------------------------- #
    dify_base_url: str = 'https://api.dify.ai/v1'
    dify_api_key: str = ''
    dify_timeout_seconds: float = 90.0
    dify_mock: bool = False

    cors_origins: str = 'http://localhost:8080,http://localhost:8000'


@lru_cache
def get_settings() -> Settings:
    return Settings()


_PLACEHOLDER_PREFIXES = ('REPLACE', 'change-this', 'change-me', 'changeme')
_PLACEHOLDER_EXACT = {
    '',
    'change-me',
    'change-this-to-a-long-random-secret',
    'REPLACE_WITH_NEW_APP_KEY',
    'dev-tool-key',
    'dev-reviewer-key',
}


def is_placeholder_secret(value: str) -> bool:
    """True when a secret is empty or still one of the repo's known placeholders."""
    value = (value or '').strip()
    return value in _PLACEHOLDER_EXACT or value.startswith(_PLACEHOLDER_PREFIXES)


def validate_settings(s: Settings) -> None:
    """Fail fast on an obviously unsafe production configuration."""
    if s.environment.lower() != 'production':
        return
    problems = []
    if is_placeholder_secret(s.jwt_secret):
        problems.append('JWT_SECRET must be a strong random secret (>=32 chars)')
    if len(s.jwt_secret or '') < 32:
        problems.append('JWT_SECRET must be at least 32 characters')
    if s.auth_mode != 'jwt':
        problems.append('AUTH_MODE must be jwt in production (dev/none only for local demos)')
    if s.engine_mode == 'dify' and is_placeholder_secret(s.dify_api_key):
        problems.append('DIFY_API_KEY must be a real key of a PUBLISHED Dify app (placeholders refused)')
    if s.engine_mode == 'builtin':
        if s.database_url.startswith('sqlite'):
            problems.append('DATABASE_URL must be PostgreSQL in production, never sqlite')
        if is_placeholder_secret(s.tool_api_key):
            problems.append('TOOL_API_KEY must be a strong non-placeholder secret')
        # Not a placeholder check on a cosmetic value: without this credential
        # the gateway cannot relay a human approval to the business service, so
        # every high-risk operation would be approved here and never executed.
        if not s.reviewer_api_key:
            problems.append('REVIEWER_API_KEY is required in production '
                            '(approvals cannot reach the business service without it)')
    if s.llm_enabled and is_placeholder_secret(s.llm_api_key):
        problems.append('LLM_API_KEY must be a real key when LLM_ENABLED=1')
    if s.embedding_enabled and not s.embedding_base_url:
        problems.append('EMBEDDING_BASE_URL is required when EMBEDDING_ENABLED=1')
    if problems:
        raise RuntimeError('Unsafe production configuration: ' + '; '.join(problems))
