# -*- coding: utf-8 -*-
"""Governance: whitelist, argument contract, risk gate, approvals, idempotency."""
import pytest
from fastapi.testclient import TestClient

from gateway.app.core import governance
from gateway.app import deps
from gateway.app.core.governance import (
    INVOCATION_TRANSITIONS,
    TOOL_REGISTRY,
    IllegalTransition,
    ToolDenied,
    assert_transition,
    invoke,
    make_idempotency_key,
    validate_arguments,
)
from gateway.app.config import get_settings


def test_registry_is_a_whitelist_not_a_passthrough():
    assert governance.is_whitelisted('get_order')
    assert not governance.is_whitelisted('drop_database')
    assert not governance.is_whitelisted('')


def test_risk_levels_are_declared_per_tool():
    assert TOOL_REGISTRY['get_order'].risk == 'low'
    assert TOOL_REGISTRY['create_ticket'].risk == 'medium'
    assert TOOL_REGISTRY['execute_refund'].risk == 'high'
    assert TOOL_REGISTRY['execute_refund'].requires_approval is True
    assert TOOL_REGISTRY['get_order'].requires_approval is False


def test_argument_contract_rejects_malformed_arguments():
    spec = TOOL_REGISTRY['get_order']
    parsed, error = validate_arguments(spec, {'user_id': 'user001'})
    assert parsed is None and 'order_id' in error

    parsed, error = validate_arguments(spec, {'user_id': 'user001', 'order_id': 'ORD-1001'})
    assert error == '' and parsed['order_id'] == 'ORD-1001'


def test_state_machine_rejects_illegal_transitions():
    assert_transition('tool_invocation', INVOCATION_TRANSITIONS, 'proposed', 'waiting_approval')
    with pytest.raises(IllegalTransition):
        assert_transition('tool_invocation', INVOCATION_TRANSITIONS, 'executed', 'approved')
    with pytest.raises(IllegalTransition):
        assert_transition('tool_invocation', INVOCATION_TRANSITIONS, 'rejected', 'executed')


@pytest.mark.asyncio
async def test_unknown_tool_is_denied_before_any_io(db_session, business_stub):
    with pytest.raises(ToolDenied) as exc:
        await invoke(
            db_session, get_settings(), deps.business_client,
            tool_name='exfiltrate_all_orders', arguments={}, tenant_id='tenant-demo',
        )
    assert exc.value.reason == 'tool_not_whitelisted'
    assert business_stub == []


@pytest.mark.asyncio
async def test_role_gate_denies_a_principal_without_the_required_role(db_session, business_stub):
    from pydantic import BaseModel

    from gateway.app.core.governance import ToolSpec, register_tool

    class _Args(BaseModel):
        user_id: str

    register_tool(ToolSpec(
        name='admin_only_tool', description='test', risk='low', args_model=_Args,
        method='POST', path='/admin-only', allowed_roles=frozenset({'admin'}),
    ))
    try:
        with pytest.raises(ToolDenied) as exc:
            await invoke(
                db_session, get_settings(), deps.business_client,
                tool_name='admin_only_tool', arguments={'user_id': 'user001'},
                tenant_id='tenant-demo', actor_roles=frozenset({'customer'}),
            )
        assert exc.value.reason == 'role_not_allowed'

        outcome = await invoke(
            db_session, get_settings(), deps.business_client,
            tool_name='admin_only_tool', arguments={'user_id': 'user001'},
            tenant_id='tenant-demo', actor_roles=frozenset({'admin'}),
        )
        assert outcome.status == 'executed'
    finally:
        TOOL_REGISTRY.pop('admin_only_tool', None)


@pytest.mark.asyncio
async def test_low_risk_tool_executes_inline(db_session, business_stub):
    outcome = await invoke(
        db_session, get_settings(), deps.business_client,
        tool_name='get_order',
        arguments={'user_id': 'user001', 'order_id': 'ORD-1001'},
        tenant_id='tenant-demo', user_id='user001',
    )
    assert outcome.status == 'executed'
    assert outcome.result['order_id'] == 'ORD-1001'


@pytest.mark.asyncio
async def test_high_risk_tool_parks_in_waiting_approval(db_session, business_stub):
    outcome = await invoke(
        db_session, get_settings(), deps.business_client,
        tool_name='execute_refund',
        # ``proposal_id`` is part of the contract now: executing "the refund for
        # this order" and "this proposal" are different operations, and only the
        # second one is provably the row a human approved.
        arguments={'user_id': 'user001', 'order_id': 'ORD-1001',
                   'proposal_id': 'RP-TEST0001'},
        tenant_id='tenant-demo', user_id='user001',
        actor_roles=frozenset({'agent'}),
    )
    assert outcome.status == 'waiting_approval'
    assert outcome.approval_id.startswith('AP-')
    # The business service must NOT have been called yet.
    assert business_stub == []


