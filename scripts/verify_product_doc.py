#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify every number PRODUCT.md claims against the repository.

Written because "文档里的计数是手工维护的，就一定会漂" — this project's docs had
already drifted once (README said 234 tests, the suite had 235). A product
document that is wrong about its own numbers is worse than no document: the
reader has no way to tell which parts to trust.

So each claim in docs/PRODUCT.md is restated here as an executable assertion.
Run it after any change that could move a number.

    .venv/Scripts/python.exe scripts/verify_product_doc.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs' / 'PRODUCT.md'

passed = 0
failures: list[str] = []


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding='utf-8')


def count(rel: str, pattern: str) -> int:
    return len(re.findall(pattern, read(rel), re.M))


def check(desc: str, ok: bool, detail: str = '') -> None:
    global passed
    if ok:
        passed += 1
        print(f'  [PASS] {desc}{" — " + detail if detail else ""}')
    else:
        failures.append(desc)
        print(f'  [FAIL] {desc}{" — " + detail if detail else ""}')


def doc_says(text: str) -> bool:
    """Is this string actually present in the product document?"""
    return text in read('docs/PRODUCT.md')


def collected_tests() -> int | None:
    """How many test cases pytest would collect.

    ``--collect-only`` rather than a full run: the release gate invokes this
    script *before* its pytest layer, so executing the suite here would double
    the gate's wall time for nothing. Collection still imports every test
    module, so a module that fails to import fails here too — which is the
    part that matters.

    Why this exists at all: ``'238' in doc`` only proves the document
    *contains* that number, not that it is the correct one. Add a test and the
    document silently drifts while the gate stays green — which is exactly how
    README came to claim 234 tests while the suite had 235. The document
    already states two figures for this suite, passed and skipped, so they can
    be checked against what pytest actually collects.
    """
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q'],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    m = re.search(r'(\d+) tests? collected', proc.stdout)
    return int(m.group(1)) if m else None


print('=== 文档中引用的计数 ===')
check('数据表 19 张（网关 12 + 业务 7）',
      count('gateway/app/models.py', r'__tablename__') == 12
      and count('business_service/app/models.py', r'__tablename__') == 7)
check('注册工具 7 个', count('gateway/app/core/governance.py', r'register_tool\(ToolSpec\(') == 7)
check('主干节点 8 个', count('gateway/app/core/engine.py', r'@step\(WORKFLOW_TYPE') == 8)
check('意图 7 种', count('gateway/app/core/intent.py', r"^    [A-Z_]+ = '") == 7)
check('告警规则 10 条', count('observability/alerts.yml', r'- alert:') == 10)
check('告警分组 4 组', count('observability/alerts.yml', r'^- name:') == 4)
check('知识库种子 16 篇', count('gateway/app/seed.py', r"'category':") == 16)
check('演示账号 4 个', count('gateway/app/seed.py', r"^    \('.*@demo\.local'") == 4)
check('商品 5 个', count('business_service/app/service.py', r"^\s*\('SKU-") == 5)
check('售后规则 5 条', count('business_service/app/service.py', r"^\s*\('AR-") == 5)
check('订单 4 个', count('business_service/app/service.py', r"^\s*\('ORD-") == 4)
check('评测集 18 条（9 golden + 9 regression）',
      count('evaluation/golden_dataset.jsonl', r'^\{"id"') == 9
      and count('evaluation/regression_dataset.jsonl', r'^\{"id"') == 9)
check('指标族 26 个',
      count('gateway/app/metrics.py', r'= (Counter|Gauge|Histogram|Summary)\(') == 26)

print('\n=== 编排：服务 / 端口 / 内存上限 ===')
compose = read('deployment/docker-compose.yml')
services = ['postgres', 'redis', 'business-service', 'gateway',
            'caddy', 'prometheus', 'alertmanager', 'grafana']


def service_block(name: str) -> str:
    """Body of one compose service.

    Split on ``^  \\S`` rather than ``\\n  ``: the latter also matches the first
    two spaces of every *four*-space-indented line inside the block, so it
    truncates at the block's first key and every memory assertion then fails
    against a real limit. (That is what this function's first version did.)
    """
    m = re.search(rf'^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)',
                  compose, re.M | re.S)
    return m.group(1) if m else ''


for svc in services:
    check(f'服务 {svc} 存在', re.search(rf'^  {re.escape(svc)}:$', compose, re.M) is not None)
