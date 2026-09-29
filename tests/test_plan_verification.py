# -*- coding: utf-8 -*-
"""Plan verification: the DAG, the type contracts, and fail-closed execution.

Two things are being tested here, and they are different in kind:

* **The verifier** — hand-built plans, one per rejection reason, including the
  adversarial ones. These plans cannot come out of the planner today; they are
  what the verifier exists to catch if the planner ever regresses.
* **The planner** — that *every* plan it can actually produce passes. A
  verifier that rejects legitimate plans is worse than none, because it turns
  a working refund into a refusal, and the obvious fix is to delete the check.

The refund chain gets the most attention because it is the only path where a
verification gap costs money rather than a wrong sentence.
"""
from __future__ import annotations

import pytest

from gateway.app.core import governance
from gateway.app.core import plan as plan_mod
from gateway.app.core.plan import (
    InputRef, Plan, PlanStep, Requirement, verify_plan,
)

REGISTRY = governance.TOOL_REGISTRY


def _p(*steps: PlanStep) -> Plan:
    return Plan(plan_id='plan-test', steps=steps, intent='test')


def _order(step_id='s1') -> PlanStep:
    return PlanStep(step_id, 'get_order', {'user_id': 'u', 'order_id': 'ORD-1001'})


def _eligibility(step_id='s1') -> PlanStep:
    return PlanStep(step_id, 'check_aftersale_eligibility',
                    {'user_id': 'u', 'order_id': 'ORD-1001'})


def _proposal(step_id='s2', requires=()) -> PlanStep:
    return PlanStep(step_id, 'create_refund_proposal',
                    {'user_id': 'u', 'order_id': 'ORD-1001', 'reason': '申请退款'},
                    requires=requires)


def _execute(step_id='s3', proposal_from='s2', order_from=None) -> PlanStep:
    return PlanStep(step_id, 'execute_refund', {'user_id': 'u', 'order_id': 'ORD-1001'},
                    inputs={
                        'proposal_id': InputRef(proposal_from, 'proposal_id'),
                        **({'order_id': InputRef(order_from, 'proposal_id')} if order_from else {}),
                    })


def _refund_chain() -> Plan:
    """The plan the planner builds for a confirmed refund: check → propose → execute."""
    return _p(_eligibility('s1'),
              _proposal('s2', requires=(Requirement('s1', 'eligible', False),)),
              _execute('s3'))


# --------------------------------------------------------------------------- #
# 1. Valid plans
# --------------------------------------------------------------------------- #
def test_an_empty_plan_is_valid():
    """Most turns call no tool at all — a knowledge question is the normal case.

    Treating "no tools" as malformed would reject the majority of traffic, so
    this is asserted rather than left to the reader.
    """
    result = verify_plan(_p(), REGISTRY)
    assert result.ok, result.detail
    assert result.steps == 0


def test_a_single_read_is_valid():
    assert verify_plan(_p(_order()), REGISTRY).ok


def test_the_three_step_refund_chain_is_valid():
    result = verify_plan(_refund_chain(), REGISTRY)
    assert result.ok, result.detail
    assert result.steps == 3


def test_the_plan_ledger_records_every_gate():
    """The ledger is what the Decision Inspector renders — it must be complete."""
    result = verify_plan(_refund_chain(), REGISTRY)
    names = [c.name for c in result.checks]
    for expected in ('unique_step_id', 'tool_and_arguments', 'literal_arguments',
                     'dependencies_resolve', 'acyclic', 'no_forward_reference',
                     'type_contracts', 'preconditions'):
        assert expected in names, expected
    assert all(c.ok for c in result.checks)


