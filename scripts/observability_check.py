#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the observability stack is actually wired together.

"The container is Up" is not evidence that monitoring works. Each layer can be
running and still be useless:

* Prometheus can be up with an empty ``activeAlertmanagers`` — the rules are
  evaluated and shown as "firing" in the Prometheus UI, so everything *looks*
  healthy, but nobody is told. This happened on the first deploy of this stack:
  ``prometheus.yml`` gained an ``alerting:`` block but the container was never
  restarted, so it was still running the old config.
* Alertmanager can be up with a config it failed to load (and, in one version,
  restart-loop on an unknown flag).
* Grafana can be up with no datasource, or with a dashboard that queries metrics
  nobody emits — which renders "No data" forever and teaches operators to ignore
  the board.

So this script asserts the **links**, not the processes. Run it after any change
to prometheus.yml / alerts.yml / alertmanager.yml / the dashboard.

Usage (on the server, where the containers live)::

    python3 scripts/observability_check.py

Written in Python rather than shell on purpose: the checks parse JSON APIs, and
doing that with ``python3 -c`` inside a shell heredoc means every nested quote
becomes a bug — which is exactly how the first version of this script failed.
"""
from __future__ import annotations

import json
import subprocess
import sys

PROM = 'support-prometheus-1'
AM = 'support-alertmanager-1'
GRAFANA = 'support-grafana-1'

# alerts.yml declares 10 rules. Asserting a floor catches the case where
# Prometheus started but silently failed to load the rule file (a typo in
# rule_files leaves zero rules and no error in the UI).
MIN_RULES = 10
MIN_TARGETS = 4

# The placeholder written by docker-compose when ALERT_WEBHOOK_URL is unset.
# A receiver pointed here satisfies every structural check — config loads, the
# receiver exists, routing matches — while every notification fails to resolve.
# That is the exact failure mode this whole script exists to catch, so the
# check must not report a clean bill of health on it.
UNCONFIGURED_WEBHOOK = 'alert-webhook-not-configured.invalid'

_passed = 0
_warned: list[str] = []
_failed: list[str] = []


def ok(message: str) -> None:
    global _passed
    _passed += 1
    print(f'  \033[32mPASS\033[0m  {message}')


def warn(message: str) -> None:
    """Structurally fine, but not doing the job. Not a failure — a deployment
    that has no chat/oncall webhook yet is legitimate — but never silent."""
    _warned.append(message)
    print(f'  \033[33mWARN\033[0m  {message}')


def bad(message: str) -> None:
    _failed.append(message)
    print(f'  \033[31mFAIL\033[0m  {message}')


def section(title: str) -> None:
    print(f'\n=== {title} ===')


def docker(*args: str) -> tuple[int, str]:
    proc = subprocess.run(['docker', *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout


def container_status(name: str) -> str:
    code, out = docker('inspect', '-f', '{{.State.Status}}', name)
    return out.strip() if code == 0 else 'missing'


def fetch(container: str, url: str) -> dict | None:
    """GET a JSON API from inside a container.

    ``wget -qO-`` rather than curl: the prom/* images are busybox-based and do
    not ship curl.
    """
    code, out = docker('exec', container, 'wget', '-qO-', url)
    if code != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def main() -> int:
    # ----------------------------------------------------------------------- #
    section('容器')
    for name in (PROM, AM, GRAFANA):
        status = container_status(name)
        (ok if status == 'running' else bad)(f'{name} 状态={status}')

    # ----------------------------------------------------------------------- #
    section('Prometheus 抓取目标')
    data = fetch(PROM, 'http://localhost:9090/api/v1/targets?state=active')
    if data is None:
        bad('无法读取 Prometheus targets API')
    else:
        targets = data['data']['activeTargets']
        down = 0
        for target in targets:
            job = target['labels'].get('job', '?')
            health = target['health']
            note = target.get('lastError', '')
            print(f'  {job:20s} {health:8s} {note}')
            if health != 'up':
                down += 1
        if len(targets) >= MIN_TARGETS:
            ok(f'抓取目标 {len(targets)} 个')
        else:
            bad(f'抓取目标只有 {len(targets)} 个（应为 gateway/business_service/'
                f'prometheus/alertmanager 至少 {MIN_TARGETS} 个）')
        if down == 0 and targets:
            ok('所有目标 health=up')
        elif targets:
            bad(f'{down} 个目标不健康')

    # ----------------------------------------------------------------------- #
    # The link that matters most.
    section('Prometheus → Alertmanager')
    data = fetch(PROM, 'http://localhost:9090/api/v1/alertmanagers')
    if data is None:
        bad('无法读取 alertmanagers API')
    else:
        active = data['data']['activeAlertmanagers']
        if active:
            ok(f'Prometheus 已注册 {len(active)} 个 Alertmanager')
        else:
            bad('activeAlertmanagers 为空——规则会被求值，但不会通知任何人')
            print('        提示：改了 observability/prometheus.yml 后需要重启 prometheus 容器')

    # ----------------------------------------------------------------------- #
    section('告警规则')
    data = fetch(PROM, 'http://localhost:9090/api/v1/rules')
    if data is None:
        bad('无法读取 rules API')
    else:
        groups = data['data']['groups']
        rules = [rule for group in groups for rule in group['rules']]
        print(f'  规则总数 {len(rules)}，分组 {len(groups)}')
        for rule in rules:
            print(f'  {rule["name"]:28s} {rule["state"]:9s} {rule.get("health", "")}')
        if len(rules) >= MIN_RULES:
            ok(f'已加载 {len(rules)} 条告警规则')
        else:
            bad(f'只加载了 {len(rules)} 条规则（alerts.yml 里定义了 {MIN_RULES} 条）')

    # ----------------------------------------------------------------------- #
    section('Alertmanager')
    data = fetch(AM, 'http://localhost:9093/api/v2/status')
    if data is None:
        bad('Alertmanager API 不可达')
    else:
        config = data.get('config', {}).get('original', '')
        print(f'  集群状态 {data.get("cluster", {}).get("status", "?")}')
        print(f'  配置字节数 {len(config)}')
        if 'url_file' in config or 'url:' in config:
            ok('Alertmanager 已加载配置，且含 webhook receiver')
        else:
            bad('Alertmanager 配置里没有 webhook receiver')

    # "Has a receiver" and "the receiver reaches someone" are different claims.
    # Read the file the entrypoint wrote — that is the value actually in use,
    # not what .env says it should be.
    code, target = docker('exec', AM, 'cat', '/tmp/alertmanager-webhook-url')
    target = (target or '').strip()
    if code != 0 or not target:
        bad('读不到 /tmp/alertmanager-webhook-url —— receiver 没有可用地址')
    elif UNCONFIGURED_WEBHOOK in target:
        warn(f'webhook 指向占位地址（{target}）——规则会路由，但通知发不出去；'
             f'在 deployment/.env 里设 ALERT_WEBHOOK_URL 后重启 alertmanager')
    else:
        ok(f'webhook 目标已配置（{target[:60]}）')

    data = fetch(AM, 'http://localhost:9093/api/v2/alerts')
    if data is not None:
        ok(f'当前告警数 {len(data)}（看板「正在告警」面板读的是同一事实来源）')

    # ----------------------------------------------------------------------- #
    # A provisioned dashboard that queries a metric nobody emits renders
    # "No data" forever. The generator validates metric existence at build time;
    # here we confirm Grafana actually picked the file up and did not error.
    section('Grafana')
    code, out = docker(
        'exec', GRAFANA, 'sh', '-c',
        'test -f /etc/grafana/provisioning/dashboards/support-platform.json && '
        'grep -c \'"type"\' /etc/grafana/provisioning/dashboards/support-platform.json',
    )
    if code == 0:
        ok(f'看板已挂载（{out.strip()} 个面板/分组定义）')
    else:
        bad('看板 JSON 不在 provisioning 目录里')

    code, logs = docker('logs', GRAFANA)
    problems = [
        line for line in (logs or '').splitlines()
        if 'provisioning' in line.lower() and ('error' in line.lower() or 'failed' in line.lower())
    ]
    if problems:
        bad(f'Grafana provisioning 日志有错误：{problems[0][:160]}')
    else:
        ok('Grafana 日志无 provisioning 错误')

    # ----------------------------------------------------------------------- #
    print()
    if _warned:
        print(f'{len(_warned)} 项警告（结构正常，但没在干活）：')
        for item in _warned:
            print(f'  ! {item}')
        print()
    if not _failed:
        suffix = f'，{len(_warned)} 项警告' if _warned else ''
        print(f'OBSERVABILITY OK（{_passed} 项通过{suffix}）')
        return 0
    print(f'OBSERVABILITY FAILED（{_passed} 通过 / {len(_failed)} 失败'
          + (f' / {len(_warned)} 警告' if _warned else '') + '）')
    for item in _failed:
        print(f'  - {item}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