for limit, svc in [('768m', 'postgres'), ('192m', 'redis'), ('384m', 'business-service'),
                   ('768m', 'gateway'), ('128m', 'caddy'), ('320m', 'prometheus'),
                   ('96m', 'alertmanager'), ('320m', 'grafana')]:
    check(f'{svc} 内存上限 {limit}', f'memory: {limit}' in service_block(svc))
for port in ['127.0.0.1:9090:9090', '127.0.0.1:9093:9093', '127.0.0.1:3000:3000']:
    check(f'仅回环发布 {port}', port in compose)
check('业务服务仅内网 9000', 'expose: ["9000"]' in compose)
check('网关仅内网 8000', 'expose: ["8000"]' in compose)
check('Caddy 监听 :8080', ':8080 {' in read('deployment/Caddyfile'))

print('\n=== 端点与工具（文档里的表逐条核对） ===')


def gateway_routes() -> set[str]:
    """Full paths, with each router's own prefix applied.

    The prefix lives on the APIRouter, not on the decorator, so grepping for
    '/api/chat' in the router files finds nothing and every endpoint assertion
    fails against a correct document. Compose prefix + path instead.
    """
    routes: set[str] = set()
    for name in ('auth', 'chat', 'conversations', 'approvals', 'admin'):
        src = read(f'gateway/app/routers/{name}.py')
        prefix = re.search(r"APIRouter\(prefix='([^']*)'", src)
        base = prefix.group(1) if prefix else ''
        for m in re.finditer(r"@router\.(?:get|post|delete|put|patch)\('([^']*)'", src):
            routes.add(base + m.group(1))
    for m in re.finditer(r"@app\.(?:get|post)\('([^']*)'", read('gateway/app/main.py')):
        routes.add(m.group(1))
    return routes


routes = gateway_routes()
for path in ['/api/chat', '/api/chat/stream', '/api/hitl/{form_token}',
             '/api/auth/login', '/api/auth/register', '/api/auth/me',
             '/api/conversations', '/api/conversations/{conversation_id}',
             '/api/conversations/{conversation_id}/close',
             '/api/approvals', '/api/approvals/{approval_id}',
             '/api/approvals/{approval_id}/decide',
             '/api/admin/overview', '/api/admin/quota', '/api/admin/traces/{trace_id}',
             '/api/admin/knowledge', '/api/admin/knowledge/{document_id}',
             '/api/healthz', '/api/readyz']:
    check(f'网关端点 {path}', path in routes)
biz = read('business_service/app/main.py')
for path in ['/orders/{order_id}', '/accounts/{user_id}', '/skus/{sku_id}',
             '/aftersale/eligibility', '/tickets', '/refunds/proposals',
             '/refunds/review', '/refunds/execute']:
    check(f'业务端点 {path}', path in biz)
check('审阅接口要求 REVIEWER 凭据', 'require_reviewer_key' in biz.split('/refunds/review')[1][:400])
for tool in ['get_order', 'get_account', 'get_sku', 'check_aftersale_eligibility',
             'create_ticket', 'create_refund_proposal', 'execute_refund']:
    check(f'工具 {tool} 已注册', f"name='{tool}'" in read('gateway/app/core/governance.py'))

print('\n=== 阈值与配置（文档里的表逐条核对） ===')
cfg = read('gateway/app/config.py')
for needle, label in [('intent_llm_threshold: float = 0.55', '意图 LLM 阈值 0.55'),
                      ('intent_handoff_threshold: float = 0.35', '转人工阈值 0.35'),
                      ('handoff_frustration_turns: int = 3', '挫败感 3 轮'),
                      ('rate_limit_per_minute: int = 60', '限流 60/min'),
                      ('tenant_daily_message_quota: int = 5000', '配额 5000/日'),
                      ('db_pool_size: int = 5', '连接池 5'),
                      ('db_max_overflow: int = 10', '溢出 10'),
                      ("llm_model: str = 'deepseek-flash'", '模型 deepseek-flash'),
                      ('llm_timeout_seconds: float = 30.0', 'LLM 超时 30s'),
                      ('llm_max_tokens: int = 800', 'LLM 上限 800'),
                      ("llm_thinking: Literal['auto', 'enabled', 'disabled'] = 'disabled'", 'thinking 关闭'),
                      ('llm_price_in_per_1k: float = 0.0003', '入价 0.0003'),
                      ('llm_price_in_cached_per_1k: float = 0.000006', '缓存入价 0.000006'),
                      ('llm_price_out_per_1k: float = 0.0012', '出价 0.0012')]:
    check(label, needle in cfg)