# --------------------------------------------------------------------------- #
# 2. Adversarial plans — one per rejection reason
# --------------------------------------------------------------------------- #
#: ``(label, plan, expected reason)``. Each row is a plan the planner must never
#: emit; if the verifier lets one through, it reaches a real tool.
ADVERSARIAL = [
    # -- tool identity ----------------------------------------------------- #
    ('unknown tool', _p(PlanStep('s1', 'delete_all_orders', {})), 'unknown_tool'),
    ('empty tool name', _p(PlanStep('s1', '', {})), 'unknown_tool'),

    # -- step identity ----------------------------------------------------- #
    ('duplicate step_id', _p(_order('s1'), _order('s1')), 'duplicate_step_id'),

    # -- arguments --------------------------------------------------------- #
    ('unknown argument', _p(PlanStep('s1', 'get_order',
                                     {'user_id': 'u', 'order_id': 'ORD-1', 'x': 1})),
     'unknown_argument'),
    ('missing required argument', _p(PlanStep('s1', 'get_order', {'user_id': 'u'})),
     'missing_argument'),
    ('literal violates contract', _p(PlanStep('s1', 'get_order',
                                              {'user_id': 'u', 'order_id': 'x'})),
     'invalid_arguments'),
    ('literal of the wrong type', _p(PlanStep('s1', 'get_order',
                                              {'user_id': 'u', 'order_id': 1001})),
     'invalid_arguments'),

    # -- graph shape ------------------------------------------------------- #
    ('self dependency', _p(PlanStep('s1', 'get_order', {'user_id': 'u'},
                                    inputs={'order_id': InputRef('s1', 'order_id')})),
     'self_dependency'),
    ('self requirement', _p(PlanStep('s1', 'create_refund_proposal',
                                     {'user_id': 'u', 'order_id': 'ORD-1001',
                                      'reason': 'xx'},
                                     requires=(Requirement('s1', 'eligible', False),))),
     'self_dependency'),
    ('dependency does not exist',
     _p(_execute('s3', proposal_from='s9')), 'missing_dependency'),
    ('requirement on a missing step', _p(
        _order('s1'),
        PlanStep('s2', 'create_refund_proposal',
                 {'user_id': 'u', 'order_id': 'ORD-1001', 'reason': 'xx'},
                 requires=(Requirement('s9', 'eligible', False),)),
    ), 'missing_dependency'),
    ('cycle a→b→a', _p(
        PlanStep('A', 'get_order', {'user_id': 'u'},
                 inputs={'order_id': InputRef('B', 'order_id')}),
        PlanStep('B', 'get_order', {'user_id': 'u'},
                 inputs={'order_id': InputRef('A', 'order_id')}),
    ), 'cycle_detected'),
    ('forward reference', _p(
        PlanStep('A', 'get_order', {'user_id': 'u'},
                 inputs={'order_id': InputRef('B', 'order_id')}),
        _order('B'),
    ), 'invalid_reference'),
    ('forward requirement', _p(
        PlanStep('A', 'create_refund_proposal',
                 {'user_id': 'u', 'order_id': 'ORD-1001', 'reason': 'xx'},
                 requires=(Requirement('B', 'eligible', False),)),
        _eligibility('B'),
    ), 'invalid_reference'),

    # -- type contracts ---------------------------------------------------- #
    ('missing output field', _p(
        _order('s1'),
        PlanStep('s2', 'create_ticket', {'user_id': 'u'},
                 inputs={'issue': InputRef('s1', 'no_such_field')}),
    ), 'missing_output'),
    ('float into a str argument', _p(
        _order('s1'),
        PlanStep('s2', 'create_ticket', {'user_id': 'u'},
                 inputs={'issue': InputRef('s1', 'amount')}),
    ), 'type_mismatch'),
    ('proposal_id into order_id', _p(
        _eligibility('s1'), _proposal('s2'), _execute('s3', order_from='s2'),
    ), 'type_mismatch'),
    ('order_id into proposal_id', _p(
        _eligibility('s1'),
        _proposal('s2', requires=(Requirement('s1', 'eligible', False),)),
        PlanStep('s3', 'execute_refund', {'user_id': 'u', 'order_id': 'ORD-1001'},
                 inputs={'proposal_id': InputRef('s2', 'order_id')}),
    ), 'type_mismatch'),
    ('requirement over a missing field', _p(
        _order('s1'),
        PlanStep('s2', 'create_refund_proposal',
                 {'user_id': 'u', 'order_id': 'ORD-1001', 'reason': 'xx'},
                 requires=(Requirement('s1', 'eligible', False),)),
    ), 'missing_output'),

    # -- sequence ---------------------------------------------------------- #
    ('execute_refund alone', _p(PlanStep('s1', 'execute_refund',
                                         {'user_id': 'u', 'order_id': 'ORD-1001',
                                          'proposal_id': 'RP-1'})),
     'invalid_sequence'),
    ('execute before propose', _p(
        PlanStep('A', 'execute_refund', {'user_id': 'u', 'order_id': 'ORD-1001',
                                         'proposal_id': 'RP-1'}),
        PlanStep('B', 'create_refund_proposal',
                 {'user_id': 'u', 'order_id': 'ORD-1001', 'reason': 'xx'}),
    ), 'invalid_sequence'),
    ('propose without asking the rule table', _p(_proposal('s1')), 'invalid_sequence'),
]


