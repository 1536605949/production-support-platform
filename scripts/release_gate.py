#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release gate.

What this replaces: a gate that parsed ``dify/workflows/*.yml`` and, unless
``LIVE_ACCEPTANCE=1`` was set, ran *only* pytest. Since CI never sets that
variable, the "release gate" in CI was a pytest run with two YAML parses in
front of it — while the three layers that have actually caught defects in this
project (the public-link probe, the DOM smoke test, the real LLM call) were
never executed. A gate that silently skips its most valuable checks is worse
than no gate, because it produces a green tick that means less than it appears.

This version:

* validates every config file the stack loads, including the generated Grafana
  dashboard — and **regenerates it first**, so a dashboard that no longer
  matches its generator (or that references a metric that does not exist) fails
  the gate rather than rotting until someone opens the board;
* runs the unit suite;
* runs the three live layers when ``LIVE_ACCEPTANCE=1``;
* when they are skipped, says so **loudly** and, under ``--strict``, fails.

Usage::

    python scripts/release_gate.py                  # static + pytest
    LIVE_ACCEPTANCE=1 python scripts/release_gate.py  # + probe/ui/llm
    python scripts/release_gate.py --strict         # live layers required
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://124.222.190.3/support')

_failures: list[str] = []


def section(title: str) -> None:
    print(f'\n=== {title} ===')


def ok(message: str) -> None:
    print(f'  PASS  {message}')


def fail(message: str) -> None:
    print(f'  FAIL  {message}')
    _failures.append(message)


def check_yaml(rel: str) -> None:
    path = ROOT / rel
    if not path.exists():
        fail(f'{rel} 不存在')
        return
    try:
        yaml.safe_load(path.read_text(encoding='utf-8'))
        ok(rel)
    except Exception as exc:  # noqa: BLE001
        fail(f'{rel}: {exc}')


def check_json(rel: str) -> None:
    path = ROOT / rel
    if not path.exists():
        fail(f'{rel} 不存在')
        return
    try:
        json.loads(path.read_text(encoding='utf-8'))
        ok(rel)
    except Exception as exc:  # noqa: BLE001
        fail(f'{rel}: {exc}')


def check_ini(rel: str) -> None:
    """``alembic.ini`` is INI, not YAML — parsing it as YAML fails on the ``%``
    in ``file_template`` and would report a false failure."""
    import configparser

    path = ROOT / rel
    if not path.exists():
        fail(f'{rel} 不存在')
        return
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding='utf-8')
        assert parser.has_section('alembic'), '缺少 [alembic] 段'
        assert parser.get('alembic', 'script_location'), 'script_location 为空'
        ok(rel)
    except Exception as exc:  # noqa: BLE001
        fail(f'{rel}: {exc}')


def run(cmd: list[str], *, required: bool = True, env: dict | None = None) -> int:
    print(f'\n$ {" ".join(cmd)}')
    merged = {**os.environ, **(env or {})}
    code = subprocess.run(cmd, cwd=ROOT, env=merged).returncode
    if code and required:
        fail(f'命令失败（exit {code}）: {" ".join(cmd)}')
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--strict', action='store_true',
                        help='把「跳过真实链路检查」视为失败')
    parser.add_argument('--skip-tests', action='store_true',
                        help='跳过 pytest（仅跑静态检查）')
    args = parser.parse_args()

    # -- 1. configuration ---------------------------------------------------- #
    section('配置校验')
    for rel in (
        'deployment/docker-compose.yml',
        'observability/prometheus.yml',
        'observability/alerts.yml',
        'observability/alertmanager/alertmanager.yml',
        'tools/business_api.openapi.yaml',
        'tools/reviewer_api.openapi.yaml',
    ):
        check_yaml(rel)
    check_ini('alembic.ini')
    check_json('observability/grafana/provisioning/dashboards/support-platform.json')

    # -- 2. generated artefacts ---------------------------------------------- #
    # Regenerating first turns "the dashboard drifted from its generator" into a
    # gate failure. The generator itself refuses to emit a panel that queries a
    # metric ``gateway/app/metrics.py`` does not declare, so this also catches a
    # renamed counter that would otherwise render as a permanent "No data".
    section('生成物一致性（Grafana 看板）')
    dashboard = ROOT / 'observability/grafana/provisioning/dashboards/support-platform.json'
    before = dashboard.read_text(encoding='utf-8') if dashboard.exists() else ''
    code = run([sys.executable, 'observability/grafana/build_dashboard.py'])
    if code == 0:
        after = dashboard.read_text(encoding='utf-8')
        if before != after:
            fail('Grafana 看板 JSON 与生成器不一致——提交的是手改过的版本，请重新生成')
        else:
            ok('看板与生成器一致，且引用的指标都存在')

    # -- 3. migration tree --------------------------------------------------- #
    section('数据库迁移')
    # ``alembic history`` works without a database, so the revision chain is
    # validated here even in CI. The column-level drift check needs a live
    # PostgreSQL and lives in scripts/migrate.sh check.
    run([sys.executable, '-m', 'alembic', '-c', 'alembic.ini', 'history'],
        env={'DATABASE_URL': 'sqlite:///./data/_gate_probe.db'})

    # -- 3b. product document ------------------------------------------------ #
    # A count in a document is a claim, and hand-maintained counts drift. This
    # repository has already drifted once — README said 234 tests while the
    # suite had 235 — and docs/architecture.md still says "九项能力 / 105 个
    # pytest / 18 个面板" years of changes later. A product document that is
    # wrong about its own numbers is worse than none: the reader cannot tell
    # which parts to trust. So every number PRODUCT.md states is asserted
    # against the source here.
    section('产品文档与代码一致性')
    run([sys.executable, 'scripts/verify_product_doc.py'])

    # -- 4. unit suite ------------------------------------------------------- #
    if not args.skip_tests:
        section('单元测试')
        run([sys.executable, '-m', 'pytest', '-q'])

    # -- 5. live layers ------------------------------------------------------ #
    live = os.getenv('LIVE_ACCEPTANCE', '0') == '1'
    if live:
        section(f'真实链路（{PUBLIC_URL}）')
        run([sys.executable, 'scripts/acceptance_probe.py', PUBLIC_URL])
        # Through the wrapper, not `node scripts/ui_smoke_test.mjs` directly: the
        # wrapper locates jsdom. Invoking node here made the UI layer depend on an
        # ambient NODE_PATH that nothing set, so in a clean shell this step always
        # failed with a bare "exit 2" that read like a UI regression instead of a
        # missing npm package.
        #
        # And pass the *absolute* path from shutil.which, not 'bash'. On Windows
        # CreateProcess searches System32 before PATH, so a bare 'bash' resolves to
        # C:\Windows\System32\bash.exe — the WSL stub — even when Git Bash is first
        # in PATH and shutil.which returns it. Observed: this step failed with exit 1
        # and "PROGRAM BLOCKED BY SECURITY POLICY ... wsl.exe" while the identical
        # command typed by hand worked.
        bash = shutil.which('bash')
        if bash is None:
            fail('找不到 bash：UI 层需要 scripts/ui_smoke.sh（它负责定位 jsdom）')
        else:
            run([bash, 'scripts/ui_smoke.sh', PUBLIC_URL])
            # The C-end page has the *opposite* contract about the same data:
            # where the console must show intent, confidence, sources and the
            # trace id, this page must show none of it. That is a negative
            # assertion, and it is only worth something when checked against the
            # DOM that was actually rendered — which is what this second script
            # does. Same wrapper, so jsdom is still located in exactly one place.
            run([bash, 'scripts/ui_smoke.sh', PUBLIC_URL,
                 'scripts/ui_smoke_customer.mjs'])
        run([sys.executable, 'scripts/llm_probe.py'])
        # Answer-quality regression, which none of the layers above can see.
        # pytest pins behaviour; the probe pins the trust chain; this pins
        # whether the answers are still as good, and flags any case that used
        # to pass and now does not.
        run([sys.executable, 'evaluation/run_eval.py', PUBLIC_URL])
    else:
        section('真实链路 —— 已跳过')
        print('  LIVE_ACCEPTANCE != 1')
        print('  未验证: 公网探针 / UI DOM 冒烟（运营台 + C 端）/ LLM 真实调用 / 答案质量回归')
        print('  这四层在本项目历史上各自抓到过单元测试抓不到的缺陷，')
        print('  所以下面这个 PASS 只代表静态与单元层通过。')
        if args.strict:
            fail('--strict 要求真实链路检查，但 LIVE_ACCEPTANCE != 1')

    print()
    if _failures:
        print(f'RELEASE GATE FAILED（{len(_failures)} 项）')
        for item in _failures:
            print(f'  - {item}')
        return 1
    if live:
        print('RELEASE GATE PASS（静态 + 单元 + 真实链路）')
    else:
        print('RELEASE GATE PASS（仅静态 + 单元；真实链路未验证）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