@pytest.mark.asyncio
async def test_retry_with_same_idempotency_key_does_not_execute_twice(db_session, business_stub):
    key = make_idempotency_key('tenant-demo', 'get_order', 'x')
    first = await invoke(
        db_session, get_settings(), deps.business_client,
        tool_name='get_order',
        arguments={'user_id': 'user001', 'order_id': 'ORD-1001'},
        tenant_id='tenant-demo', idempotency_key=key,
    )
    second = await invoke(
        db_session, get_settings(), deps.business_client,
        tool_name='get_order',
        arguments={'user_id': 'user001', 'order_id': 'ORD-1001'},
        tenant_id='tenant-demo', idempotency_key=key,
    )
    assert first.status == second.status == 'executed'
    assert second.replayed is True
    assert len(business_stub) == 1  # only one real call happened


# --------------------------------------------------------------------------- #
# End-to-end approval through the HTTP API
# --------------------------------------------------------------------------- #
def _start_refund_execution(client: TestClient, headers: dict) -> dict:
    body = client.post('/api/chat', headers=headers,
                       json={'message': '确认退款 ORD-1001 现在就退'}).json()
    assert body['handoff_level'] == 0
    return body


#: Every refund turn opens with a read-only check against the after-sale rule
#: table, which can veto the write path. A confirmation turn additionally
#: proposes against the order, because the refund plan is a three-step graph
#: (check → propose → execute) and the execute step consumes the proposal id.
#:
#: These are spelled out in the expected call sequences below rather than
#: filtered out of ``business_stub``. Filtering would make each of these guards
#: quieter than it looks: an *extra* unexpected call would stop failing the
#: assertion, and the guard would slowly stop guarding.
_RULE_PROBE = 'check_aftersale_eligibility'
_PROPOSE = 'create_refund_proposal'


def test_high_risk_chat_creates_an_approval_and_does_not_execute(client, dev_headers, business_stub):
    body = _start_refund_execution(client, dev_headers)
    assert body['tool']['status'] == 'waiting_approval'
    assert body['approval_id'].startswith('AP-')
    assert '人工审批' in body['answer']
    # The rule check and the proposal are both safe to run without a human; the
    # money only moves after approval, so ``execute_refund`` must not be here.
    assert [name for name, _ in business_stub] == [_RULE_PROBE, _PROPOSE]


def test_approval_requires_the_reviewer_role(client, dev_headers, reviewer_headers, business_stub):
    body = _start_refund_execution(client, dev_headers)
    approval_id = body['approval_id']

    denied = client.get('/api/approvals', headers=dev_headers)
    assert denied.status_code == 403

    queue = client.get('/api/approvals', headers=reviewer_headers)
    assert queue.status_code == 200
    assert any(a['id'] == approval_id for a in queue.json())


def test_approving_executes_exactly_once_and_resumes_the_turn(
    client, dev_headers, reviewer_headers, business_stub
):
    body = _start_refund_execution(client, dev_headers)
    approval_id = body['approval_id']
    conversation_id = body['conversation_id']

    decision = client.post(f'/api/approvals/{approval_id}/decide', headers=reviewer_headers,
                           json={'decision': 'approve', 'comment': '同意'})
    assert decision.status_code == 200
    assert decision.json()['status'] == 'approved'
    assert [name for name, _ in business_stub] == [_RULE_PROBE, _PROPOSE, 'execute_refund']

    # The execute call carries the proposal id that the *proposal step* returned
    # (``RP-TEST0001`` from the stub), not one the caller supplied and not a
    # placeholder. This is the end-to-end half of "the approval a human granted
    # and the approval this call consumes are the same row": the value travelled
    # plan step 2 -> InputRef -> resolved arguments -> stored invocation ->
    # resume -> business request, and any silent drop along that chain would
    # show up here as a missing or wrong id.
    execute_calls = [args for name, args in business_stub if name == 'execute_refund']
    assert execute_calls == [{'user_id': 'user001', 'order_id': 'ORD-1001',
                              'proposal_id': 'RP-TEST0001'}]

    # The parked turn resumed, so the user sees the outcome without asking again.
    detail = client.get(f'/api/conversations/{conversation_id}', headers=dev_headers).json()
    last = detail['messages'][-1]
    assert last['role'] == 'assistant'
    assert '退款已执行' in last['content']

    # A second decision is refused: the approval was consumed.
    again = client.post(f'/api/approvals/{approval_id}/decide', headers=reviewer_headers,
                        json={'decision': 'approve'})
    assert again.status_code == 409
    assert len(business_stub) == 3


