#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Badcase regression gate.

What this is for
----------------
``pytest`` proves the code still does what it was written to do. It cannot prove
the **answers** are still as good, because answer quality depends on the
knowledge base, the intent thresholds, the model, and the prompt — none of which
are pinned by a unit test. This script is that missing layer: a fixed set of
cases, each with expectations, run against a live deployment.

Two defects this file was written to fix
----------------------------------------
The previous version authenticated with an ``X-Demo-User`` header. That only
works in ``AUTH_MODE=dev``; the deployment runs ``AUTH_MODE=jwt``, so against the
real thing **every case failed on 401** — a regression gate that could only ever
run somewhere nobody deploys. It now logs in for real and falls back to the dev
header only when explicitly asked.

It also only asserted on the answer *text* (``must_contain``). An answer can be
worded perfectly while the system took the wrong path to it — asked the wrong
tool, skipped a handoff it should have made, or silently degraded to the
template. Those are invisible in the text, and they are exactly the regressions
worth catching, so each case may now assert on the **trajectory**: intent, tool,
tool status, handoff level, degradation, and the **plan**.

Why the plan is asserted separately from the tool
-------------------------------------------------
``expect.tool`` names the tool that *ran*. It cannot see the graph the tool ran
inside. For the refund path the graph is the whole point: step 3 may only
consume the ``proposal_id`` that step 2 produced, and if that edge is dropped the
turn still calls ``execute_refund`` and still looks right in the audit trail —
it just does so against a proposal the caller named by hand. So the plan gets its
own assertions (``plan_status`` / ``plan_steps`` / ``plan_dependencies`` /
``plan_rejection_reason``), and they are checked against the *verified* graph
rather than the observed side effects.

``plan_status: null`` is a supported value, not a missing one: the trunk skips
planning entirely once ``handoff_level >= 2``, so "there is no plan" is the
correct expectation for a turn that must escalate. No dataset case pins it —
which level the frustration counter lands on is not stable enough to assert —
so ``tests/test_eval_plan_assertions.py`` covers it with a synthetic body. A
supported key that nothing exercises is untested code, and this one is exactly
the kind that would rot silently.

One thing these assertions deliberately cannot cover here: a *rejected* plan.
The planner is deterministic code, so an invalid plan means a bug in the planner
or a drift between a tool's declared outputs and the step that consumes them —
not something a chat message can provoke. ``tests/test_plan_verification.py``
covers the rejection path directly; this file covers the plans that reach users.

What this deliberately does NOT cover
-------------------------------------
The approval trust chain (propose -> park -> human decides -> execute). Those
cases move real state in the demo order system, and the second run would find
the order already refunded — which is why ``acceptance_probe.py`` owns them and
resets the demo data first. Keeping them out of here means this gate is
repeatable: run it as often as you like, nothing is consumed.

Usage
-----
    # against a deployment (JWT)
    python3 evaluation/run_eval.py https://124.222.190.3/support

    # against a local dev-mode instance
    EVAL_DEV_USER=user001 python3 evaluation/run_eval.py http://localhost:8080

    # record the current results as the baseline to compare against
    python3 evaluation/run_eval.py <base> --update-baseline

Exit code is 0 only when no critical case fails and nothing regressed against
the baseline. Stdlib only, so CI needs no extra installs.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent


def _base_from_argv() -> str:
    """First ``http(s)://`` argument wins.

    Not ``sys.argv[1]``: this module is also imported by the pytest suite, where
    ``argv[1]`` is a pytest flag such as ``-q``. Taking it positionally turned
    every in-process run into a request against a host called ``-q``.
    """
    for arg in sys.argv[1:]:
        if arg.startswith(('http://', 'https://')):
            return arg.rstrip('/')
    return os.getenv('EVAL_BASE_URL', 'http://localhost:8080').rstrip('/')


BASE = _base_from_argv()

TENANT = os.getenv('EVAL_TENANT', 'tenant-demo')
EMAIL = os.getenv('EVAL_EMAIL', 'customer@demo.local')
PASSWORD = os.getenv('EVAL_PASSWORD', 'DemoCustomer123')
# Set this to skip login entirely (only meaningful against AUTH_MODE=dev).
DEV_USER = os.getenv('EVAL_DEV_USER', '')

# The deployment uses a self-signed certificate (no domain yet). This is a
# probe, not a security control.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

BASELINE = HERE / 'baseline.json'
REPORT = HERE / 'latest_report.json'


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def call(method: str, path: str, body: dict | None = None, token: str = '') -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f'{BASE}{path}', data=data, method=method)
    request.add_header('Content-Type', 'application/json')
    if token:
        request.add_header('Authorization', f'Bearer {token}')
    elif DEV_USER:
        request.add_header('X-Demo-User', DEV_USER)
    try:
        with urllib.request.urlopen(request, timeout=130, context=CTX) as response:
            return response.status, json.loads(response.read().decode('utf-8', 'replace') or 'null')
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode('utf-8', 'replace')
        try:
            return exc.code, json.loads(payload or 'null')
        except json.JSONDecodeError:
            return exc.code, {'detail': payload}


