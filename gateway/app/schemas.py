# -*- coding: utf-8 -*-
"""Request/response contracts for the platform API.

Every inbound write body is a Pydantic model, which is the same discipline the
governance layer applies to tool arguments: nothing reaches business logic
unparsed.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=64)


class RegisterRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default='', max_length=128)
    store_id: str = Field(default='', max_length=64)
    tenant_name: str = Field(default='', max_length=128)

    @field_validator('password')
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        if len(set(value)) < 4:
            raise ValueError('password is too repetitive')
        return value


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    expires_in: int
    tenant_id: str
    user_id: str
    roles: list[str] = Field(default_factory=list)


class PrincipalOut(BaseModel):
    user_id: str
    tenant_id: str
    store_id: str = ''
    email: str = ''
    roles: list[str] = Field(default_factory=list)
    auth_mode: str


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str = ''
    #: optional client-supplied key; makes a retried send idempotent
    idempotency_key: str = Field(default='', max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict)


class SourceOut(BaseModel):
    id: str = ''
    title: str = ''
    category: str = ''
    score: float = 0.0
    keyword_score: float = 0.0
    vector_score: float = 0.0


class ChatResponse(BaseModel):
    request_id: str
    trace_id: str
    answer: str
    conversation_id: str = ''
    message_id: str = ''
    intent: str = ''
    intent_label: str = ''
    confidence: float = 0.0
    intent_source: str = ''
    handoff_level: int = 0
    suggestions: list[str] = Field(default_factory=list)
    sources: list[SourceOut] = Field(default_factory=list)
    tool: dict[str, Any] | None = None
    #: The verified plan for this turn: its steps, their dependency edges, and
    #: the ledger of every gate it passed — or the reason it was rejected.
    #:
    #: This field was missing when ``plan`` was first added to ``TurnResult``,
    #: while ``_to_response`` already passed ``plan=result.plan`` into the
    #: constructor. Pydantic ignores undeclared keyword arguments, so the plan
    #: was **silently dropped from both the blocking and the streaming
    #: response**: the operator console's decision inspector had nothing to
    #: render, and the evaluation gate could not see a plan at all. Nothing
    #: failed — which is why the guard lives in
    #: ``tests/test_response_contract.py`` rather than in a comment.
    plan: dict[str, Any] | None = None
    approval_id: str = ''
    workflow_run_id: str = ''
    degraded: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
class ConversationOut(BaseModel):
    id: str
    title: str = ''
    status: str = 'active'
    last_intent: str = ''
    handoff_level: int = 0
    message_count: int = 0
    last_message_at: str = ''
    created_at: str = ''


class MessageOut(BaseModel):
    id: str
    seq: int
    role: str
    content: str
    intent: str = ''
    confidence: float = 0.0
    sources: list[dict] = Field(default_factory=list)
    created_at: str = ''
    ttft_ms: float = 0.0
    latency_ms: float = 0.0
    # These live in the row's ``meta`` JSON rather than in dedicated columns, but
    # they are part of what the UI needs to explain an answer *after a reload*:
    # without them a reopened conversation silently loses the "this came from
    # the deterministic template, not the model" marker that the live stream
    # showed, so the same message looks different depending on how you got to it.
    intent_source: str = ''
    compose_source: str = ''
    degraded: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    #: The plan this turn ran, and the ledger of every verification gate it
    #: passed. Present so "why was this AI action allowed to execute" is
    #: answerable from the conversation record rather than from the logs of a
    #: process that has since restarted. ``None`` when the turn built no plan
    #: (the step did not run at all, e.g. a handoff).
    plan: dict | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
class ApprovalOut(BaseModel):
    id: str
    tool_name: str
    risk_level: str
    status: str
    reason: str = ''
    conversation_id: str = ''
    invocation_id: str = ''
    reviewer_id: str = ''
    comment: str = ''
    arguments: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ''
    expires_at: str = ''


class ApprovalDecision(BaseModel):
    decision: Literal['approve', 'reject']
    comment: str = Field(default='', max_length=1000)


# --------------------------------------------------------------------------- #
# Legacy (Dify proxy + HITL)
# --------------------------------------------------------------------------- #
class HitlSubmitRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    action: str
