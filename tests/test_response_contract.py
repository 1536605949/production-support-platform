# -*- coding: utf-8 -*-
"""The chat response must account for every public field of the turn result.

Pydantic **ignores undeclared keyword arguments**. So a field that exists on
:class:`TurnResult` but not on :class:`ChatResponse` produces no type error, no
runtime error and no log line — it simply never reaches the client. That is how
the verified plan (steps, dependency edges, the gate ledger) was missing from
every chat response in v2.6 while ``_to_response`` appeared to pass it straight
through: the operator console's decision inspector had nothing to render, the
evaluation gate could not assert on a plan, and the whole suite stayed green.

The first test below is the guard for that class — it is written against the
*field sets*, not against ``plan``, so the next field added to ``TurnResult``
cannot go missing quietly either. The second test proves the mechanism the guard
defends against is real, so that the guard does not read as paranoia and get
deleted by the next person who finds it inconvenient.
"""
from __future__ import annotations

from gateway.app.core.engine import TurnResult
from gateway.app.routers.chat import _to_response
from gateway.app.schemas import ChatResponse

#: Turn-result fields deliberately kept off the wire, each for a stated reason.
#: An entry here is a decision; an omission from both this set and the response
#: model is a bug, which is what the guard below reports.
INTERNAL = {
    # Internal failure text. It belongs in the trace and the log, not in a reply
    # a customer can read — and it is not in ``metadata`` for the same reason.
    'error',
}


def test_every_turn_result_field_reaches_the_client_or_is_declared_internal():
    """Either the response model declares it, or the response metadata carries it.

    Both routes are checked because both are legitimate: ``tokens_in`` and
    friends ride in ``metadata`` rather than as top-level fields, and that is a
    choice, not an omission. What is not legitimate is a field that takes
    neither route — which is exactly what happened to ``plan``.
    """
    response = _to_response(TurnResult(), 'req-test', 'trace-test')
    accounted = set(ChatResponse.model_fields) | set(response.metadata) | INTERNAL
    missing = set(TurnResult.__dataclass_fields__) - accounted
    assert not missing, (
        f'这些 TurnResult 字段既不在 ChatResponse 上、也不在 metadata 里: {sorted(missing)}。'
        f'Pydantic 会静默丢掉未声明的关键字参数——客户端永远看不到它们，而且不会报错。'
    )


def test_the_verified_plan_reaches_the_response_body():
    """The concrete regression: the decision record must survive serialisation.

    ``plan`` is what answers "why was this AI action allowed?" at the moment the
    answer is read. Dropping it turns the operator console's inspector into dead
    code that renders nothing, without failing anything.
    """
    plan = {
        'plan_id': 'plan-x',
        'intent': 'refund',
        'steps': [{'step_id': 'step-1', 'tool': 'get_order',
                   'depends_on': [], 'arguments': {}, 'inputs': {}}],
        'validation': {'ok': True, 'reason': '', 'detail': '', 'checks': []},
    }
    response = _to_response(TurnResult(answer='a', plan=plan), 'req-test', 'trace-test')

    assert response.plan == plan
    # The streaming endpoint sends ``final.model_dump()``, so the plan has to
    # survive the dump too — asserting on the attribute alone would leave the
    # SSE path unverified.
    assert response.model_dump()['plan'] == plan


def test_pydantic_really_does_ignore_undeclared_kwargs():
    """Why the guard above is a fix and not a style preference.

    If Pydantic ever starts rejecting unknown fields, this test fails — and that
    failure is good news: it means the silent-drop class is closed at the source
    and the guard above is merely belt and braces. Do not delete it to make the
    suite green; read the failure.
    """
    response = ChatResponse(request_id='r', trace_id='t', answer='a',
                            not_a_declared_field='dropped')
    assert not hasattr(response, 'not_a_declared_field')
    assert 'not_a_declared_field' not in response.model_dump()
