#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live LLM path probe.

A deployment can look perfectly healthy with ``LLM_ENABLED=1`` while every
single call silently degrades to the deterministic template: the intent
fallback parses an empty completion, gives up, the template answers, and the
HTTP response is a clean 200. Nothing in the container status or the readiness
endpoint reveals it.

The only way to catch that is to assert the **metering actually moved**. Tokens
and cost are written from the provider's own ``usage`` block, so a non-zero
token count with ``degraded=false`` is direct evidence the model ran — and it
cannot be faked by the deterministic path.

Also asserts the error contract over the public URL, because the unified error
envelope is only worth having if it survives the edge proxy.

Stdlib only. Usage::

    python3 scripts/llm_probe.py https://124.222.190.3/support

Exit code is 0 only when the model really answered.
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'https://124.222.190.3/support').rstrip('/')
TENANT = 'tenant-demo'
CUSTOMER = ('customer@demo.local', 'DemoCustomer123')

# Self-signed certificate (no domain yet) — this is a probe, not a security control.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> bool:
    (PASS if ok else FAIL).append(name)
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f' — {detail}' if detail else ''))
    return ok


def call(method: str, path: str, body=None, token: str = '', raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f'{BASE}{path}', data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=90, context=CTX) as res:
            text = res.read().decode('utf-8', 'replace')
            return res.status, (text if raw else json.loads(text or 'null'))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode('utf-8', 'replace')
        try:
            return exc.code, json.loads(text or 'null')
        except json.JSONDecodeError:
            return exc.code, text


def login() -> str:
    status, body = call('POST', '/api/auth/login', {
        'tenant_id': TENANT, 'email': CUSTOMER[0], 'password': CUSTOMER[1],
    })
    if status != 200:
        raise SystemExit(f'login failed: {status} {body}')
    return body['access_token']


def stream_events(token: str, message: str) -> tuple[list[dict], float]:
    """Collect SSE events and the time to the first delta."""
    req = urllib.request.Request(
        f'{BASE}/api/chat/stream',
        data=json.dumps({'message': message}).encode(),
        method='POST',
    )
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', f'Bearer {token}')

    events: list[dict] = []
    ttft = 0.0
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=90, context=CTX) as res:
        for raw in res:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if evt.get('event') == 'delta' and ttft == 0.0:
                ttft = (time.perf_counter() - started) * 1000
            events.append(evt)
    return events, ttft


