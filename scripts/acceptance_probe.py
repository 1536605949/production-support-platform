#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end acceptance probe for a deployed support platform.

Talks to the *public* URL (through the edge nginx and Caddy), not to the
container network, so it exercises the same path a browser does — including
TLS, the ``/support/`` prefix rewrite, and the SSE proxy settings. A probe that
only reaches the gateway container would pass while the edge config was broken.

Stdlib only (no ``requests``) so it runs on any host with Python 3.10+.

Usage::

    python acceptance_probe.py https://124.222.190.3/support

Exit code is 0 only when every check passes.
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'https://124.222.190.3/support').rstrip('/')
TENANT = 'tenant-demo'
CUSTOMER = ('customer@demo.local', 'DemoCustomer123')
REVIEWER = ('reviewer@demo.local', 'DemoReviewer123')

# The deployment uses a self-signed certificate (no domain yet), so certificate
# verification is intentionally off. This is a probe, not a security control.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PASS, FAIL, SKIP = [], [], []


def check(name: str, ok: bool, detail: str = '') -> bool:
    (PASS if ok else FAIL).append(name)
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f' — {detail}' if detail else ''))
    return ok


def note(message: str) -> None:
    """Record a deliberate skip.

    The refund flow moves real money in the demo order system, so a second run
    against the same deployment finds the order already refunded. Skipping is
    honest; silently re-asserting would either fail spuriously or, worse, pass
    without testing anything.
    """
    SKIP.append(message)
    print(f'  [SKIP] {message}')


def call(method: str, path: str, body: Any = None, token: str = '',
         raw: bool = False) -> tuple[int, Any]:
    url = f'{BASE}{path}'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as res:
            payload = res.read().decode('utf-8', 'replace')
            return res.status, payload if raw else json.loads(payload or 'null')
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode('utf-8', 'replace')
        try:
            return exc.code, json.loads(payload or 'null')
        except json.JSONDecodeError:
            return exc.code, payload


def login(email: str, password: str) -> str:
    status, body = call('POST', '/api/auth/login',
                        {'tenant_id': TENANT, 'email': email, 'password': password})
    if status != 200:
        raise SystemExit(f'login failed for {email}: {status} {body}')
    return body['access_token']


