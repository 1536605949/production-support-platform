# -*- coding: utf-8 -*-
"""The evaluation gate's plan assertions, and the dataset that uses them.

``evaluation/run_eval.py`` grew four expectation keys in v2.6 —
``plan_status`` / ``plan_steps`` / ``plan_dependencies`` /
``plan_rejection_reason`` — so a badcase can pin the *graph* the verifier
approved, not just the tool that ran. This file is what keeps those keys honest,
in three separate ways, because they fail differently:

1. **The evaluator.** Driven with synthetic bodies, so every key is shown to
   both accept the right plan and reject the wrong one. A key that only ever
   passes is indistinguishable from a key that is ignored.
2. **The dataset.** Asserted to actually use the keys, and to keep using them.
   Deleting the plan assertions from the dataset would otherwise leave the suite
   green while the new surface went unverified.
3. **The one key no dataset case may use.** ``plan_rejection_reason`` is covered
   here and in ``test_plan_verification.py``, never in the dataset, because the
   planner is deterministic and cannot be made to emit an invalid plan from a
   chat message. A dataset case that claims otherwise is fabricated, and the
   test below says so out loud.

The live half of this layer is ``run_eval.py`` against a deployment; the
in-process half is ``test_eval_dataset.py``, which replays the same dataset on
the deterministic trunk. Both call :func:`evaluate`, so both inherit whatever
these keys check.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_EVAL = Path(__file__).parents[1] / 'evaluation' / 'run_eval.py'
_spec = importlib.util.spec_from_file_location('plan_assert_run_eval', _EVAL)
assert _spec and _spec.loader, f'无法加载 {_EVAL}'
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

CASES = run_eval.load_cases()

#: The keys this file exists to protect.
PLAN_KEYS = ('plan_status', 'plan_steps', 'plan_dependencies', 'plan_rejection_reason')


# --------------------------------------------------------------------------- #
# Synthetic bodies, in the exact shape the API returns
# --------------------------------------------------------------------------- #
def _plan(ok: bool = True, reason: str = '', steps=(), checks=()) -> dict:
    return {
        'plan_id': 'plan-test',
        'intent': 'order_query',
        'steps': [
            {'step_id': step_id, 'tool': tool, 'depends_on': list(depends_on),
             'arguments': {}, 'inputs': {}}
            for step_id, tool, depends_on in steps
        ],
        'validation': {'ok': ok, 'reason': reason, 'detail': '', 'checks': list(checks)},
    }


ORDER_PLAN = _plan(steps=[('step-1', 'get_order', [])])
EMPTY_PLAN = _plan()
REFUND_PLAN = _plan(steps=[
    ('step-1', 'check_aftersale_eligibility', []),
    ('step-2', 'create_refund_proposal', ['step-1']),
    ('step-3', 'execute_refund', ['step-2']),
])
REJECTED_PLAN = _plan(ok=False, reason='invalid_sequence',
                      steps=[('step-1', 'execute_refund', [])])


def _case(expect: dict) -> dict:
    return {'id': 'synthetic', 'expect': expect}


def _body(plan: dict | None, **extra) -> dict:
    body = {'answer': '（合成回答）', **extra}
    if plan is not None:
        body['plan'] = plan
    return body


# --------------------------------------------------------------------------- #
# 1. The keys accept the right plan
# --------------------------------------------------------------------------- #
def test_a_matching_plan_satisfies_all_four_keys_at_once():
    failures = run_eval.evaluate(
        _case({'plan_status': 'passed', 'plan_steps': ['get_order'],
               'plan_dependencies': {'step-1': []}}),
        _body(ORDER_PLAN),
    )
    assert failures == [], failures


def test_the_refund_graph_is_assertable_edge_by_edge():
    """The reason these keys exist: pinning the edge, not just the tool list.

    Step 3 must consume step 2's ``proposal_id``. A plan that still calls
    ``execute_refund`` but lost that edge would satisfy every pre-existing
    assertion in the dataset — same tool, same order, same answer — while
    turning the money path back into "refund against whatever was approved
    last".
    """
    failures = run_eval.evaluate(
        _case({'plan_status': 'passed',
               'plan_steps': ['check_aftersale_eligibility',
                              'create_refund_proposal', 'execute_refund'],
               'plan_dependencies': {'step-1': [], 'step-2': ['step-1'],
                                     'step-3': ['step-2']}}),
        _body(REFUND_PLAN),
    )
    assert failures == [], failures


def test_a_rejected_plan_is_assertable():
    """The key the dataset never uses is still a working key."""
    failures = run_eval.evaluate(
        _case({'plan_status': 'rejected', 'plan_steps': ['execute_refund'],
               'plan_rejection_reason': 'invalid_sequence'}),
        _body(REJECTED_PLAN),
    )
    assert failures == [], failures


def test_an_absent_plan_is_not_the_same_as_a_passing_one():
    """``handoff_level >= 2`` skips planning, so "no plan" needs its own value.

    Collapsing it into ``passed`` would make "the trunk never planned this turn"
    look like "the trunk planned it and the plan was fine" — the two are
    different claims, and only one of them is about the plan.
    """
    assert run_eval.evaluate(_case({'plan_status': None}), _body(None)) == []
    failures = run_eval.evaluate(_case({'plan_status': 'passed'}), _body(None))
    assert failures and '计划状态' in failures[0]


def test_an_empty_plan_is_a_passing_plan():
    """A knowledge question is the normal case, and it must not read as failure."""
    assert run_eval.evaluate(
        _case({'plan_status': 'passed', 'plan_steps': []}), _body(EMPTY_PLAN)
    ) == []


# --------------------------------------------------------------------------- #
# 2. Every key can also fail — and says what it saw
# --------------------------------------------------------------------------- #
NEGATIVES = [
    ('状态',
     {'plan_status': 'rejected'},
     _body(ORDER_PLAN),
     '计划状态'),
    ('步骤',
     {'plan_steps': ['get_sku']},
     _body(ORDER_PLAN),
     '计划步骤'),
    ('依赖',
     {'plan_dependencies': {'step-1': ['step-9']}},
     _body(ORDER_PLAN),
     '计划依赖'),
    ('拒绝原因',
     {'plan_rejection_reason': 'cycle_detected'},
     _body(ORDER_PLAN),
     '计划拒绝原因'),
]


@pytest.mark.parametrize('label,expect,body,needle', NEGATIVES,
                         ids=[row[0] for row in NEGATIVES])
def test_a_wrong_expectation_fails_and_names_the_observed_value(label, expect, body, needle):
    """``r1 failed`` is useless. The message has to carry both sides."""
    failures = run_eval.evaluate(_case(expect), body)
    assert failures, f'{label}: 期望不符却通过了'
    assert needle in failures[0], failures
    # Both the expected and the observed value must appear, or the report is a
    # riddle for whoever reads it after the run.
    assert '期望' in failures[0], failures


def test_the_negative_table_is_not_silently_empty():
    assert len(NEGATIVES) >= 4
    assert {row[3] for row in NEGATIVES} == {'计划状态', '计划步骤', '计划依赖', '计划拒绝原因'}


def test_plan_assertions_do_not_fire_when_the_case_does_not_ask_for_them():
    """A case that says nothing about the plan must not be judged on it.

    Otherwise every legacy case starts failing the moment the trunk changes
    shape, and the dataset becomes something people delete rather than fix.
    """
    assert run_eval.evaluate(_case({'tool': None}), _body(REJECTED_PLAN)) == []


# --------------------------------------------------------------------------- #
# 3. The dataset actually uses them
# --------------------------------------------------------------------------- #
def _plan_asserting_cases() -> list[dict]:
    return [c for c in CASES if set(c.get('expect') or {}) & set(PLAN_KEYS)]


def test_the_dataset_asserts_on_plans():
    """A floor, not a count: the point is that this surface cannot be dropped.

    Without it, someone tidying the dataset could remove every plan assertion
    and the suite would stay green — the new keys would be exercised only by the
    synthetic bodies in this file, which is not where a routing regression
    shows up.
    """
    count = len(_plan_asserting_cases())
    assert count >= 15, f'只有 {count} 条用例断言了 plan——这个面实际上没人守了'


def test_every_case_that_pins_a_tool_also_pins_the_plan_that_contains_it():
    """Pinning the tool but not the graph is half an assertion.

    If a case is specific enough to name ``get_order``, it is specific enough to
    say the plan was exactly one ``get_order`` step — and that is the difference
    between "the right tool ran" and "the right tool ran in the right graph".
    """
    offenders = []
    for case in CASES:
        expect = case.get('expect') or {}
        tool = expect.get('tool')
        if not tool:
            continue
        if tool not in (expect.get('plan_steps') or []):
            offenders.append(case['id'])
    assert not offenders, (
        f'这些用例钉了工具却没钉计划步骤: {offenders}——'
        f'工具对了但图错了是看不见的'
    )


def test_every_plan_asserting_case_also_asserts_the_plan_passed():
    """``plan_steps`` alone does not say the plan was accepted.

    Reading a graph out of a *rejected* plan would let a case pass while the
    turn called nothing at all.
    """
    offenders = [c['id'] for c in _plan_asserting_cases()
                 if 'plan_status' not in (c.get('expect') or {})]
    assert not offenders, f'{offenders} 断言了计划内容却没断言计划状态'


def test_no_dataset_case_claims_to_reject_a_plan():
    """Deliberate, and worth stating rather than leaving as an absence.

    ``plan_tools`` is deterministic code, so an invalid plan means a bug in the
    planner or a drift between a tool's declared outputs and the consuming step.
    No chat message provokes one. A case that asserts ``plan_rejection_reason``
    would therefore be either fabricated or flaky, and both are worse than the
    gap — ``test_plan_verification.py`` covers rejections with hand-built plans.

    If the planner is ever replaced by something non-deterministic, delete this
    test on purpose and add real rejection cases; do not weaken it.
    """
    offenders = [c['id'] for c in CASES
                 if 'plan_rejection_reason' in (c.get('expect') or {})]
    assert offenders == [], (
        f'{offenders} 声称能触发计划被拒——确定性 planner 做不到这件事，'
        f'请先确认这条用例不是编出来的'
    )
