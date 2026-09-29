# -*- coding: utf-8 -*-
"""HTTP-level contract tests for the platform API."""
import json

from fastapi.testclient import TestClient

from gateway.app import deps
from gateway.app.dify_client import DifyClient
from gateway.app.main import app
from gateway.app.rate_limit import RateLimiter


def _events(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith('data: '):
            payload = line[6:].strip()
            if payload and payload != '[DONE]':
                out.append(json.loads(payload))
    return out


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def test_healthz(client):
    body = client.get('/api/healthz').json()
    assert body['status'] == 'ok'
    assert body['version']


def test_readyz_reports_engine_and_database(client):
    body = client.get('/api/readyz').json()
    assert body['status'] == 'ready'
    assert body['engine_mode'] == 'builtin'
    assert body['checks']['database'] is True


# --------------------------------------------------------------------------- #
# Chat (deterministic trunk)
# --------------------------------------------------------------------------- #
def test_chat_routes_order_query_through_a_whitelisted_tool(client, dev_headers, business_stub):
    resp = client.post('/api/chat', headers=dev_headers,
                       json={'message': '帮我查一下订单 ORD-1001 的物流'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['intent'] == 'order_query'
    assert body['intent_source'] == 'deterministic'
    assert 'ORD-1001' in body['answer']
    assert body['conversation_id']
    assert body['message_id']
    assert body['request_id'] and body['trace_id']
    assert [name for name, _ in business_stub] == ['get_order']


def test_chat_persists_history_across_requests(client, dev_headers, business_stub):
    first = client.post('/api/chat', headers=dev_headers, json={'message': '你好'}).json()
    conversation_id = first['conversation_id']

    second = client.post('/api/chat', headers=dev_headers,
                         json={'message': '我要退款 ORD-1001', 'conversation_id': conversation_id})
    assert second.status_code == 200
    assert second.json()['conversation_id'] == conversation_id

    detail = client.get(f'/api/conversations/{conversation_id}', headers=dev_headers).json()
    roles = [m['role'] for m in detail['messages']]
    assert roles == ['user', 'assistant', 'user', 'assistant']
    assert detail['messages'][2]['content'] == '我要退款 ORD-1001'


def test_chat_handoff_when_nothing_matches(client, dev_headers):
    resp = client.post('/api/chat', headers=dev_headers, json={'message': 'zzzz qqqq xxxx'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['handoff_level'] >= 1
    assert body['suggestions']


def test_explicit_handoff_marks_conversation_waiting_human(client, dev_headers):
    body = client.post('/api/chat', headers=dev_headers, json={'message': '我要转人工客服'}).json()
    assert body['handoff_level'] == 3
    detail = client.get(f"/api/conversations/{body['conversation_id']}", headers=dev_headers).json()
    assert detail['conversation']['status'] == 'waiting_human'


def test_chat_stream_emits_start_deltas_and_final(client, dev_headers, business_stub):
    with client.stream('POST', '/api/chat/stream', headers=dev_headers,
                       json={'message': '订单 ORD-1001 什么时候到'}) as resp:
        assert resp.status_code == 200
        assert resp.headers['content-type'].startswith('text/event-stream')
        body = ''.join(resp.iter_text())

    events = _events(body)
    kinds = [e['event'] for e in events]
    assert kinds[0] == 'start'
    assert 'final' in kinds
    assert kinds[-1] == 'final'
    final = events[-1]
    assert final['conversation_id']
    assert final['message_id']
    assert body.rstrip().endswith('data: [DONE]')


def test_chat_rejects_empty_message(client, dev_headers):
    assert client.post('/api/chat', headers=dev_headers, json={'message': ''}).status_code == 422


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
def test_rate_limit_returns_429(client, dev_headers, monkeypatch):
    # The limiter uses a fixed one-minute window, so freeze the clock: without
    # this the three requests can straddle a minute boundary and the third one
    # would legitimately start a fresh window.
    monkeypatch.setattr('gateway.app.rate_limit.time.time', lambda: 1_700_000_000.0)
    monkeypatch.setattr(deps, 'limiter', RateLimiter('redis://127.0.0.1:6399/0', 2))
    monkeypatch.setattr(deps, 'principal_tenant_rate_limit', lambda principal: 2)

    codes = [
        client.post('/api/chat', headers=dev_headers, json={'message': 'hi'}).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


# --------------------------------------------------------------------------- #
# Legacy Dify proxy mode
# --------------------------------------------------------------------------- #
class FakeDify:
    async def chat_blocking(self, **kwargs):
        return {'answer': 'fake answer', 'conversation_id': 'c', 'message_id': 'm',
                'metadata': {'usage': {}}}

    async def chat_stream(self, **kwargs):
        yield 'data: {"event": "message", "answer": "离线演示"}'

    async def submit_human_input(self, **kwargs):
        return {'status': 'submitted', 'action': kwargs.get('action')}


def test_dify_mode_proxies_blocking_chat(client, dev_headers, monkeypatch):
    monkeypatch.setattr(deps.settings, 'engine_mode', 'dify')
    monkeypatch.setattr(deps, 'get_dify_client', lambda: FakeDify())
    body = client.post('/api/chat', headers=dev_headers, json={'message': 'hello'}).json()
    assert body['answer'] == 'fake answer'
    assert body['conversation_id'] == 'c'


def test_dify_mode_streams_mock_answer(client, dev_headers, monkeypatch):
    monkeypatch.setattr(deps.settings, 'engine_mode', 'dify')
    monkeypatch.setattr(deps, 'get_dify_client',
                        lambda: DifyClient('https://example.invalid/v1', '', mock=True))
    with client.stream('POST', '/api/chat/stream', headers=dev_headers,
                       json={'message': 'hi'}) as resp:
        body = ''.join(resp.iter_text())
    assert '离线演示' in body and body.startswith('data: ')


def test_hitl_submit_requires_reviewer_role(client, dev_headers, reviewer_headers, monkeypatch):
    monkeypatch.setattr(deps.settings, 'engine_mode', 'dify')
    monkeypatch.setattr(deps, 'get_dify_client', lambda: FakeDify())

    denied = client.post('/api/hitl/form-token', headers=dev_headers,
                         json={'inputs': {}, 'action': 'approve'})
    assert denied.status_code == 403

    allowed = client.post('/api/hitl/form-token', headers=reviewer_headers,
                          json={'inputs': {}, 'action': 'approve'})
    assert allowed.status_code == 200
    assert allowed.json()['status'] == 'submitted'


def test_index_endpoint():
    with TestClient(app) as c:
        assert c.get('/').json()['service'] == 'production-support-platform'
