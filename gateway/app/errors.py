# -*- coding: utf-8 -*-
"""Unified error envelope for every HTTP error the platform returns.

Before this module the API answered errors in three incompatible shapes:

* ``HTTPException`` → ``{"detail": "rate limit exceeded"}``
* Pydantic validation failure → ``{"detail": [{"loc": [...], "msg": "..."}]}``
* unhandled exception → ``{"detail": "internal error"}``

The middle one is the dangerous one. ``detail`` is a **list** there, so any
client that does ``body.detail`` and renders it shows ``[object Object]`` —
which is exactly what the demo client did, so a user who typed a message longer
than 8000 characters got a useless error and no idea what to fix.

Every error now carries one envelope::

    {
      "detail": "message: String should have at most 8000 characters",
      "error": {
        "code": "validation_error",
        "message": "message: String should have at most 8000 characters",
        "fields": [{"field": "body.message", "message": "String should have at most 8000 characters"}]
      },
      "request_id": "...",
      "trace_id": "..."
    }

``detail`` deliberately stays a **plain string**. It is what existing clients
and the test suite read (``'messages/day' in resp.json()['detail']``), and
changing its type would break them for no gain. ``error`` is the structured
form new clients should use; ``fields`` is only populated for validation errors.

``request_id`` / ``trace_id`` are always present so a user can quote one line
from a screenshot and an operator can find the exact request in the logs.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger('gateway.errors')

#: Stable machine-readable codes. Clients switch on these; humans read
#: ``message``. Keeping the mapping in one place is what stops a new endpoint
#: from inventing a fourth error shape.
_CODE_BY_STATUS = {
    400: 'bad_request',
    401: 'unauthorized',
    403: 'forbidden',
    404: 'not_found',
    405: 'method_not_allowed',
    409: 'conflict',
    422: 'validation_error',
    429: 'rate_limited',
    500: 'internal_error',
    502: 'upstream_error',
    503: 'service_unavailable',
    504: 'upstream_timeout',
}

#: 429 responses tell the client when to come back. The limiter is a fixed
#: one-minute window, so the honest answer is "when the current minute ends".
RATE_LIMIT_WINDOW_SECONDS = 60


def code_for_status(status: int) -> str:
    return _CODE_BY_STATUS.get(status, f'http_{status}')


def _trace_fields(request: Request) -> dict:
    return {
        'request_id': getattr(request.state, 'request_id', ''),
        'trace_id': getattr(request.state, 'trace_id', ''),
    }


def _field_errors(exc: RequestValidationError) -> list[dict]:
    """Flatten Pydantic's error list into ``{field, message}`` pairs.

    The leading transport marker (``body`` / ``query`` / ``path`` / ``header``)
    is dropped so the field reads like the JSON the client actually sent:
    ``message`` is what a user has to fix, ``body.message`` is an implementation
    detail of how it arrived.
    """
    fields: list[dict] = []
    for err in exc.errors():
        loc = [str(part) for part in (err.get('loc') or [])
               if part not in ('__root__', 'body', 'query', 'path', 'header')]
        fields.append({
            'field': '.'.join(loc) or '(body)',
            'message': str(err.get('msg') or 'invalid value'),
        })
    return fields


def _validation_message(fields: list[dict]) -> str:
    """One readable line naming the first offending field."""
    if not fields:
        return 'request validation failed'
    first = fields[0]
    suffix = f'（另有 {len(fields) - 1} 处问题）' if len(fields) > 1 else ''
    return f'{first["field"]}: {first["message"]}{suffix}'


def _as_message(detail: object) -> str:
    """``HTTPException.detail`` may be a str, dict or list — always yield text."""
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - a bad detail must not break the handler
        return str(detail)


def install_error_handlers(app: FastAPI) -> None:
    """Register the three handlers that make the envelope universal."""

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        fields = _field_errors(exc)
        message = _validation_message(fields)
        return JSONResponse(
            status_code=422,
            content={
                'detail': message,
                'error': {
                    'code': 'validation_error',
                    'message': message,
                    'fields': fields,
                },
                **_trace_fields(request),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        message = _as_message(exc.detail)
        status = exc.status_code
        headers = dict(exc.headers or {})

        if status == 429 and 'Retry-After' not in headers:
            remaining = RATE_LIMIT_WINDOW_SECONDS - int(time.time()) % RATE_LIMIT_WINDOW_SECONDS
            headers['Retry-After'] = str(max(1, remaining))

        return JSONResponse(
            status_code=status,
            headers=headers,
            content={
                'detail': message,
                'error': {
                    'code': code_for_status(status),
                    'message': message,
                    'fields': [],
                },
                **_trace_fields(request),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # The detail of an unexpected exception is *not* echoed to the client:
        # it can contain a connection string or an upstream payload. The
        # operator gets it from the log line, correlated by trace_id.
        logger.exception('unhandled error on %s', request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                'detail': 'internal error',
                'error': {
                    'code': 'internal_error',
                    'message': '服务器内部错误，请把 trace_id 提供给运维排查。',
                    'fields': [],
                },
                **_trace_fields(request),
            },
        )
