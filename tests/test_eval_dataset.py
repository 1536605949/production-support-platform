# -*- coding: utf-8 -*-
"""The badcase dataset, enforced on every CI run.

``evaluation/run_eval.py`` is the live gate: it drives a real deployment, with
the real model, and compares against a recorded baseline. That is the right tool
for catching drift in answers — but it needs a running stack, so it can only run
from the deploy runbook.

This file closes the other half of the gap. It replays **the same dataset**
against the in-process app, on the deterministic trunk, with the model switched
off. No infrastructure, so it runs in CI on every commit, and it catches the
regressions that have nothing to do with the model:

* a routing rule that starts sending knowledge questions to a tool
* an ownership check that stops denying another user's order
* the frustration counter no longer reaching handoff
* an injection that starts leaking the system prompt

What is deliberately NOT asserted here
--------------------------------------
``expect.degraded``. With ``LLM_ENABLED=0`` every answer takes the template
path, so asserting ``degraded is False`` would fail for the wrong reason. Whether
the model actually ran is a property of the live path, and
``scripts/llm_probe.py`` plus the live ``run_eval.py`` own it. Silently dropping
the assertion in both places would be worse than dropping it in one, so it is
dropped here and documented.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_EVAL = Path(__file__).parents[1] / 'evaluation' / 'run_eval.py'
_spec = importlib.util.spec_from_file_location('eval_run_eval', _EVAL)
assert _spec and _spec.loader, f'无法加载 {_EVAL}'
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

CASES = run_eval.load_cases()
# Keys this layer cannot meaningfully assert (see module docstring).
_LIVE_ONLY_KEYS = {'degraded'}


def _in_process_body(client, headers, case: dict) -> dict:
    """Replay a case through the in-process app, honouring multi-turn cases."""
    turns = case.get('turns') or [case['question']]
    conversation_id = ''
    body: dict = {}
    for text in turns:
        payload: dict = {'message': text}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        response = client.post('/api/chat', json=payload, headers=headers)
        assert response.status_code == 200, f'{case["id"]}: {response.text[:300]}'
        body = response.json()
        conversation_id = body.get('conversation_id') or conversation_id
    return body


@pytest.mark.parametrize('case', CASES, ids=[c['id'] for c in CASES])
def test_badcase_dataset(client, dev_headers, case):
    # Some cases cannot be judged here, and pretending otherwise is worse than
    # skipping: with no business service on the wire every tool call fails, so a
    # case asserting ``tool_status == 'failed'`` would pass because the service
    # is absent, not because the ownership check denied it. Each such case names
    # the dependency it needs, so the skip reason says what is actually missing.
    if case.get('live_only'):
        pytest.skip(f'仅真实链路可验（依赖 {case["live_only"]}）: {case.get("live_only_reason", "")}')

    body = _in_process_body(client, dev_headers, case)

    # Strip the expectations this layer cannot judge, so the shared evaluator
    # does not report a failure that says nothing about the trunk.
    trimmed = dict(case)
    trimmed['expect'] = {
        key: value for key, value in (case.get('expect') or {}).items()
        if key not in _LIVE_ONLY_KEYS
    }
    failures = run_eval.evaluate(trimmed, body)
    assert not failures, (
        f'{case["id"]} 在确定性主干上失败: ' + '; '.join(failures)
        + f'\n  实际轨迹: intent={body.get("intent")!r} '
          f'tool={(body.get("tool") or {}).get("tool")!r} '
          f'status={(body.get("tool") or {}).get("status")!r} '
          f'handoff={body.get("handoff_level")}'
    )


def test_dataset_ids_are_unique():
    ids = [case['id'] for case in CASES]
    assert len(ids) == len(set(ids)), f'用例 id 重复: {[i for i in ids if ids.count(i) > 1]}'


def test_every_case_declares_something_to_check():
    """A case with no assertions would pass forever and inflate the pass rate.

    This is the failure mode that makes a regression suite stop being trusted:
    someone adds a case, forgets the expectations, and it shows up green.
    """
    for case in CASES:
        expect = case.get('expect') or {}
        assert (
            expect or case.get('must_contain') or case.get('must_not_contain')
        ), f'{case["id"]} 没有任何断言，它会永远通过'


def test_critical_cases_cover_the_high_risk_kinds():
    """Guard the guard: the dataset must keep testing safety, not just FAQ.

    Deleting the tenancy or security cases would leave the suite green while it
    stopped protecting the things that actually matter.
    """
    critical_kinds = {case.get('kind') for case in CASES if case.get('critical')}
    for required in ('security', 'tenancy', 'business'):
        assert required in critical_kinds, (
            f'没有 critical 级别的 {required!r} 用例——安全相关的高危场景失去了回归保护'
        )


def test_most_cases_run_without_live_dependencies():
    """A case may be marked ``live_only``, but not all of them.

    Marking cases live-only is how a suite quietly stops running in CI: each
    individual skip looks justified, and the total coverage reaches zero. This
    floor makes that visible — it is the only thing standing between "some cases
    need a real stack" and "no cases need a real stack".
    """
    runnable = [c for c in CASES if not c.get('live_only')]
    assert len(runnable) >= 10, (
        f'只有 {len(runnable)} 条用例能在 CI 里跑——live_only 用得太多了，'
        f'回归保护实际上已经失效'
    )


def test_live_only_cases_name_their_missing_dependency():
    """A skip must say what is missing, or it reads as an unexplained omission."""
    allowed = {'business_service', 'llm'}
    for case in CASES:
        dependency = case.get('live_only')
        if not dependency:
            continue
        assert dependency in allowed, (
            f'{case["id"]} 的 live_only={dependency!r} 不是已知依赖 {sorted(allowed)}'
        )
        assert case.get('live_only_reason'), (
            f'{case["id"]} 标了 live_only 但没写原因——跳过就成了无声的遗漏'
        )