@pytest.mark.parametrize('label,plan,reason', ADVERSARIAL,
                         ids=[row[0] for row in ADVERSARIAL])
def test_adversarial_plans_are_rejected_for_the_right_reason(label, plan, reason):
    result = verify_plan(plan, REGISTRY)
    assert not result.ok, f'{label}: 应该被拒绝'
    assert result.reason == reason, f'{label}: 期望 {reason}，实际 {result.reason} ({result.detail})'


def test_the_adversarial_table_is_not_silently_empty():
    """A parametrized table that got emptied would make the suite green and mute."""
    assert len(ADVERSARIAL) >= 20, len(ADVERSARIAL)
    assert len({row[2] for row in ADVERSARIAL}) >= 10


def test_every_declared_reason_is_exercised():
    """Every code in REASONS must be reachable, or it is documentation, not a check.

    Computed from the verifier's *actual* verdicts, not from the table's
    declared expectations: reading the expected column would make this pass
    even when the plan produced a different reason — which is exactly how a
    rejection code ends up listed in the metrics label set and the docs while
    guarding nothing. (The first version of this test did read the expected
    column, and `self_dependency` was in fact unreachable at the time.)
    """
    produced = {verify_plan(plan, REGISTRY).reason for _, plan, _ in ADVERSARIAL}
    produced.discard('')
    missing = set(plan_mod.REASONS) - produced
    assert not missing, f'这些 reason 没有任何用例真的触发过: {sorted(missing)}'


# --------------------------------------------------------------------------- #
# 3. The nominal type contract
# --------------------------------------------------------------------------- #
def test_ids_are_not_interchangeable_even_though_all_are_strings():
    """``order_id`` and ``proposal_id`` are both ``str`` on the wire.

    A structural type system accepts either in either slot, which is exactly
    how "refund this order" turns into "refund against some other proposal".
    The nominal wrapper is what makes that a type error.
    """
    result = verify_plan(_p(_eligibility('s1'), _proposal('s2'), _execute('s3', order_from='s2')),
                         REGISTRY)
    assert result.reason == 'type_mismatch'
    assert 'expected=OrderId' in result.detail
    assert 'actual=ProposalId' in result.detail


def test_narrowing_a_nominal_id_to_str_is_allowed():
    """``OrderId`` → ``str`` loses no guarantee, so it must not be rejected."""
    assert plan_mod._assignable(plan_mod.OrderId, str)
    assert not plan_mod._assignable(str, plan_mod.OrderId)


def test_int_widens_to_float_but_bool_does_not_count_as_int():
    """``bool`` is an ``int`` subclass in Python; ``True`` as an amount is a bug."""
    assert plan_mod._assignable(int, float)
    assert not plan_mod._assignable(bool, int)
    assert not plan_mod._assignable(float, int)


