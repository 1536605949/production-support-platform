# -*- coding: utf-8 -*-
"""FastAPI application: the single HTTP entry point behind Caddy.

Request flow::

    browser → Caddy → this app (SSE) → core trunk → business service
                                    ↘ PostgreSQL / Redis

The middleware below is what makes the trace requirement real: a ``trace_id``
is minted (or accepted from the caller) before any handler runs, attached to
the ambient :class:`TraceContext`, echoed back in the response headers, and
stamped onto every log line and every persisted row for the rest of the
request.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from . import deps, metrics
from .config import get_settings, is_placeholder_secret, validate_settings
from .core import observability
from .db import create_schema
from .errors import install_error_handlers
from .logging_utils import configure_logging
from .routers import admin, approvals, auth, chat, conversations

settings = deps.settings
validate_settings(settings)
configure_logging(settings.log_level)
logger = logging.getLogger('gateway')


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown hooks.

    Replaces the deprecated ``@app.on_event('startup')``: ``on_event`` is
    removed in newer Starlette releases and already emits a DeprecationWarning
    on every boot, which trains operators to ignore warnings.
    """
    if settings.auto_create_schema:
        create_schema()
    if settings.auto_seed:
        from .seed import seed_platform

        seed_platform()
    observability.log_event(
        logger, 'startup',
        engine_mode=settings.engine_mode, auth_mode=settings.auth_mode,
        environment=settings.environment, llm_enabled=deps.llm_client.available,
        llm_model=settings.llm_model if deps.llm_client.available else '',
        embedding_enabled=settings.embedding_enabled,
    )
    yield


app = FastAPI(title='Production Support Platform', version=settings.version,
              lifespan=lifespan)
install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(',') if o.strip()],
    allow_credentials=False,
    allow_methods=['GET', 'POST', 'DELETE', 'OPTIONS'],
    allow_headers=['*'],
    expose_headers=['X-Request-ID', 'X-Trace-ID'],
)

app.mount('/metrics', make_asgi_app())


@app.middleware('http')
async def request_context(request: Request, call_next):
    request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())
    trace_id = request.headers.get('X-Trace-ID') or observability.new_trace_id()
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    observability.set_context(observability.TraceContext(trace_id=trace_id, request_id=request_id))

    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Trace-ID'] = trace_id
        return response
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        metrics.REQUESTS.labels(request.url.path, str(status)).inc()
        observability.log_event(
            logger, 'request_finished',
            path=request.url.path, method=request.method, status=status,
            elapsed_ms=elapsed_ms,
        )


@app.get('/api/healthz')
def healthz():
    return {'status': 'ok', 'version': settings.version}


@app.get('/api/readyz')
def readyz():
    checks: dict[str, bool] = {}
    if settings.engine_mode == 'dify':
        checks['dify_api_key'] = bool(settings.dify_api_key) and not is_placeholder_secret(
            settings.dify_api_key
        )
    else:
        checks['database'] = _database_ok()
        # A placeholder JWT secret is fatal in production (validate_settings
        # already refuses to boot); in dev the offline demo intentionally uses
        # one, so it must not make the service report itself unready.
        if settings.environment.lower() == 'production':
            checks['jwt_secret'] = not is_placeholder_secret(settings.jwt_secret)
    ready = all(checks.values()) if checks else True
    return {
        'status': 'ready' if ready else 'not_ready',
        'version': settings.version,
        'engine_mode': settings.engine_mode,
        'auth_mode': settings.auth_mode,
        'llm_enabled': deps.llm_client.available,
        # Informational only — deliberately NOT part of ``checks``. The trunk is
        # designed to answer correctly with no model at all, so a missing or
        # circuit-broken LLM must never flip the service to ``not_ready`` and
        # take it out of a load balancer.
        'llm': deps.llm_client.status(),
        'embedding_enabled': settings.embedding_enabled,
        'checks': checks,
    }


def _database_ok() -> bool:
    from sqlalchemy import text

    from .db import engine

    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        return True
    except Exception:  # noqa: BLE001 - readiness must never raise
        return False


app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(approvals.router)
app.include_router(admin.router)


@app.get('/')
def index():
    return {
        'service': 'production-support-platform',
        'version': settings.version,
        'docs': '/docs',
        'health': '/api/healthz',
    }


# Legacy aliases kept so external tooling that imported them keeps working.
s = settings
client = deps.dify_client
limiter = deps.limiter