def authenticate() -> str:
    if DEV_USER:
        print(f'鉴权: dev 头 X-Demo-User={DEV_USER}（仅 AUTH_MODE=dev 可用）')
        return ''
    status, body = call('POST', '/api/auth/login',
                        {'tenant_id': TENANT, 'email': EMAIL, 'password': PASSWORD})
    if status != 200 or 'access_token' not in (body or {}):
        raise SystemExit(
            f'登录失败: {status} {body}\n'
            f'若目标实例是 AUTH_MODE=dev，请设 EVAL_DEV_USER=user001 后重跑。'
        )
    print(f'鉴权: JWT（{EMAIL} @ {TENANT}）')
    return body['access_token']


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
def load_cases() -> list[dict]:
    cases: list[dict] = []
    for path in sorted(HERE.glob('*_dataset.jsonl')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f'{path.name}:{number} 不是合法 JSON: {exc}') from exc
            case.setdefault('critical', False)
            cases.append(case)
    if not cases:
        raise SystemExit('没有找到任何 *_dataset.jsonl 用例')
    return cases


def evaluate(case: dict, body: dict) -> list[str]:
    """Return the list of failed expectations (empty = the case passed).

    Every failure is reported with the observed value, because "r1 failed" is
    useless when you are trying to work out whether the intent drifted or the
    wording did.
    """
    failures: list[str] = []
    answer = (body.get('answer') or '')
    lowered = answer.lower()
    tool = body.get('tool') or {}
    expect = case.get('expect') or {}

    # -- answer text (backwards compatible with the top-level keys) -------- #
    for needle in case.get('must_contain', []):
        if needle.lower() not in lowered:
            failures.append(f'回答缺少「{needle}」')
    for needle in case.get('must_not_contain', []):
        if needle.lower() in lowered:
            failures.append(f'回答出现了不该有的「{needle}」')

    # -- trajectory -------------------------------------------------------- #
    # The point of these: a well-worded answer can still have taken the wrong
    # path, and the path is what determines whether the system is trustworthy.
    if 'intent' in expect:
        allowed = expect['intent']
        if allowed is not None and body.get('intent') not in allowed:
            failures.append(f'意图={body.get("intent")!r}，期望 {allowed}')

    if 'tool' in expect:
        expected_tool = expect['tool']
        if expected_tool is None:
            if tool.get('tool'):
                failures.append(f'不该调用工具，实际调用了 {tool.get("tool")!r}')
        elif tool.get('tool') != expected_tool:
            failures.append(f'工具={tool.get("tool")!r}，期望 {expected_tool!r}')

    if 'tool_status' in expect and tool.get('tool'):
        if tool.get('status') != expect['tool_status']:
            failures.append(f'工具状态={tool.get("status")!r}，期望 {expect["tool_status"]!r}')

    if 'handoff_level' in expect:
        actual = int(body.get('handoff_level') or 0)
        if actual != expect['handoff_level']:
            failures.append(f'转人工级别={actual}，期望 {expect["handoff_level"]}')

    # ``handoff_min`` for cases where escalation is required but the *level* is
    # an implementation detail. Pinning the exact level would make the gate fail
    # on a harmless renumbering, which trains people to ignore it.
    if 'handoff_min' in expect:
        actual = int(body.get('handoff_level') or 0)
        if actual < expect['handoff_min']:
            failures.append(f'转人工级别={actual}，期望至少 {expect["handoff_min"]}')

    if 'degraded' in expect:
        actual = bool(body.get('degraded'))
        if actual != expect['degraded']:
            failures.append(f'降级={actual}，期望 {expect["degraded"]}')

    # -- plan -------------------------------------------------------------- #
    # Asserted against the graph the verifier *approved before the first tool
    # ran*, not against what the tools then did. That distinction is what makes
    # these assertions state-independent: the refund plan is the same three
    # steps whether or not ORD-1001 happens to be refundable today, because the
    # plan is built from (intent, slots, phrasing) and the eligibility verdict
    # only gates execution. A case can therefore pin the money path's shape
    # without moving any state.
    plan = body.get('plan') or None
    steps = (plan or {}).get('steps') or []
    validation = (plan or {}).get('validation') or {}

    if 'plan_status' in expect:
        actual = None if plan is None else ('passed' if validation.get('ok') else 'rejected')
        if actual != expect['plan_status']:
            failures.append(f'计划状态={actual!r}，期望 {expect["plan_status"]!r}')

    if 'plan_steps' in expect:
        actual = [step.get('tool') for step in steps]
        if actual != expect['plan_steps']:
            failures.append(f'计划步骤={actual}，期望 {expect["plan_steps"]}')

    if 'plan_dependencies' in expect:
        actual = {step.get('step_id'): list(step.get('depends_on') or []) for step in steps}
        if actual != expect['plan_dependencies']:
            failures.append(f'计划依赖={actual}，期望 {expect["plan_dependencies"]}')

    if 'plan_rejection_reason' in expect:
        actual = validation.get('reason') or ''
        if actual != expect['plan_rejection_reason']:
            failures.append(f'计划拒绝原因={actual!r}，期望 {expect["plan_rejection_reason"]!r}')

    return failures