# --------------------------------------------------------------------------- #
# 4. Requirements: three-valued, and absent never blocks
# --------------------------------------------------------------------------- #
def test_a_requirement_blocks_only_on_a_definite_negative():
    req = Requirement('s1', 'eligible', False)
    assert req.violated(False, present=True) is True
    assert req.violated(True, present=True) is False
    # ``None`` is "could not tell", which is not "no".
    assert req.violated(None, present=True) is False
    # Absent means the source step failed. A read timeout is not a refusal.
    assert req.violated(None, present=False) is False


def test_requirements_are_not_confused_by_truthiness():
    """``0`` must not be treated as ``False`` here — the comparison is identity."""
    req = Requirement('s1', 'count', False)
    assert req.violated(0, present=True) is False
    assert req.violated(False, present=True) is True


def test_dependencies_are_derived_from_references():
    """A step's dependencies are its actual references, never a separate field.

    Declaring them separately would let a plan claim ``depends_on: [s1]`` while
    reading from ``s2``, and then two callers would disagree about the graph.
    """
    step = _proposal('s2', requires=(Requirement('s1', 'eligible', False),))
    step = PlanStep(step.step_id, step.tool, step.arguments, step.inputs, step.requires)
    assert step.dependencies == ('s1',)

    both = PlanStep('s3', 'execute_refund',
                    {'user_id': 'u', 'order_id': 'ORD-1001'},
                    inputs={'proposal_id': InputRef('s2', 'proposal_id')},
                    requires=(Requirement('s1', 'eligible', False),))
    assert both.dependencies == ('s1', 's2')
    # A step is never its own dependency, even if it references itself — that is
    # reported as ``self_dependency`` rather than silently tolerated here.
    selfish = PlanStep('s1', 'get_order', {'user_id': 'u'},
                       inputs={'order_id': InputRef('s1', 'order_id')})
    assert selfish.dependencies == ()


# --------------------------------------------------------------------------- #
# 5. The planner's own plans must always verify
# --------------------------------------------------------------------------- #
#: Every shape ``plan_tools`` can produce, with the message that produces it.
PLANNER_CASES = [
    ('product + sku', {'intent': 'product', 'slots': {'sku_id': 'SKU-1001'}}, '还有货吗'),
    ('product without sku', {'intent': 'product', 'slots': {}}, '还有货吗'),
    ('order query', {'intent': 'order_query', 'slots': {'order_id': 'ORD-1001'}}, '订单到哪了'),
    ('order query without id', {'intent': 'order_query', 'slots': {}}, '订单到哪了'),
    ('refund, not confirmed', {'intent': 'refund', 'slots': {'order_id': 'ORD-1001'}}, '我要退款'),
    ('refund, confirmed', {'intent': 'refund', 'slots': {'order_id': 'ORD-1001'}}, '确认退款'),
    ('refund without order id', {'intent': 'refund', 'slots': {}}, '我要退款'),
    ('tech support with hint', {'intent': 'tech_support', 'slots': {}}, '帮我建工单'),
    ('tech support without hint', {'intent': 'tech_support', 'slots': {}}, '打不开了'),
    ('knowledge', {'intent': 'knowledge', 'slots': {}}, '退款多久到账'),
    ('smalltalk', {'intent': 'smalltalk', 'slots': {}}, '你好'),
    ('handoff', {'intent': 'handoff', 'slots': {}}, '转人工'),
]


@pytest.mark.parametrize('label,state,message', PLANNER_CASES,
                         ids=[row[0] for row in PLANNER_CASES])
def test_every_plan_the_planner_can_build_verifies(label, state, message):
    """The verifier must not reject legitimate plans.

    A false rejection here is a working refund turning into a refusal, and the
    tempting fix is to remove the check — so this is the guard on the guard.
    """
    from gateway.app.core import engine

    state = {**state, 'user_id': 'user001', 'business_customer_id': 'user001'}
    plan = engine.plan_tools(state, message)
    result = verify_plan(plan, REGISTRY)
    assert result.ok, f'{label}: {result.reason} — {result.detail}'