def test_rejecting_does_not_execute(client, dev_headers, reviewer_headers, business_stub):
    body = _start_refund_execution(client, dev_headers)
    decision = client.post(f"/api/approvals/{body['approval_id']}/decide", headers=reviewer_headers,
                           json={'decision': 'reject', 'comment': '不符合条件'})
    assert decision.status_code == 200
    assert decision.json()['status'] == 'rejected'
    assert [name for name, _ in business_stub] == [_RULE_PROBE, _PROPOSE]


def test_approvals_are_tenant_scoped(client, dev_headers, reviewer_headers,
                                     other_tenant_headers, business_stub):
    body = _start_refund_execution(client, dev_headers)
    approval_id = body['approval_id']

    other_reviewer = dict(other_tenant_headers)
    other_reviewer['X-Demo-Roles'] = 'reviewer,admin'
    assert client.get(f'/api/approvals/{approval_id}', headers=other_reviewer).status_code == 404


# --------------------------------------------------------------------------- #
# Identity mapping: the platform's user id and the order system's customer id
# are different identity spaces.
#
# Regression guard. Before this was explicit, the gateway passed its own user id
# straight into tool arguments. That happened to work in dev mode (where the
# demo identity is literally ``user001``) and broke the moment real JWT logins
# were used, because the seeded account id is ``U-customer``. The business
# service then failed its ownership check and every order read came back as a
# 403 — a failure that looks like an authorization bug, not a mapping bug.
# --------------------------------------------------------------------------- #
def test_principal_business_ref_prefers_the_explicit_mapping():
    from gateway.app.security import Principal

    mapped = Principal(user_id='U-customer', business_customer_id='user001')
    assert mapped.business_ref == 'user001'

    # Empty mapping means the two id spaces coincide.
    unmapped = Principal(user_id='user001')
    assert unmapped.business_ref == 'user001'


def _tool_args(plan) -> list[tuple[str, dict]]:
    """A plan as ``(tool, literal arguments)`` pairs.

    ``plan_tools`` returns a :class:`~gateway.app.core.plan.Plan` — a typed
    graph, not the flat list it used to be. These tests are about *which tool
    gets called with which arguments*, so they read the plan through this
    projection rather than comparing the whole structure: the graph's shape is
    covered by ``tests/test_plan_verification.py``, and restating it here would
    mean every dependency change broke unrelated argument assertions.
    """
    return [(s.tool, dict(s.arguments)) for s in plan.steps]


def test_tool_arguments_carry_the_upstream_customer_id():
    from gateway.app.core import engine

    state = {
        'intent': 'refund',
        'slots': {'order_id': 'ORD-1001'},
        'user_id': 'U-customer',          # platform identity
        'business_customer_id': 'user001',  # order-system identity
    }
    plan = engine.plan_tools(state, '我要退款')
    assert _tool_args(plan) == [
        ('check_aftersale_eligibility',
         {'user_id': 'user001', 'order_id': 'ORD-1001'}),
        ('create_refund_proposal',
         {'user_id': 'user001', 'order_id': 'ORD-1001', 'reason': '我要退款'}),
    ]
    # Every step, not just the last one: the identity mapping has to hold for
    # the whole plan, and asserting only the final step would miss a step that
    # leaked the platform id.
    assert all(args['user_id'] == 'user001' for _, args in _tool_args(plan))


def test_tool_arguments_fall_back_to_the_platform_id_when_unmapped():
    from gateway.app.core import engine

    state = {'intent': 'order_query', 'slots': {'order_id': 'ORD-1001'}, 'user_id': 'user001'}
    plan = engine.plan_tools(state, '订单到哪了')
    assert _tool_args(plan) == [('get_order', {'user_id': 'user001', 'order_id': 'ORD-1001'})]


def test_workflow_input_preserves_the_mapping_across_a_park():
    """A parked approval run rebuilds its request from the workflow input."""
    from gateway.app.core.engine import TurnRequest, _workflow_input

    request = TurnRequest(
        tenant_id='tenant-demo', user_id='U-customer',
        business_customer_id='user001', message='确认退款 ORD-1001',
    )
    snapshot = _workflow_input(request)
    assert snapshot['business_customer_id'] == 'user001'
    assert TurnRequest(
        tenant_id='tenant-demo', user_id=snapshot['user_id'],
        business_customer_id=snapshot['business_customer_id'],
        message=snapshot['message'],
    ).business_ref == 'user001'


# --------------------------------------------------------------------------- #
# Upstream contract: how arguments reach the business service, and how a human
# decision reaches *its* state machine.
# --------------------------------------------------------------------------- #
def test_get_tool_arguments_are_sent_as_query_params(monkeypatch):
    """``get_order`` carries ``user_id`` in the query string, not the path.

    Regression guard. The client used to drop every non-path argument for GET,
    so the business service received ``GET /orders/ORD-1001`` with no
    ``user_id`` and answered 422. The ownership check never ran — the call
    failed before it could be authorised or refused.
    """
    import asyncio

    import httpx

    from gateway.app.core.governance import BusinessClient, TOOL_REGISTRY

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{}'

        def json(self) -> dict:
            return {'status': 'ok'}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            captured.update(url=url, params=dict(params or {}))
            return FakeResponse()

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, json=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)

    client = BusinessClient(get_settings())
    asyncio.run(client.call(TOOL_REGISTRY['get_order'],
                            {'user_id': 'user001', 'order_id': 'ORD-1001'}))

    assert captured['url'].endswith('/orders/ORD-1001')
    assert captured['params'] == {'user_id': 'user001'}