def run_case(case: dict, token: str) -> dict:
    """Drive one case, optionally over several turns, and score the last reply.

    Multi-turn support exists because some behaviour only appears across turns:
    frustration handoff needs three consecutive dissatisfied messages, and a
    single-message harness cannot reach it. ``turns`` reuses one conversation so
    the trunk sees the history; without it the case is a single message.
    """
    started = time.perf_counter()
    turns = case.get('turns') or [case['question']]
    conversation_id = ''
    body: dict = {}
    try:
        for text in turns:
            payload: dict = {'message': text}
            if conversation_id:
                payload['conversation_id'] = conversation_id
            status, body = call('POST', '/api/chat', payload, token=token)
            if status != 200:
                detail = (body or {}).get('detail') or (body or {}).get('error') or body
                return {'id': case['id'], 'pass': False,
                        'failures': [f'HTTP {status}: {str(detail)[:200]}'],
                        'latency_s': round(time.perf_counter() - started, 3)}
            conversation_id = (body or {}).get('conversation_id') or conversation_id
    except Exception as exc:  # noqa: BLE001 - a transport failure is a failed case
        return {'id': case['id'], 'pass': False, 'failures': [f'请求失败: {exc}'],
                'latency_s': round(time.perf_counter() - started, 3)}

    latency = round(time.perf_counter() - started, 3)
    failures = evaluate(case, body or {})
    # ``observed`` is what makes a failure diagnosable after the fact: the
    # report is the only record of a run, so it has to carry the plan shape and
    # not just the tool name — otherwise "计划步骤=[...] 与期望不符" leaves the
    # reader guessing what the actual graph was.
    plan = (body or {}).get('plan') or None
    validation = (plan or {}).get('validation') or {}
    return {
        'id': case['id'],
        'kind': case.get('kind', ''),
        'critical': bool(case.get('critical')),
        'pass': not failures,
        'failures': failures,
        'latency_s': latency,
        'observed': {
            'intent': body.get('intent'),
            'confidence': body.get('confidence'),
            'intent_source': body.get('intent_source'),
            'tool': (body.get('tool') or {}).get('tool'),
            'tool_status': (body.get('tool') or {}).get('status'),
            'handoff_level': body.get('handoff_level'),
            'degraded': body.get('degraded'),
            'plan_status': None if plan is None else ('passed' if validation.get('ok') else 'rejected'),
            'plan_steps': [step.get('tool') for step in (plan or {}).get('steps') or []],
        },
    }


# --------------------------------------------------------------------------- #
# Baseline comparison
#
# A gate that only reports "18/20 passed" cannot tell you whether 18 is good or
# whether it was 20 last week. Recording the per-case outcome makes a *drop* the
# signal, which is what "regression" actually means.
# --------------------------------------------------------------------------- #
def load_baseline() -> dict | None:
    if not BASELINE.exists():
        return None
    try:
        return json.loads(BASELINE.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return None


def main() -> int:
    cases = load_cases()
    print(f'=== 评测门禁: {BASE} ===')
    print(f'用例 {len(cases)} 条\n')
    token = authenticate()
    print()

    results = [run_case(case, token) for case in cases]

    for result in results:
        mark = '\033[32mPASS\033[0m' if result['pass'] else '\033[31mFAIL\033[0m'
        print(f'  [{mark}] {result["id"]:6s} {result["latency_s"]:5.2f}s  '
              + ('' if result['pass'] else '; '.join(result['failures'])[:160]))

    passed = sum(1 for r in results if r['pass'])
    critical_failed = [r['id'] for r in results if not r['pass'] and r.get('critical')]
    failed = {r['id'] for r in results if not r['pass']}

    # -- regression against the baseline ----------------------------------- #
    baseline = load_baseline()
    regressions: list[str] = []
    if baseline:
        previously_passing = {k for k, v in (baseline.get('cases') or {}).items() if v}
        regressions = sorted(previously_passing & failed)
        if regressions:
            print(f'\n相对基线新失败的用例: {regressions}')
    else:
        print('\n没有基线文件——本次结果只作为自检，无法判断是否变差。')

    REPORT.write_text(json.dumps({
        'base_url': BASE, 'passed': passed, 'total': len(results),
        'critical_failures': critical_failed, 'regressions': regressions,
        'cases': results,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    if '--update-baseline' in sys.argv:
        BASELINE.write_text(json.dumps({
            'base_url': BASE,
            'cases': {r['id']: r['pass'] for r in results},
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'已写入基线: {BASELINE}')

    print(f'\n=== {passed}/{len(results)} 通过'
          + (f'，关键失败 {len(critical_failed)}' if critical_failed else '')
          + (f'，回归 {len(regressions)}' if regressions else '') + ' ===')
    print(f'报告: {REPORT}')

    if critical_failed or regressions:
        return 1
    print('无关键失败、无回归 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