def test_the_confirmed_refund_plan_links_proposal_to_execute():
    """The whole point of the graph: step 3 consumes step 2's proposal id."""
    from gateway.app.core import engine

    plan = engine.plan_tools(
        {'intent': 'refund', 'slots': {'order_id': 'ORD-1001'},
         'user_id': 'u', 'business_customer_id': 'u'}, '确认退款')
    assert plan.tool_names() == ('check_aftersale_eligibility',
                                 'create_refund_proposal', 'execute_refund')
    execute = plan.steps[2]
    assert execute.inputs['proposal_id'].step_id == plan.steps[1].step_id
    assert execute.dependencies == (plan.steps[1].step_id,)


# --------------------------------------------------------------------------- #
# 6. Fail-closed: a rejected plan runs nothing
# --------------------------------------------------------------------------- #
def test_a_rejected_plan_produces_no_business_calls_at_all(
    client, dev_headers, business_stub, monkeypatch
):
    """The assertion this whole feature exists for.

    Governance validates each call *in isolation*, so it would happily run step
    1 and step 2 and only discover step 3 was incoherent — after two side
    effects. Verification happens before the first tool, so a bad plan must
    leave the business service untouched. Asserting the call list is empty is
    the only version of this that cannot be satisfied by accident.
    """
    from gateway.app.core import engine, plan as plan_mod

    def broken_plan(state, message, plan_id=''):
        return plan_mod.Plan('plan-broken', (
            plan_mod.PlanStep('s1', 'execute_refund',
                              {'user_id': 'user001', 'order_id': 'ORD-1001',
                               'proposal_id': 'RP-1'}),
        ), intent='refund')

    monkeypatch.setattr(engine, 'plan_tools', broken_plan)

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '确认退款 ORD-1001 现在就退'}).json()

    assert business_stub == [], '被拒绝的 plan 不应该产生任何业务调用'
    assert business_stub.reviews == []
    assert body['tool']['status'] == 'plan_rejected'
    assert body['tool']['reason'] == 'invalid_sequence'


def test_a_rejected_plan_still_answers_the_user(client, dev_headers,
                                                business_stub, monkeypatch):
    """Fail-closed must not mean fail-silent: the user still gets a reply."""
    from gateway.app.core import engine, plan as plan_mod

    monkeypatch.setattr(engine, 'plan_tools', lambda state, message, plan_id='':
                        plan_mod.Plan('plan-broken', (
                            plan_mod.PlanStep('s1', 'get_order', {'user_id': 'u'}),
                        )))

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '订单到哪了'}).json()

    assert body['answer'].strip()
    assert body['tool']['status'] == 'plan_rejected'
    assert business_stub == []


def test_a_rejected_plan_is_counted_and_queryable(client, dev_headers,
                                                  business_stub, monkeypatch):
    """A rejection must be visible in the trace, not only in a log line."""
    from gateway.app.core import engine, plan as plan_mod

    monkeypatch.setattr(engine, 'plan_tools', lambda state, message, plan_id='':
                        plan_mod.Plan('plan-broken', (
                            plan_mod.PlanStep('s1', 'get_order', {'user_id': 'u'}),
                        )))

    body = client.post('/api/chat', headers=dev_headers,
                       json={'message': '订单到哪了'}).json()
    conversation_id = body['conversation_id']

    detail = client.get(f'/api/conversations/{conversation_id}',
                        headers=dev_headers).json()
    plan_state = detail['messages'][-1].get('plan')
    assert plan_state is not None, 'turn 上应该记录 plan'
    assert plan_state['validation']['ok'] is False
    assert plan_state['validation']['reason'] == 'missing_argument'
    assert plan_state['steps'][0]['tool'] == 'get_order'
