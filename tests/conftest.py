# -*- coding: utf-8 -*-
"""Shared test setup.

The environment is configured *before* any application module is imported,
because ``gateway.app.db`` builds its engine at import time from the settings.
Tests run against a throwaway SQLite file so the trunk's real persistence path
(conversations, messages, approvals, checkpoints) is exercised rather than
mocked away.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / 'support_platform_tests'
_TMP.mkdir(parents=True, exist_ok=True)
_DB_FILE = _TMP / 'test.db'
if _DB_FILE.exists():
    _DB_FILE.unlink()

os.environ.update({
    'ENVIRONMENT': 'development',
    'ENGINE_MODE': 'builtin',
    'AUTH_MODE': 'dev',
    'AUTO_CREATE_SCHEMA': '1',
    'AUTO_SEED': '1',
    'DATABASE_URL': f'sqlite:///{_DB_FILE.as_posix()}',
    'REDIS_URL': 'redis://127.0.0.1:6399/0',
    'LLM_ENABLED': '0',
    'EMBEDDING_ENABLED': '0',
    'LOG_LEVEL': 'WARNING',
    'TENANT_DAILY_MESSAGE_QUOTA': '100000',
    'RATE_LIMIT_PER_MINUTE': '100000',
    # The gateway needs the reviewer credential to relay a human approval into
    # the business service's own review state machine.
    'REVIEWER_API_KEY': 'test-reviewer-key',
    'REVIEWER_ID': 'test-reviewer',
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway.app import deps  # noqa: E402
from gateway.app.db import create_schema  # noqa: E402
from gateway.app.main import app  # noqa: E402
from gateway.app.seed import seed_platform  # noqa: E402


@pytest.fixture(scope='session', autouse=True)
def _database():
    create_schema()
    seed_platform()
    yield


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def dev_headers() -> dict:
    """Headers for the offline demo identity (AUTH_MODE=dev)."""
    return {
        'X-Demo-User': 'user001',
        'X-Demo-Tenant': 'tenant-demo',
        'X-Demo-Store': 'store-demo',
        'X-Demo-Roles': 'customer',
    }


@pytest.fixture()
def reviewer_headers() -> dict:
    return {
        'X-Demo-User': 'reviewer001',
        'X-Demo-Tenant': 'tenant-demo',
        'X-Demo-Store': 'store-demo',
        'X-Demo-Roles': 'reviewer,admin',
    }


@pytest.fixture()
def other_tenant_headers() -> dict:
    return {
        'X-Demo-User': 'user002',
        'X-Demo-Tenant': 'tenant-other',
        'X-Demo-Store': 'store-other',
        'X-Demo-Roles': 'customer',
    }


@pytest.fixture()
def db_session():
    from gateway.app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class _RecordedCalls(list):
    """Tool calls, plus the approval relays that were sent to the business side.

    Kept as a ``list`` so existing assertions (``len(business_stub) == 1``)
    continue to mean "one tool call", while ``.reviews`` carries the separate
    decision-relay traffic.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reviews: list[tuple[str, dict, str]] = []


@pytest.fixture()
def business_stub(monkeypatch):
    """Replace the HTTP call to the business service with an in-process stub.

    The governance path (whitelist → args → risk → result contract) still runs
    for real; only the network hop is removed.
    """
    calls = _RecordedCalls()

    async def fake_call(spec, arguments, trace_id=''):
        calls.append((spec.name, dict(arguments)))
        if spec.name == 'get_order':
            return {
                'status': 'ok', 'order_id': arguments.get('order_id'),
                'user_id': arguments.get('user_id'), 'state': 'shipped',
                'status_text': 'shipped', 'amount_cents': 19900, 'refund_available': True,
            }
        if spec.name == 'create_refund_proposal':
            return {'status': 'ok', 'proposal_id': 'RP-TEST0001', 'state': 'pending_review'}
        if spec.name == 'create_ticket':
            return {'status': 'ok', 'ticket_id': 'T-TEST0001', 'state': 'created'}
        if spec.name == 'execute_refund':
            return {'status': 'ok', 'proposal_id': 'RP-TEST0001', 'state': 'executed'}
        return {'status': 'ok'}

    async def fake_review(spec, arguments, *, decision, comment='', trace_id=''):
        calls.reviews.append((spec.name, dict(arguments), decision))
        return {'status': 'ok', 'decision': decision}

    monkeypatch.setattr(deps.business_client, 'call', fake_call)
    monkeypatch.setattr(deps.business_client, 'review', fake_review)
    return calls