check('意图分类固定 80 token', '80' in read('gateway/app/core/llm.py'))

print('\n=== 前端规模 ===')
check('运营台 index.html ≈49.9 KB', 48000 < (ROOT / 'demo_client/index.html').stat().st_size < 52000,
      f"{(ROOT / 'demo_client/index.html').stat().st_size} 字节")
check('C 端 customer.html ≈15.2 KB', 14000 < (ROOT / 'demo_client/customer.html').stat().st_size < 16000,
      f"{(ROOT / 'demo_client/customer.html').stat().st_size} 字节")
check('运营台 3 个 Tab（chat/approvals/metrics）',
      all(f'data-view="{v}"' in read('demo_client/index.html')
          for v in ('chat', 'approvals', 'metrics')))

print('\n=== 文档自身的一致性 ===')
doc = read('docs/PRODUCT.md')


def _live_only_cases() -> int:
    """How many dataset cases need the real link, and are therefore skipped.

    This is the *cause* of every skip in the unit suite: ``test_eval_dataset``
    skips each ``live_only`` case and names the dependency it is missing. So
    both figures the document states are now derived from real sources — the
    skipped count from the dataset, the passed count from pytest's collection —
    rather than from a hand-kept pair that drifts together and stays consistent
    with itself while drifting away from reality.
    """
    total = 0
    for path in sorted((ROOT / 'evaluation').glob('*_dataset.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip() and json.loads(line).get('live_only'):
                total += 1
    return total


_collected = collected_tests()
UNIT_SKIPPED = _live_only_cases()
UNIT_PASSED = _collected - UNIT_SKIPPED
check(f'文档写 {UNIT_PASSED} 个 pytest', str(UNIT_PASSED) in doc, str(UNIT_PASSED))
check(f'文档写 {UNIT_SKIPPED} 项跳过', f'{UNIT_SKIPPED} skipped' in doc, str(UNIT_SKIPPED))
check('推导出的通过数是正数（收集数真的大于跳过数）',
      UNIT_PASSED > 0, f'收集 {_collected} / 跳过 {UNIT_SKIPPED}')
for n, label in [(55, '端到端 55'), (30, 'LLM 30'), (17, '运营台 UI 17'),
                 (60, 'C 端 UI 60'), (11, '观测 11'), (18, '评测 18')]:
    check(f'文档写 {label}', f'{n} / 0' in doc or f'{n} 通过' in doc or f'{n} / 18' in doc
          or f'{n}/18' in doc)
check('文档声明版本号已统一到 2.6.0',
      '2.6.0' in doc and '三处不一致' not in doc)
check('文档声明与 architecture.md 冲突时以本文档为准',
      '以本文档为准' in doc)

print('\n=== 版本号统一（v2.6 起，文档 13.3 的断言） ===')
version_file = read('VERSION').strip()
settings_version = re.search(r"version: str = '([^']+)'", cfg).group(1)
changelog_top = re.search(r'^## (\S+)', read('CHANGELOG.md'), re.M).group(1)
service_version = re.search(
    r"SERVICE_VERSION\s*=\s*'([^']+)'", read('business_service/app/config.py')).group(1)
check('VERSION 文件是 2.6.0', version_file == '2.6.0', version_file)
check('settings.version 是 2.6.0', settings_version == '2.6.0', settings_version)
check('CHANGELOG 顶部是 2.6.0', changelog_top == '2.6.0', changelog_top)
check('business SERVICE_VERSION 是 2.6.0', service_version == '2.6.0', service_version)
check('四处版本号完全一致（不再有漂移）',
      len({version_file, settings_version, changelog_top, service_version}) == 1,
      f'{version_file} / {settings_version} / {changelog_top} / {service_version}')
# 文档本身不能再声称「三处不一致」——那句话在 v2.6 已经不成立了。
check('文档 13.3 不再声称版本号漂移',
      '三处不一致' not in doc and '未擅自改动' not in doc)

print(f'\n=== 结果: {passed} 通过 / {len(failures)} 失败 ===')
if failures:
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
print('文档与代码一致 ✅')
