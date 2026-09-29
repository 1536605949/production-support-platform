# -*- coding: utf-8 -*-
"""Full-chain trace context and structured event logging.

A single :class:`TraceContext` is created at the HTTP edge and stored in a
``contextvars.ContextVar``. Every trunk component (intent, retrieval, LLM,
governance, workflow) logs through :func:`log_event`, which stamps the same
``trace_id`` onto each line — so one ``grep`` on a trace id reconstructs the
whole request across modules and services.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class TraceContext:
    trace_id: str = ''
    request_id: str = ''
    tenant_id: str = ''
    store_id: str = ''
    user_id: str = ''
    conversation_id: str = ''
    extras: dict[str, Any] = field(default_factory=dict)

    def evolve(self, **kwargs) -> 'TraceContext':
        return replace(self, **kwargs)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'trace_id': self.trace_id,
            'request_id': self.request_id,
            'tenant_id': self.tenant_id,
            'user_id': self.user_id,
        }
        if self.store_id:
            out['store_id'] = self.store_id
        if self.conversation_id:
            out['conversation_id'] = self.conversation_id
        if self.extras:
            out.update(self.extras)
        return out


_current: ContextVar[TraceContext] = ContextVar('trace_context', default=TraceContext())


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_context(ctx: TraceContext) -> None:
    _current.set(ctx)


def get_context() -> TraceContext:
    return _current.get()


def bind(**kwargs) -> None:
    """Attach fields to the ambient context (e.g. once the conversation is known)."""
    _current.set(_current.get().evolve(**kwargs))


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured JSON log line carrying the ambient trace context."""
    payload = get_context().as_dict()
    payload['event'] = event
    payload.update(fields)
    logger.log(level, event, extra={'event': payload})


def traced_span(logger: logging.Logger, name: str, **fields: Any):
    """Context manager that logs ``<name>_start`` / ``<name>_done`` with timing.

    Usage::

        with traced_span(logger, 'knowledge_retrieve', top_k=4) as span:
            hits = retrieve(...)
            span['hits'] = len(hits)
    """
    import time
    from contextlib import contextmanager

    @contextmanager
    def _span():
        span_fields: dict[str, Any] = dict(fields)
        started = time.perf_counter()
        log_event(logger, f'{name}_start', **span_fields)
        try:
            yield span_fields
        except Exception as exc:  # noqa: BLE001 - we re-raise, just logging
            log_event(
                logger,
                f'{name}_error',
                level=logging.ERROR,
                error=type(exc).__name__,
                message=str(exc)[:500],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                **span_fields,
            )
            raise
        else:
            log_event(
                logger,
                f'{name}_done',
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                **span_fields,
            )

    return _span()
