# -*- coding: utf-8 -*-
"""The API's error contract.

Every error the platform returns has to be machine-readable *and* renderable.
The specific bug these tests lock down: FastAPI's default 422 body has
``detail`` as a **list**, and the demo client rendered ``body.detail`` directly,
so a user who typed a too-long message saw ``[object Object]``.

So the contract asserted here is:

* ``detail`` is always a **string** — existing clients and tests read it, and
  changing its type would be a silent breaking change;
* ``error.code`` is a stable enum a client can switch on;
* ``error.fields`` names the offending fields for validation failures;
* ``request_id`` / ``trace_id`` are always present, so a screenshot is actionable.
"""
from __future__ import annotations

import pytest

from gateway.app import deps
from gateway.app.errors import code_for_status


def test_validation_error_detail_is_a_string(client, dev_headers):
    """The regression that started this: 422 used to return a list."""
    resp = client.post('/api/chat', headers=dev_headers, json={'message': ''})
    assert resp.status_code == 422

    body = resp.json()
    assert isinstance(body['detail'], str), 'detail must stay a string for clients'
    assert body['detail'], 'detail must not be empty'
    assert body['error']['code'] == 'validation_error'


def test_validation_error_names_the_offending_field(client, dev_headers):
    resp = client.post('/api/chat', headers=dev_headers,
                       json={'message': 'x' * 9000})
    assert resp.status_code == 422
    fields = resp.json()['error']['fields']
    assert fields, 'a validation failure must list at least one field'
    assert any('message' in f['field'] for f in fields), fields
    # The field path must not be prefixed with the transport wrapper.
    assert not any(f['field'].startswith('body.') for f in fields), fields


def test_missing_required_field_is_reported(client):
    resp = client.post('/api/chat', json={})
    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body['detail'], str)
    assert body['error']['fields']


def test_dev_mode_accepts_a_bare_request(client):
    """``AUTH_MODE=dev`` is the offline demo path: no token required.

    Production runs ``AUTH_MODE=jwt``, where this exact request is a 401 — that
    path is asserted by the public end-to-end probe rather than here, because
    this suite deliberately runs in dev mode so the trunk can be exercised
    without minting tokens.
    """
    resp = client.post('/api/chat', json={'message': '你好'})
    assert resp.status_code == 200
    assert resp.json()['conversation_id'], 'the request must resolve to a real tenant'


def test_forbidden_has_a_stable_code(client, dev_headers):
    """A customer asking for the approval queue is 403, not 500."""
    resp = client.get('/api/approvals', headers=dev_headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body['error']['code'] == 'forbidden'
    assert isinstance(body['detail'], str)


def test_not_found_has_a_stable_code(client, dev_headers):
    resp = client.get('/api/conversations/does-not-exist', headers=dev_headers)
    assert resp.status_code == 404
    assert resp.json()['error']['code'] == 'not_found'


def test_rate_limited_carries_retry_after(client, dev_headers, monkeypatch):
    """429 must tell the client when to come back, not just that it failed."""
    async def deny(*_args, **_kwargs):
        return False

    monkeypatch.setattr(deps.limiter, 'allow', deny)
    resp = client.post('/api/chat', headers=dev_headers, json={'message': 'hi'})

    assert resp.status_code == 429
    assert resp.json()['error']['code'] == 'rate_limited'

    retry_after = resp.headers.get('Retry-After')
    assert retry_after is not None, 'a 429 without Retry-After forces blind retries'
    assert 1 <= int(retry_after) <= 60


def test_every_error_carries_trace_ids(client, dev_headers):
    resp = client.post('/api/chat', headers=dev_headers, json={'message': ''})
    body = resp.json()
    assert body['request_id'], 'a user must be able to quote a request id'
    assert body['trace_id']
    assert resp.headers.get('X-Trace-ID') == body['trace_id']


def test_success_response_is_unchanged(client, dev_headers):
    """The envelope is additive: a 200 body must not grow an ``error`` key."""
    resp = client.post('/api/chat', headers=dev_headers, json={'message': '你好'})
    assert resp.status_code == 200
    assert 'error' not in resp.json()


@pytest.mark.parametrize('status,expected', [
    (400, 'bad_request'),
    (401, 'unauthorized'),
    (403, 'forbidden'),
    (404, 'not_found'),
    (409, 'conflict'),
    (422, 'validation_error'),
    (429, 'rate_limited'),
    (500, 'internal_error'),
    (502, 'upstream_error'),
    (504, 'upstream_timeout'),
    (418, 'http_418'),
])
def test_status_to_code_mapping_is_total(status, expected):
    assert code_for_status(status) == expected