def main() -> int:
    print(f'\n=== 端到端验收: {BASE} ===\n')

    # -- 1. edge + static ------------------------------------------------- #
    print('[1] 边缘入口与静态资源')
    status, html = call('GET', '/', raw=True)
    check('静态首页 200', status == 200, f'status={status}')
    check('返回的是前端页面', isinstance(html, str) and '<html' in html[:400].lower(),
          repr(html[:60]) if isinstance(html, str) else type(html).__name__)
    status, body = call('GET', '/api/healthz')
    check('/api/healthz 存活', status == 200 and body.get('status') == 'ok', str(body))
    status, body = call('GET', '/api/readyz')
    check('/api/readyz 就绪', status == 200 and body.get('status') == 'ready', str(body))
    check('引擎为确定性主干', body.get('engine_mode') == 'builtin')
    check('鉴权为 jwt', body.get('auth_mode') == 'jwt')
    check('数据库连通', (body.get('checks') or {}).get('database') is True)

    # -- 2. auth boundary -------------------------------------------------- #
    print('\n[2] 鉴权边界')
    status, _ = call('POST', '/api/chat', {'message': '你好'})
    check('未带令牌被拒 401', status == 401, f'status={status}')
    status, _ = call('POST', '/api/auth/login',
                     {'tenant_id': TENANT, 'email': CUSTOMER[0], 'password': 'wrong-password'})
    check('错误口令被拒 401', status == 401, f'status={status}')
    status, _ = call('POST', '/api/auth/login',
                     {'tenant_id': 'tenant-does-not-exist',
                      'email': CUSTOMER[0], 'password': CUSTOMER[1]})
    check('未知租户被拒', status in (401, 403, 404), f'status={status}')

    token = login(*CUSTOMER)
    status, me = call('GET', '/api/auth/me', token=token)
    check('/auth/me 返回租户', status == 200 and me.get('tenant_id') == TENANT, str(me))
    check('角色含 customer', 'customer' in (me.get('roles') or []))

    # -- 3. deterministic trunk -------------------------------------------- #
    # ``turns_sent`` counts every chat turn aimed at the *same* conversation.
    # Section 7 derives its persistence floor from this counter instead of a
    # hardcoded number: the approval section is skipped on re-runs (the demo
    # order is already refunded), and a magic ``>= 8`` turned that legitimate
    # skip into a spurious red — the exact false negative ``note()`` exists to
    # avoid. A dropped write still trips the check, because the floor tracks
    # what this run actually sent.
    print('\n[3] 确定性意图路由与知识库')
    turns_sent = 0
    status, body = call('POST', '/api/chat', {'message': '退款一般多久到账？'}, token=token)
    check('对话 200', status == 200, f'status={status}')
    check('意图判定为 refund', body.get('intent') == 'refund',
          f"intent={body.get('intent')} conf={body.get('confidence')}")
    check('置信度高于 LLM 阈值', float(body.get('confidence') or 0) >= 0.55,
          str(body.get('confidence')))
    check('意图来自确定性规则', body.get('intent_source') in ('rule', 'deterministic'),
          str(body.get('intent_source')))
    check('命中知识库并给出依据', len(body.get('sources') or []) > 0,
          f"{len(body.get('sources') or [])} 条")
    check('回答非空', bool((body.get('answer') or '').strip()))
    check('未误触发转人工', (body.get('handoff_level') or 0) == 0)
    conversation_id = body.get('conversation_id') or ''
    check('返回会话 id', bool(conversation_id))
    turns_sent += 1

    # -- 4. identity mapping (the bug this probe exists to catch) ---------- #
    print('\n[4] 平台身份 → 订单系统身份映射')
    status, body = call('POST', '/api/chat',
                        {'message': '帮我查一下订单 ORD-1001 到哪了', 'conversation_id': conversation_id},
                        token=token)
    turns_sent += 1
    tool = body.get('tool') or {}
    check('触发 get_order 工具', tool.get('tool') == 'get_order', str(tool.get('tool')))
    check('工具执行成功（非越权拒绝）', tool.get('status') == 'executed',
          f"status={tool.get('status')} detail={str(tool.get('detail'))[:80]}")
    result = tool.get('result') or {}
    check('返回的订单号正确', result.get('order_id') == 'ORD-1001', str(result)[:120])

    # -- 5. governance: high-risk parks for approval ----------------------- #
    print('\n[5] 治理：高危操作进入人工审批')
    refundable = ((body.get('tool') or {}).get('result') or {}).get('refund_available')
    approval_id = ''

    if not refundable:
        note('订单 ORD-1001 已被上一次验收退款，跳过高危审批闭环。'
             '完整重跑请先重置业务库：见 docs/OPERATIONS.md「重置演示数据」')
    else:
        status, body = call('POST', '/api/chat',
                            {'message': '订单 ORD-1001 有问题，我要退款',
                             'conversation_id': conversation_id},
                            token=token)
        turns_sent += 1
        tool = body.get('tool') or {}
        check('低危退款申请被受理', tool.get('tool') == 'create_refund_proposal',
              str(tool.get('tool')))
        check('退款申请已执行', tool.get('status') == 'executed', str(tool.get('status')))

        status, body = call('POST', '/api/chat',
                            {'message': '确认退款 ORD-1001', 'conversation_id': conversation_id},
                            token=token)
        turns_sent += 1
        tool = body.get('tool') or {}
        approval_id = body.get('approval_id') or tool.get('approval_id') or ''
        check('高危退款进入 waiting_approval', tool.get('status') == 'waiting_approval',
              f"status={tool.get('status')}")
        check('返回审批单号', bool(approval_id), approval_id)
        check('等待审批时未执行业务动作', not (tool.get('result') or {}).get('executed'))

        # -- 6. reviewer decides -------------------------------------------- #
        print('\n[6] 人工审批闭环')
        status, _ = call('POST', f'/api/approvals/{approval_id}/decide',
                         {'decision': 'approve', 'comment': '验收通过'}, token=token)
        check('普通用户不能审批（403）', status == 403, f'status={status}')

        reviewer_token = login(*REVIEWER)
        status, body = call('GET', '/api/approvals', token=reviewer_token)
        check('审批列表可见', status == 200 and any(
            a.get('id') == approval_id for a in (body or [])), f'status={status}')
        status, body = call('POST', f'/api/approvals/{approval_id}/decide',
                            {'decision': 'approve', 'comment': '验收通过'}, token=reviewer_token)
        check('审批通过', status == 200 and body.get('status') == 'approved', str(body)[:160])
        status, body = call('POST', f'/api/approvals/{approval_id}/decide',
                            {'decision': 'approve', 'comment': '重复审批'}, token=reviewer_token)
        check('审批不可重复消费', status >= 400, f'status={status}')

        # The decision has to reach the *order system*, not just this service's
        # own approvals table. If the relay is missing, the order system still
        # answers ``approval_required`` and the refund silently never happens.
        status, conv = call('GET', f'/api/conversations/{conversation_id}', token=token)
        resumed = [m for m in (conv or {}).get('messages') or [] if m.get('role') == 'assistant']
        check('审批后已回写会话（用户看到结果）', len(resumed) >= 4, f'{len(resumed)} 条助手消息')
        status, order = call('POST', '/api/chat',
                             {'message': '查订单 ORD-1001', 'conversation_id': conversation_id},
                             token=token)
        turns_sent += 1
        now_refundable = ((order.get('tool') or {}).get('result') or {}).get('refund_available')
        check('退款已在订单系统生效（不可重复退）', now_refundable is False,
              f'refund_available={now_refundable}')

    # -- 7. persistence ---------------------------------------------------- #
    print('\n[7] 对话持久化')
    status, body = call('GET', f'/api/conversations/{conversation_id}', token=token)
    check('会话可回读', status == 200, f'status={status}')
    messages = (body or {}).get('messages') or []
    # Each turn writes a user message plus an assistant message, so the floor is
    # derived from the turns this run actually sent. The approval path appends
    # one extra assistant message when it resumes the parked run, hence ``>=``.
    expected = 2 * turns_sent
    check(f'消息已落库（本轮 {turns_sent} 轮 → >={expected} 条）',
          len(messages) >= expected, f'{len(messages)} 条')
    check('刷新历史不丢：含首轮提问', any('退款' in (m.get('content') or '') for m in messages))
    check('消息带 ttft/延迟指标', any((m.get('latency_ms') or 0) > 0 for m in messages))
    status, _ = call('GET', f'/api/conversations/{conversation_id}',
                     token=login(*REVIEWER))
    check('跨用户读取会话被拒/不可见', status in (403, 404), f'status={status}')

    # -- 8. SSE ------------------------------------------------------------ #
    print('\n[8] SSE 流式')
    events = []
    try:
        req = urllib.request.Request(
            f'{BASE}/api/chat/stream',
            data=json.dumps({'message': '七天无理由退货的规则是什么？'}).encode(),
            method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Authorization', f'Bearer {token}')
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60, context=CTX) as res:
            for raw_line in res:
                line = raw_line.decode('utf-8', 'replace').strip()
                if line.startswith('data:'):
                    payload = line[5:].strip()
                    if payload == '[DONE]':
                        break
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass
        elapsed = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001
        elapsed = 0.0
        check('SSE 连接', False, f'{type(exc).__name__}: {exc}')
    names = [e.get('event') for e in events]
    check('收到 SSE 事件', bool(events), ','.join(names[:8]))
    check('含 start 事件', 'start' in names)
    check('含终态事件', 'final' in names or 'replace' in names)
    check('流式响应及时（未缓冲）', elapsed < 30, f'{elapsed:.2f}s')

    # -- 9. observability -------------------------------------------------- #
    print('\n[9] 可观测指标')
    status, metrics = call('GET', '/metrics', raw=True)
    check('/metrics 可抓取', status == 200, f'status={status}')
    # A redirect that escapes the ``/support/`` prefix lands on the neighbouring
    # site and returns that site's HTML with status 200 — indistinguishable from
    # success unless the body is actually inspected.
    check('返回的是 Prometheus 文本而非别站 HTML',
          isinstance(metrics, str) and '<html' not in metrics[:400].lower()
          and '# TYPE' in metrics[:4000],
          repr(metrics[:60]) if isinstance(metrics, str) else type(metrics).__name__)
    for series in ('gateway_ttft_seconds', 'gateway_chat_outcomes_total',
                   'gateway_intent_total', 'gateway_intent_confidence',
                   'gateway_tool_calls_total', 'gateway_approvals_total',
                   'gateway_knowledge_hits_total', 'gateway_handoffs_total',
                   'gateway_answer_latency_seconds'):
        check(f'指标存在 {series}', isinstance(metrics, str) and series in metrics)
    check('指标不含租户标签（避免基数爆炸）',
          isinstance(metrics, str) and 'tenant_id=' not in metrics)

    # -- summary ----------------------------------------------------------- #
    summary = f'\n=== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败'
    summary += f' / {len(SKIP)} 跳过 ===' if SKIP else ' ==='
    print(summary)
    if SKIP:
        print('跳过项（需重置演示数据后重跑）:')
        for message in SKIP:
            print(f'  - {message}')
    if FAIL:
        print('失败项:')
        for name in FAIL:
            print(f'  - {name}')
        return 1
    print('全部通过 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