def main() -> int:
    print(f'\n=== LLM 链路验收: {BASE} ===\n')

    # -- 1. configuration is actually live -------------------------------- #
    print('[1] 配置生效')
    status, ready = call('GET', '/api/readyz')
    check('readyz 200', status == 200, f'status={status}')
    check('LLM 已启用', ready.get('llm_enabled') is True)
    llm = ready.get('llm') or {}
    check('模型已配置', bool(llm.get('model')), str(llm.get('model')))
    check('thinking 已关闭（否则短任务会被推理吃光预算）',
          llm.get('thinking') in ('disabled', 'auto'), str(llm.get('thinking')))
    check('熔断器未打开', llm.get('circuit_open') is False)
    # A model that is down must not take the service out of rotation.
    check('LLM 状态不影响 readyz 结论（主干独立可用）',
          ready.get('status') == 'ready' and 'llm' not in (ready.get('checks') or {}),
          f"status={ready.get('status')} checks={ready.get('checks')}")

    token = login()

    # -- 2. the model is really on the path -------------------------------- #
    print('\n[2] 模型确实被调用（不是静默降级）')
    status, body = call('POST', '/api/chat',
                        {'message': '退款一般多久到账？'}, token=token)
    check('对话 200', status == 200, f'status={status}')
    meta = body.get('metadata') or {}
    tokens_in = int(meta.get('tokens_in') or 0)
    tokens_out = int(meta.get('tokens_out') or 0)
    cost = float(meta.get('cost_usd') or 0.0)

    # These three are the whole point: they come from the provider's usage
    # block, so the deterministic path cannot produce them.
    check('确实消耗了输入 token', tokens_in > 0, f'tokens_in={tokens_in}')
    check('确实消耗了输出 token', tokens_out > 0, f'tokens_out={tokens_out}')
    check('成本已计量（单价配置生效）', cost > 0, f'cost_usd={cost}')
    check('未发生降级', body.get('degraded') is False,
          f"degraded={body.get('degraded')}")
    check('回答非空', bool((body.get('answer') or '').strip()),
          (body.get('answer') or '')[:60])
    check('回答不是模板回退产物', '（无）' not in (body.get('answer') or ''))

    # -- 3. streaming really streams -------------------------------------- #
    print('\n[3] 流式（真实 TTFT 有意义）')
    events, ttft = stream_events(token, '七天无理由退货的规则是什么？')
    kinds = [e.get('event') for e in events]
    final = next((e for e in events if e.get('event') == 'final'), {})
    deltas = sum(1 for k in kinds if k == 'delta')

    check('收到多个 delta（而非一次性吐出）', deltas >= 2, f'{deltas} 个 delta')
    check('收到终态事件', 'final' in kinds)
    check('首字延迟已记录', ttft > 0, f'{ttft:.0f}ms')
    fmeta = final.get('metadata') or {}
    check('流式路径同样计量 token',
          int(fmeta.get('tokens_in') or 0) > 0,
          f"tokens_in={fmeta.get('tokens_in')}")
    check('流式路径未降级', final.get('degraded') is False,
          f"degraded={final.get('degraded')}")

    # -- 4. error contract survives the edge proxy ------------------------- #
    print('\n[4] 错误契约（经边缘 nginx + Caddy）')
    status, body = call('POST', '/api/chat', {'message': 'x' * 9000}, token=token)
    check('超长消息被拒 422', status == 422, f'status={status}')
    if isinstance(body, dict):
        check('detail 是字符串（不是数组，前端不会显示 [object Object]）',
              isinstance(body.get('detail'), str), repr(body.get('detail'))[:80])
        err = body.get('error') or {}
        check('error.code 存在', err.get('code') == 'validation_error', str(err.get('code')))
        fields = err.get('fields') or []
        check('error.fields 指出了字段', bool(fields), str(fields)[:100])
        check('字段名不带 body. 前缀',
              all(not f.get('field', '').startswith('body.') for f in fields))
        check('带 trace_id（用户可据此报障）', bool(body.get('trace_id')))

    status, body = call('GET', '/api/conversations/nope', token=token)
    check('未知会话 404 且有稳定 code',
          status == 404 and isinstance(body, dict)
          and (body.get('error') or {}).get('code') == 'not_found',
          f'status={status}')

    # -- 5. degraded answers are visible after a reload -------------------- #
    print('\n[5] 降级状态可持久化回看')
    status, body = call('POST', '/api/chat',
                        {'message': '退款一般多久到账？'}, token=token)
    conversation_id = body.get('conversation_id') or ''
    status, detail = call('GET', f'/api/conversations/{conversation_id}', token=token)
    check('会话可回读', status == 200, f'status={status}')
    messages = (detail or {}).get('messages') or []
    assistants = [m for m in messages if m.get('role') == 'assistant']
    check('助手消息存在', bool(assistants), f'{len(assistants)} 条')
    check('消息带 intent_source（刷新后仍能解释判定来源）',
          any(m.get('intent_source') for m in assistants),
          str([m.get('intent_source') for m in assistants]))
    check('消息带 degraded 字段（刷新后仍能看出是否降级）',
          all('degraded' in m for m in assistants))
    check('消息带 token 计量', any(int(m.get('tokens_in') or 0) > 0 for m in assistants))

    print(f'\n=== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ===')
    if FAIL:
        print('失败项:')
        for name in FAIL:
            print(f'  - {name}')
        return 1
    print('全部通过 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
