# -*- coding: utf-8 -*-
"""Multi-tenancy, JWT auth and row-level isolation."""
import pytest
from fastapi.testclient import TestClient

from gateway.app.config import get_settings
from gateway.app.core import auth as auth_mod
from gateway.app.core import tenant as tenant_mod


# --------------------------------------------------------------------------- #
# Password + token primitives
# --------------------------------------------------------------------------- #
def test_password_hash_is_salted_and_verifiable():
    first = auth_mod.hash_password('CorrectHorse1')
    second = auth_mod.hash_password('CorrectHorse1')
    assert first != second                      # salted
    assert auth_mod.verify_password('CorrectHorse1', first)
    assert not auth_mod.verify_password('wrong', first)
    assert not auth_mod.verify_password('CorrectHorse1', 'garbage')


def test_token_round_trip_carries_the_tenant_scope():
    token = auth_mod.issue_token(
        subject='U-1', secret='s' * 40, issuer='test', tenant_id='t-1',
        store_id='st-1', roles=['customer'],
    )
    payload = auth_mod.decode_token(token, 's' * 40, 'test')
    assert payload['sub'] == 'U-1'
    assert payload['tenant_id'] == 't-1'
    assert payload['store_id'] == 'st-1'
    assert payload['roles'] == ['customer']


def test_token_rejects_wrong_secret_issuer_and_algorithm():
    token = auth_mod.issue_token(subject='U-1', secret='s' * 40, issuer='test', tenant_id='t-1')
    with pytest.raises(auth_mod.TokenError):
        auth_mod.decode_token(token, 'x' * 40, 'test')
    with pytest.raises(auth_mod.TokenError):
        auth_mod.decode_token(token, 's' * 40, 'other-issuer')

    # alg:none must be rejected before the signature is even considered.
    import base64, json
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'none'}).encode()).rstrip(b'=').decode()
    payload = base64.urlsafe_b64encode(json.dumps({'sub': 'x', 'iss': 'test', 'exp': 9999999999}).encode()).rstrip(b'=').decode()
    with pytest.raises(auth_mod.TokenError):
        auth_mod.decode_token(f'{header}.{payload}.', 's' * 40, 'test')


def test_expired_token_is_rejected():
    token = auth_mod.issue_token(subject='U-1', secret='s' * 40, issuer='test',
                                 tenant_id='t-1', ttl_minutes=-1)
    with pytest.raises(auth_mod.TokenError):
        auth_mod.decode_token(token, 's' * 40, 'test')


# --------------------------------------------------------------------------- #
# Registration / login
# --------------------------------------------------------------------------- #
def test_register_then_login_returns_a_usable_token(client: TestClient):
    registered = client.post('/api/auth/register', json={
        'tenant_id': 'tenant-acme', 'email': 'owner@acme.test',
        'password': 'AcmePass123', 'display_name': 'Acme Owner', 'tenant_name': 'Acme',
    })
    assert registered.status_code == 201
    token = registered.json()['access_token']
    assert registered.json()['tenant_id'] == 'tenant-acme'
    assert registered.json()['roles'] == ['admin']  # first user owns the tenant

    logged_in = client.post('/api/auth/login', json={
        'tenant_id': 'tenant-acme', 'email': 'owner@acme.test', 'password': 'AcmePass123',
    })
    assert logged_in.status_code == 200
    # The token is re-issued (its ``iat``/``exp`` differ), so compare the claims
    # rather than the raw string.
    settings = get_settings()
    payload = auth_mod.decode_token(logged_in.json()['access_token'],
                                    settings.jwt_secret, settings.jwt_issuer)
    original = auth_mod.decode_token(token, settings.jwt_secret, settings.jwt_issuer)
    assert payload['sub'] == original['sub']
    assert payload['tenant_id'] == original['tenant_id'] == 'tenant-acme'


def test_login_with_a_wrong_password_is_401_and_does_not_leak_existence(client: TestClient):
    client.post('/api/auth/register', json={
        'tenant_id': 'tenant-beta', 'email': 'b@beta.test', 'password': 'BetaPass123',
    })
    wrong_password = client.post('/api/auth/login', json={
        'tenant_id': 'tenant-beta', 'email': 'b@beta.test', 'password': 'NopePass123',
    })
    unknown_user = client.post('/api/auth/login', json={
        'tenant_id': 'tenant-beta', 'email': 'ghost@beta.test', 'password': 'NopePass123',
    })
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()['detail'] == unknown_user.json()['detail']


def test_duplicate_email_in_the_same_tenant_is_rejected(client: TestClient):
    payload = {'tenant_id': 'tenant-gamma', 'email': 'g@gamma.test', 'password': 'GammaPass123'}
    assert client.post('/api/auth/register', json=payload).status_code == 201
    assert client.post('/api/auth/register', json=payload).status_code == 409


def test_me_reflects_the_dev_identity(client: TestClient, dev_headers):
    body = client.get('/api/auth/me', headers=dev_headers).json()
    assert body['tenant_id'] == 'tenant-demo'
    assert body['user_id'] == 'user001'
    assert 'customer' in body['roles']


# --------------------------------------------------------------------------- #
# Row-level isolation
# --------------------------------------------------------------------------- #
def test_conversations_are_not_visible_across_tenants(client, dev_headers, other_tenant_headers):
    created = client.post('/api/chat', headers=dev_headers, json={'message': '你好'}).json()
    conversation_id = created['conversation_id']

    assert client.get(f'/api/conversations/{conversation_id}', headers=dev_headers).status_code == 200
    # Another tenant gets 404, not 403: existence itself is not disclosed.
    assert client.get(f'/api/conversations/{conversation_id}',
                      headers=other_tenant_headers).status_code == 404


def test_conversation_list_is_scoped_to_the_caller(client, dev_headers, other_tenant_headers):
    client.post('/api/chat', headers=dev_headers, json={'message': '你好'})
    mine = client.get('/api/conversations', headers=dev_headers).json()
    theirs = client.get('/api/conversations', headers=other_tenant_headers).json()
    assert mine and not theirs


# --------------------------------------------------------------------------- #
# Quota
# --------------------------------------------------------------------------- #
def test_quota_is_tracked_per_tenant(client, dev_headers):
    client.post('/api/chat', headers=dev_headers, json={'message': '你好'})
    quota = client.get('/api/admin/quota', headers=dev_headers).json()
    assert quota['tenant_id'] == 'tenant-demo'
    assert quota['messages_used'] >= 1
    assert quota['remaining'] == quota['messages_limit'] - quota['messages_used']


def test_daily_quota_exhaustion_is_403(client, dev_headers, monkeypatch, db_session):
    tenant = tenant_mod.get_tenant(db_session, 'tenant-demo')
    monkeypatch.setattr(tenant, 'daily_message_quota', 0)
    db_session.commit()
    try:
        resp = client.post('/api/chat', headers=dev_headers, json={'message': '你好'})
        assert resp.status_code == 403
        assert 'messages/day' in resp.json()['detail']
    finally:
        monkeypatch.undo()
        tenant = tenant_mod.get_tenant(db_session, 'tenant-demo')
        tenant.daily_message_quota = 100000
        db_session.commit()