def test_execute_refund_body_names_the_proposal(monkeypatch):
    """The execute call must carry ``proposal_id`` in the body.

    The business service requires it and answers 400 without it, so a gateway
    that stopped sending it would break the money path. More importantly the
    *value* has to come from the plan's ``InputRef``, not from the caller: this
    test fixes the client-boundary half of that (the field is sent), and
    ``test_plan_verification`` fixes the other half (it is resolved from the
    proposal step rather than invented).
    """
    import asyncio

    import httpx

    from gateway.app.core.governance import BusinessClient, TOOL_REGISTRY

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{}'

        def json(self) -> dict:
            return {'status': 'executed', 'proposal_id': 'RP-ABC123'}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, json=dict(json or {}))
            return FakeResponse()

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)

    client = BusinessClient(get_settings())
    asyncio.run(client.call(TOOL_REGISTRY['execute_refund'],
                            {'user_id': 'user001', 'order_id': 'ORD-1001',
                             'proposal_id': 'RP-ABC123'}))

    assert captured['url'].endswith('/refunds/execute')
    assert captured['json']['proposal_id'] == 'RP-ABC123'
    assert captured['json']['order_id'] == 'ORD-1001'


def test_review_relay_uses_the_reviewer_credential(monkeypatch):
    """The relay must present the reviewer key, never the agent tool key."""
    import asyncio

    import httpx

    from gateway.app.core.governance import BusinessClient, TOOL_REGISTRY

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = '{}'

        def json(self) -> dict:
            return {'status': 'ok', 'decision': 'approve'}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=dict(headers or {}), json=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)

    client = BusinessClient(get_settings())
    asyncio.run(client.review(TOOL_REGISTRY['execute_refund'],
                              {'user_id': 'user001', 'order_id': 'ORD-1001'},
                              decision='approve', comment='同意'))

    assert captured['url'].endswith('/refunds/review')
    assert 'X-Reviewer-API-Key' in captured['headers']
    assert 'X-Tool-API-Key' not in captured['headers']
    assert captured['json']['decision'] == 'approve'
    assert captured['json']['order_id'] == 'ORD-1001'


def test_review_relay_refuses_to_run_without_a_credential(monkeypatch):
    """A missing reviewer key is a hard error, not a silently skipped step."""
    import asyncio

    from gateway.app.core.governance import BusinessClient, TOOL_REGISTRY

    settings = get_settings().model_copy(update={'reviewer_api_key': ''})
    client = BusinessClient(settings)
    with pytest.raises(ToolDenied) as exc:
        asyncio.run(client.review(TOOL_REGISTRY['execute_refund'],
                                  {'user_id': 'user001', 'order_id': 'ORD-1001'},
                                  decision='approve'))
    assert exc.value.reason == 'reviewer_credential_missing'


def test_approving_relays_the_decision_before_executing(
    client, dev_headers, reviewer_headers, business_stub
):
    """The order system only executes once *its* proposal is approved."""
    body = _start_refund_execution(client, dev_headers)
    decision = client.post(f"/api/approvals/{body['approval_id']}/decide",
                           headers=reviewer_headers,
                           json={'decision': 'approve', 'comment': '同意'})
    assert decision.status_code == 200
    assert decision.json()['status'] == 'approved'

    assert business_stub.reviews == [
        ('execute_refund', {'user_id': 'user001', 'order_id': 'ORD-1001',
                            'proposal_id': 'RP-TEST0001'}, 'approve')
    ]
    assert [name for name, _ in business_stub] == [_RULE_PROBE, _PROPOSE, 'execute_refund']


def test_rejecting_relays_the_rejection_and_executes_nothing(
    client, dev_headers, reviewer_headers, business_stub
):
    body = _start_refund_execution(client, dev_headers)
    decision = client.post(f"/api/approvals/{body['approval_id']}/decide",
                           headers=reviewer_headers,
                           json={'decision': 'reject', 'comment': '不符合条件'})
    assert decision.json()['status'] == 'rejected'
    assert business_stub.reviews == [
        ('execute_refund', {'user_id': 'user001', 'order_id': 'ORD-1001',
                            'proposal_id': 'RP-TEST0001'}, 'reject')
    ]
    assert [name for name, _ in business_stub] == [_RULE_PROBE, _PROPOSE]
