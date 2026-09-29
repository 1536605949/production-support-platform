# -*- coding: utf-8 -*-
"""Platform persistence schema.

Design rules that every table below follows:

* **Tenant first.** Every business row carries ``tenant_id`` so a query can be
  scoped without a join. ``store_id`` adds the second isolation level required
  by multi-store merchants.
* **State machines are explicit.** ``tool_invocations``, ``approvals`` and
  ``workflow_runs`` store their status as a string column with a documented
  transition table (see ``core/governance.py`` and ``core/workflow.py``). The
  database is the source of truth; the API only ever *reads* the status.
* **Idempotency is a table, not a convention.** ``idempotency_keys`` makes a
  retried write return the original response instead of executing twice.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, JSONType


def now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #
class Tenant(Base, TimestampMixin):
    __tablename__ = 'tenants'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    plan: Mapped[str] = mapped_column(String(32), default='standard')
    status: Mapped[str] = mapped_column(String(32), default='active')
    daily_message_quota: Mapped[int] = mapped_column(Integer, default=5000)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)


class Store(Base, TimestampMixin):
    """Second isolation level: one tenant can operate several storefronts."""

    __tablename__ = 'stores'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey('tenants.id'), index=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default='active')


class User(Base, TimestampMixin):
    __tablename__ = 'users'
    __table_args__ = (UniqueConstraint('tenant_id', 'email', name='uq_users_tenant_email'),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey('tenants.id'), index=True)
    store_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    email: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), default='')
    display_name: Mapped[str] = mapped_column(String(128), default='')
    # customer | agent | reviewer | admin
    role: Mapped[str] = mapped_column(String(32), default='customer')
    status: Mapped[str] = mapped_column(String(32), default='active')
    # The customer id this account maps to in the *upstream order system*. The
    # two systems have separate identity spaces and their ids are not
    # interchangeable, so the mapping is stored explicitly rather than assumed.
    # Empty means "same id as ``users.id``", which is what the offline demo uses.
    business_customer_id: Mapped[str] = mapped_column(String(64), default='')


# --------------------------------------------------------------------------- #
# Conversation persistence
# --------------------------------------------------------------------------- #
class Conversation(Base, TimestampMixin):
    __tablename__ = 'conversations'
    __table_args__ = (
        Index('ix_conversations_scope', 'tenant_id', 'user_id', 'last_message_at'),
        # ``ix_conversations_scope`` leads with tenant_id, so the admin console's
        # ``WHERE tenant_id = ? AND created_at >= ?`` can seek on the tenant but
        # must then filter created_at as a residual — reading every conversation
        # the tenant has ever had. This index matches the predicate the window
        # query actually uses, so the range becomes a seek instead of a scan.
        Index('ix_conversations_tenant_created', 'tenant_id', 'created_at'),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey('tenants.id'), index=True)
    store_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255), default='')
    # active | waiting_human | closed
    status: Mapped[str] = mapped_column(String(32), default='active')
    channel: Mapped[str] = mapped_column(String(32), default='web')
    last_intent: Mapped[str] = mapped_column(String(32), default='')
    last_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    handoff_level: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (
        UniqueConstraint('conversation_id', 'seq', name='uq_messages_conv_seq'),
        Index('ix_messages_scope', 'tenant_id', 'conversation_id', 'seq'),
        # Same reasoning as conversations: the analytics window filters on
        # (tenant_id, created_at), and no existing index covers the second
        # column. Messages is the table that grows fastest, so this is the one
        # that matters most.
        Index('ix_messages_tenant_created', 'tenant_id', 'created_at'),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # user | assistant | system | tool
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default='')
    intent: Mapped[str] = mapped_column(String(32), default='')
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    trace_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    # observability
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ttft_ms: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    sources: Mapped[list] = mapped_column(JSONType, default=list)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #
class KnowledgeDocument(Base, TimestampMixin):
    __tablename__ = 'knowledge_documents'
    __table_args__ = (Index('ix_knowledge_scope', 'tenant_id', 'store_id', 'enabled'),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # '' means "global, visible to every tenant" (platform-authored FAQ).
    tenant_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    store_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    category: Mapped[str] = mapped_column(String(64), default='general', index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(Text, default='')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Phase 2: semantic recall. Stored as JSON so SQLite tests keep working; the
    # PostgreSQL deployment migrates it to a real pgvector column.
    embedding: Mapped[list] = mapped_column(JSONType, default=list)


# --------------------------------------------------------------------------- #
# Governance: tool calls, risk, approval, idempotency
# --------------------------------------------------------------------------- #
class ToolInvocation(Base, TimestampMixin):
    """One attempt by the agent to use a governed tool.

    Status machine::

        proposed ──(risk < threshold)──> executed | failed
            └────(risk >= threshold)───> waiting_approval ──> approved ──> executed
                                                          └─> rejected
    """

    __tablename__ = 'tool_invocations'
    __table_args__ = (
        UniqueConstraint('idempotency_key', name='uq_tool_invocations_idem'),
        Index('ix_tool_invocations_scope', 'tenant_id', 'status'),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    store_id: Mapped[str] = mapped_column(String(64), default='')
    conversation_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    message_id: Mapped[str] = mapped_column(String(64), default='')
    user_id: Mapped[str] = mapped_column(String(64), default='')
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default='low')
    arguments: Mapped[dict] = mapped_column(JSONType, default=dict)
    # proposed | waiting_approval | approved | rejected | executed | failed
    status: Mapped[str] = mapped_column(String(32), default='proposed', index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default='')
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    trace_id: Mapped[str] = mapped_column(String(64), default='')


class Approval(Base, TimestampMixin):
    __tablename__ = 'approvals'
    __table_args__ = (Index('ix_approvals_scope', 'tenant_id', 'status'),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    store_id: Mapped[str] = mapped_column(String(64), default='')
    invocation_id: Mapped[str] = mapped_column(ForeignKey('tool_invocations.id'), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), default='')
    tool_name: Mapped[str] = mapped_column(String(64), default='')
    risk_level: Mapped[str] = mapped_column(String(16), default='high')
    reason: Mapped[str] = mapped_column(Text, default='')
    # pending | approved | rejected | expired
    status: Mapped[str] = mapped_column(String(32), default='pending', index=True)
    reviewer_id: Mapped[str] = mapped_column(String(64), default='')
    comment: Mapped[str] = mapped_column(Text, default='')
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(Base):
    """Replay store for write endpoints.

    A retry with the same key returns the recorded response instead of running
    the side effect twice.
    """

    __tablename__ = 'idempotency_keys'
    __table_args__ = (UniqueConstraint('tenant_id', 'scope', 'key', name='uq_idem_scope_key'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64), default='')
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    response: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# --------------------------------------------------------------------------- #
# Persistent workflow shell (Temporal-aligned interface)
# --------------------------------------------------------------------------- #
class WorkflowRun(Base, TimestampMixin):
    """A durable, resumable execution of a named workflow.

    The interface mirrors the subset of Temporal's API the trunk needs
    (``start`` / ``signal`` / ``query`` / ``get_result``) so that swapping the
    local PostgreSQL implementation for a real Temporal server later does not
    change any call site.
    """

    __tablename__ = 'workflow_runs'
    __table_args__ = (Index('ix_workflow_runs_scope', 'tenant_id', 'status'),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    store_id: Mapped[str] = mapped_column(String(64), default='')
    conversation_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    workflow_type: Mapped[str] = mapped_column(String(64))
    # running | waiting | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default='running', index=True)
    current_step: Mapped[str] = mapped_column(String(64), default='')
    input: Mapped[dict] = mapped_column(JSONType, default=dict)
    state: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    idempotency_key: Mapped[str] = mapped_column(String(128), default='', index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default='')


class WorkflowCheckpoint(Base):
    __tablename__ = 'workflow_checkpoints'
    __table_args__ = (UniqueConstraint('run_id', 'step_index', name='uq_checkpoint_run_step'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('workflow_runs.id'), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    step_name: Mapped[str] = mapped_column(String(64))
    state: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# --------------------------------------------------------------------------- #
# Quota
# --------------------------------------------------------------------------- #
# NOTE: there is deliberately no ``AuditEvent`` model here.
#
# Both services share one PostgreSQL database, and the business service already
# owns a table called ``audit_events`` with a different shape. Declaring a second
# ``audit_events`` in this module did not create a second table — ``create_all``
# skips a name that already exists — it just produced a model whose INSERTs would
# fail on missing columns. It was never used, so the only effect was a landmine.
#
# The platform's audit trail lives in ``tool_invocations`` (who called which
# governed tool, with what arguments, under which trace) and ``workflow_runs``.
# ``tests/test_schema_namespaces.py`` asserts the two services' table names stay
# disjoint so this cannot come back unnoticed.
class QuotaUsage(Base):
    __tablename__ = 'quota_usage'
    __table_args__ = (UniqueConstraint('tenant_id', 'day', name='uq_quota_tenant_day'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[str] = mapped_column(String(10))
    messages: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
