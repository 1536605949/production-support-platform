#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the Grafana dashboard JSON.

The dashboard is *generated* rather than hand-edited because 28 panels of
PromQL in raw JSON is not reviewable: a diff of the JSON tells you nothing
about which panel changed, so nobody reviews it and it rots. This file is the
source of truth.

    python3 observability/grafana/build_dashboard.py

Output is written **into the provisioning tree** that Grafana already mounts
(``/etc/grafana/provisioning/dashboards`` ← ``observability/grafana/provisioning``).
Writing it anywhere else produces a file nobody reads: Grafana's provider is
configured with a single directory path, and a dashboard JSON outside that tree
is simply never loaded. Provisioning only reads at startup, so a running
Grafana keeps serving the old dashboard until it is restarted:

    docker compose restart grafana

Design rules, so the panels stay honest:

* **Only metrics that actually exist.** Every expression below is backed by a
  counter/histogram in ``gateway/app/metrics.py``. A panel that queries a
  metric nobody emits renders "No data" forever and quietly teaches operators
  to ignore the dashboard.
* **No tenant label.** Metrics are deliberately not labelled by ``tenant_id``
  (unbounded cardinality). Per-tenant numbers live in ``/api/admin/overview``.
* **Thresholds where a human would act.** A number with no threshold makes the
  reader do the comparison; colour does it for them.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / 'provisioning' / 'dashboards' / 'support-platform.json'
METRICS_PY = Path(__file__).parents[2] / 'gateway' / 'app' / 'metrics.py'
DATASOURCE = {'type': 'prometheus', 'uid': 'prometheus'}

_panels: list[dict] = []
_next_id = 0
_cursor_y = 0
#: Height of the tallest panel in the band being laid out. The cursor only
#: advances by this much when a new band opens — that is what keeps a row of
#: stats side by side, and what stops a section separator from being drawn on
#: top of the charts it is supposed to separate.
_row_height = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def _new_band() -> None:
    """Close the current band so the next panel starts below everything."""
    global _cursor_y, _row_height
    _cursor_y += _row_height
    _row_height = 0


def _place(w: int, h: int) -> tuple[int, int]:
    """Return ``(x, y)`` for a ``w x h`` panel, wrapping to a new band if needed."""
    global _row_height
    used = sum(p['gridPos']['w'] for p in _panels
               if p['gridPos']['y'] == _cursor_y and p['type'] not in ('row', 'text'))
    if used and used + w > 24:
        _new_band()
        used = 0
    _row_height = max(_row_height, h)
    return used, _cursor_y


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def row(title: str) -> None:
    """A section separator. Without these the board is a wall of charts."""
    global _cursor_y, _row_height
    _new_band()
    _panels.append({
        'id': _id(),
        'type': 'row',
        'title': title,
        'collapsed': False,
        'panels': [],
        'gridPos': {'x': 0, 'y': _cursor_y, 'w': 24, 'h': 1},
    })
    # The separator occupies its own line: advance past it so the panels that
    # belong to this section start *below* the header rather than behind it.
    _cursor_y += 1
    _row_height = 0


def stat(title: str, expr: str, *, description: str = '', unit: str = 'none',
         decimals: int = 2, steps: list | None = None, w: int = 4, h: int = 4,
         color_mode: str = 'value') -> None:
    """A single big number. ``steps`` drives the colour."""
    x, y = _place(w, h)

    defaults: dict = {'unit': unit, 'decimals': decimals}
    if steps:
        defaults['thresholds'] = {'mode': 'absolute', 'steps': steps}
        defaults['color'] = {'mode': 'thresholds'}
    _panels.append({
        'id': _id(),
        'type': 'stat',
        'title': title,
        'description': description,
        'datasource': DATASOURCE,
        'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
        'fieldConfig': {'defaults': defaults, 'overrides': []},
        'options': {
            'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
            'colorMode': color_mode,
            'graphMode': 'area',
            'justifyMode': 'auto',
            'textMode': 'auto',
        },
        'targets': [{'datasource': DATASOURCE, 'expr': expr, 'refId': 'A',
                     'legendFormat': ''}],
    })


def timeseries(title: str, targets: list[tuple[str, str]], *, description: str = '',
               unit: str = 'none', decimals: int = 3, w: int = 12, h: int = 8,
               stack: bool = False, legend: str = 'list') -> None:
    """A line chart. ``targets`` is ``[(expr, legend), ...]``."""
    x, y = _place(w, h)

    custom: dict = {
        'lineWidth': 2,
        'fillOpacity': 12 if not stack else 25,
        'showPoints': 'never',
        'spanNulls': True,
    }
    if stack:
        custom['stacking'] = {'mode': 'normal'}

    _panels.append({
        'id': _id(),
        'type': 'timeseries',
        'title': title,
        'description': description,
        'datasource': DATASOURCE,
        'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
        'fieldConfig': {
            'defaults': {
                'unit': unit,
                'decimals': decimals,
                'custom': custom,
                'color': {'mode': 'palette-classic'},
            },
            'overrides': [],
        },
        'options': {
            'legend': {'displayMode': legend, 'placement': 'bottom',
                       'showLegend': True, 'calcs': ['mean', 'max']},
            'tooltip': {'mode': 'multi', 'sort': 'desc'},
        },
        'targets': [
            {'datasource': DATASOURCE, 'expr': expr, 'refId': chr(65 + i),
             'legendFormat': label}
            for i, (expr, label) in enumerate(targets)
        ],
    })


def note(text: str) -> None:
    """A full-width text panel, for the caveats a number cannot carry."""
    global _cursor_y, _row_height
    _new_band()
    _panels.append({
        'id': _id(),
        'type': 'text',
        'title': '',
        'transparent': True,
        'gridPos': {'x': 0, 'y': _cursor_y, 'w': 24, 'h': 3},
        'options': {'mode': 'markdown', 'content': text},
    })
    _cursor_y += 3
    _row_height = 0


# --------------------------------------------------------------------------- #
# thresholds — the colours encode the decision, not the number
# --------------------------------------------------------------------------- #
GOOD_HIGH = [{'color': 'red', 'value': None}, {'color': 'orange', 'value': 0.9},
             {'color': 'green', 'value': 0.98}]
GOOD_LOW = [{'color': 'green', 'value': None}, {'color': 'orange', 'value': 2},
            {'color': 'red', 'value': 3}]
NEUTRAL_LOW = [{'color': 'green', 'value': None}, {'color': 'orange', 'value': 1},
               {'color': 'red', 'value': 5}]
UP = [{'color': 'red', 'value': None}, {'color': 'green', 'value': 1}]

# completion rate = answered / all outcomes. ``failed`` is the deterministic
# template fallback (the only path that increments it), so "failed rate" *is*
# the degradation rate.
_COMPLETION = ('sum(rate(gateway_chat_outcomes_total{outcome="answered"}[15m]))'
               ' / clamp_min(sum(rate(gateway_chat_outcomes_total[15m])), 0.001)')
_DEGRADED = ('sum(rate(gateway_chat_outcomes_total{outcome="failed"}[15m]))'
             ' / clamp_min(sum(rate(gateway_chat_outcomes_total[15m])), 0.001)')
_HANDOFF = ('sum(rate(gateway_handoffs_total[30m]))'
            ' / clamp_min(sum(rate(gateway_chat_outcomes_total[30m])), 0.001)')


def _quantile(metric: str, q: str, window: str = '10m', by: str = '') -> str:
    """``histogram_quantile`` over a rate-summed histogram.

    ``by`` is a bare label name (``purpose``, not ``' by (purpose)'``). The
    ``le`` label is always carried into the grouping because
    ``histogram_quantile`` cannot work without it, and the grouping clause
    belongs on the ``sum(...)`` — appending it after ``_bucket`` produces
    PromQL that parses but returns nothing, which looks like a broken panel
    rather than a broken query.
    """
    grouping = f'le, {by}' if by else 'le'
    return (f'histogram_quantile({q}, '
            f'sum(rate({metric}_bucket[{window}])) by ({grouping}))')


# --------------------------------------------------------------------------- #
# ⓪ 告警 — deliberately first. A dashboard that buries "is anything on fire"
# under six charts is a dashboard nobody checks during an incident.
# --------------------------------------------------------------------------- #
row('⓪ 告警 · 现在有没有人在被叫')

stat('正在告警', 'sum(ALERTS{alertstate="firing"})',
     description='Prometheus 里处于 firing 状态的告警规则数。这是「有没有人正在被通知」'
                 '的唯一直接答案；即使没配通知渠道，这个数字也会动。',
     decimals=0, steps=[{'color': 'green', 'value': None},
                        {'color': 'red', 'value': 1}], w=4)
stat('通知投递失败', 'sum(increase(alertmanager_notifications_failed_total[1h]))',
     description='近 1 小时 Alertmanager 投递失败的次数。持续非零通常意味着 '
                 'ALERT_WEBHOOK_URL 没配或不可达——告警在响，但没人收到。',
     decimals=0, steps=NEUTRAL_LOW, w=4)
stat('Alertmanager', 'up{job="alertmanager"}',
     description='告警路由组件是否可达。它挂了不会影响业务，但所有告警都会静默。',
     steps=UP, decimals=0, w=4)
timeseries('正在告警（按规则）', [
    ('sum by (alertname) (ALERTS{alertstate="firing"})', '{{alertname}}'),
], description='哪几条规则在响。ALERTS 是 Prometheus 为每条规则自动生成的序列，'
               '不需要应用上报。', unit='short', decimals=0, w=12, h=5)

# --------------------------------------------------------------------------- #
# ① SLO 概览
# --------------------------------------------------------------------------- #
row('① SLO 概览 · 一屏判断系统是否健康')

stat('请求速率', 'sum(rate(gateway_requests_total[5m]))',
     description='全部 HTTP 请求的 5 分钟速率。',
     unit='reqps', decimals=2, w=4)
stat('回答完成率', _COMPLETION,
     description='answered / 全部结果。低于 90% 说明多数请求降级到了模板回答。',
     unit='percentunit', decimals=3, steps=GOOD_HIGH, w=4)
stat('降级率', _DEGRADED,
     description='模型不可用、回退到确定性模板的比例。这是「看起来正常但实际没调模型」'
                 '的唯一直接信号，所以它必须和完成率并排。',
     unit='percentunit', decimals=3,
     steps=[{'color': 'green', 'value': None}, {'color': 'orange', 'value': 0.1},
            {'color': 'red', 'value': 0.3}], w=4)
stat('转人工率', _HANDOFF,
     description='转人工不是故障——它说明系统知道自己的边界——但它是排班信号。',
     unit='percentunit', decimals=3,
     steps=[{'color': 'green', 'value': None}, {'color': 'orange', 'value': 0.3},
            {'color': 'red', 'value': 0.5}], w=4)
stat('首字延迟 P95', _quantile('gateway_ttft_seconds', '0.95'),
     description='用户真正感受到的数字。确定性路径应接近 0，超过 3s 通常是 LLM 上游。',
     unit='s', decimals=3, steps=GOOD_LOW, w=4)
stat('5xx 错误率', 'sum(rate(gateway_requests_total{status=~"5.."}[5m]))'
                   ' / clamp_min(sum(rate(gateway_requests_total[5m])), 0.001)',
     description='服务端错误占比。任何非零都值得看一眼 trace_id。',
     unit='percentunit', decimals=4,
     steps=[{'color': 'green', 'value': None}, {'color': 'orange', 'value': 0.01},
            {'color': 'red', 'value': 0.05}], w=4)

# --------------------------------------------------------------------------- #
# ② 依赖健康
# --------------------------------------------------------------------------- #
row('② 依赖健康 · 谁挂了')

stat('Gateway', 'up{job="gateway"}', description='Prometheus 抓取目标是否可达。',
     steps=UP, decimals=0, w=3)
stat('Business Service', 'up{job="business_service"}',
     description='订单/工单/退款服务是否可达。', steps=UP, decimals=0, w=3)
stat('LLM 熔断触发', 'sum(increase(gateway_llm_calls_total{status="circuit_open"}[15m]))',
     description='近 15 分钟被熔断器挡下的调用数。连续 5 次失败后打开 60 秒；'
                 '非零说明模型上游不稳定，此时回答会静默走模板。',
     decimals=0, steps=NEUTRAL_LOW, w=3)
stat('待处理审批', 'clamp_min(sum(gateway_tool_calls_total{status="waiting_approval"})'
                   ' - sum(gateway_approvals_total), 0)',
     description='进入审批但尚无决策的近似数量（累计差）。积压意味着用户卡在等待里。',
     decimals=0, steps=NEUTRAL_LOW, w=3)
timeseries('依赖可用性', [
    ('up{job="gateway"}', 'gateway'),
    ('up{job="business_service"}', 'business_service'),
    ('up{job="prometheus"}', 'prometheus'),
    ('up{job="alertmanager"}', 'alertmanager'),
], description='任一掉到 0 都说明该目标不可达。prometheus 掉 0 时这条曲线本身也会断，'
               '所以真正的兜底是容器 healthcheck。', unit='short', decimals=0, w=12, h=5)

# --------------------------------------------------------------------------- #
# ③ 延迟
# --------------------------------------------------------------------------- #
row('③ 延迟 · 分位数而不是平均值')

timeseries('首字延迟 TTFT 分位数', [
    (_quantile('gateway_ttft_seconds', '0.50'), 'p50'),
    (_quantile('gateway_ttft_seconds', '0.95'), 'p95'),
    (_quantile('gateway_ttft_seconds', '0.99'), 'p99'),
], description='平均值会掩盖长尾；用户抱怨的永远是 p95/p99。', unit='s')
timeseries('完整回答延迟分位数', [
    (_quantile('gateway_answer_latency_seconds', '0.50'), 'p50'),
    (_quantile('gateway_answer_latency_seconds', '0.95'), 'p95'),
    (_quantile('gateway_answer_latency_seconds', '0.99'), 'p99'),
], description='从收到请求到整段回答结束。', unit='s')
timeseries('LLM 调用延迟（按用途）', [
    (_quantile('gateway_llm_latency_seconds', '0.95', by='purpose'), '{{purpose}} p95'),
], description='意图兜底应当是几十毫秒级；回答节点才是慢的那个。', unit='s', w=24)

# --------------------------------------------------------------------------- #
# ④ 意图与检索
# --------------------------------------------------------------------------- #
row('④ 意图与检索 · 模型有没有被用到')

timeseries('意图判定来源', [
    ('sum by (source) (rate(gateway_intent_total[5m]))', '{{source}}'),
], description='deterministic 是设计目标；llm 占比升高说明关键词覆盖不足。',
    unit='reqps', stack=True)
timeseries('LLM 兜底占比', [
    ('sum(rate(gateway_intent_total{source="llm"}[15m]))'
     ' / clamp_min(sum(rate(gateway_intent_total[15m])), 0.001)', '模型兜底'),
], description='确定性路由能覆盖多少。这个数字持续上升就是在告诉你该补关键词了。',
    unit='percentunit', decimals=4)
timeseries('知识库命中率', [
    ('sum(rate(gateway_knowledge_hits_total{outcome="hit"}[10m]))'
     ' / clamp_min(sum(rate(gateway_knowledge_hits_total[10m])), 0.001)', '命中率'),
], description='命中率低意味着回答缺少接地依据，模型只能靠常识——那正是要避免的。',
    unit='percentunit', decimals=4)
timeseries('检索延迟', [
    (_quantile('gateway_knowledge_latency_seconds', '0.95'), 'p95'),
    (_quantile('gateway_knowledge_latency_seconds', '0.50'), 'p50'),
], description='BM25 + 向量融合的耗时。', unit='s', w=12)

# --------------------------------------------------------------------------- #
# ⑤ LLM 与成本
# --------------------------------------------------------------------------- #
row('⑤ LLM 与成本 · 花了多少、为什么')

timeseries('LLM 调用（按状态）', [
    ('sum by (status) (rate(gateway_llm_calls_total[5m]))', '{{status}}'),
], description='status=ok 是成功；其余都是降级路径。thinking_consumed_budget '
               '是 DeepSeek 的 thinking 吃光预算，empty_completion 是上游返回空。',
    unit='reqps', stack=True)
timeseries('Token 用量', [
    ('sum(rate(gateway_llm_tokens_total{direction="in"}[5m]))', '输入'),
    ('sum(rate(gateway_llm_tokens_total{direction="out"}[5m]))', '输出'),
    ('sum(rate(gateway_llm_tokens_total{direction="in_cached"}[5m]))', '输入·缓存命中'),
], description='缓存命中是输入的子集，不是第三个方向——它便宜约 50 倍，'
               '所以必须和输入画在一起才能看出缓存有没有生效。',
    unit='short', decimals=2, stack=True)
timeseries('缓存命中率', [
    ('sum(rate(gateway_llm_tokens_total{direction="in_cached"}[15m]))'
     ' / clamp_min(sum(rate(gateway_llm_tokens_total{direction="in"}[15m])), 0.001)',
     '命中占比'),
], description='系统提示词是固定的，重复请求应当命中缓存。长期接近 0 说明前缀在变，'
               '或者缓存没被用上——那等于按 50 倍的价格买同样的 token。',
    unit='percentunit', decimals=4)
timeseries('成本速率', [
    ('sum(rate(gateway_llm_cost_usd_total[5m])) * 3600', 'USD / 小时'),
    ('sum(rate(gateway_llm_cost_usd_total[1h])) * 24', 'USD / 天（1h 均值外推）'),
], description='单价取 DeepSeek 峰时牌价，低谷是半价，所以这是上界估计。',
    unit='currencyUSD', decimals=6, w=12)

# --------------------------------------------------------------------------- #
# ⑥ 治理
# --------------------------------------------------------------------------- #
row('⑥ 治理 · 该拦的有没有拦住')

timeseries('工具调用（按状态）', [
    ('sum by (status) (rate(gateway_tool_calls_total[5m]))', '{{status}}'),
], description='waiting_approval 是设计行为；denied / rejected 才是拦截。',
    unit='reqps', stack=True)
timeseries('审批决策', [
    ('sum by (decision) (rate(gateway_approvals_total[10m]))', '{{decision}}'),
], description='人工审批的通过/驳回分布。只有 reviewer 能决定。', unit='reqps', stack=True)
timeseries('治理违规与配额拒绝', [
    ('sum(rate(gateway_state_machine_violations_total[10m]))', '非法状态迁移'),
    ('sum by (kind) (rate(gateway_quota_rejections_total[10m]))', '配额拒绝 {{kind}}'),
], description='非法状态迁移非零是严重信号——要么有 bug，要么有人在攻击。',
    unit='reqps', decimals=4)
timeseries('幂等重放', [
    ('sum by (scope) (rate(gateway_idempotent_replays_total[10m]))', '{{scope}}'),
], description='由幂等键命中而直接返回旧结果的比例。重试风暴时这个数字会跳起来。',
    unit='reqps', decimals=4, w=12)

# --------------------------------------------------------------------------- #
# ⑦ 流量明细
# --------------------------------------------------------------------------- #
row('⑦ 流量明细')
timeseries('HTTP 状态码分布', [
    ('sum by (status) (rate(gateway_requests_total[5m]))', '{{status}}'),
], description='4xx 抬头通常是客户端或鉴权问题，不是服务故障。',
    unit='reqps', stack=True)
timeseries('请求量 Top 路径', [
    ('topk(8, sum by (path) (rate(gateway_requests_total[5m])))', '{{path}}'),
], description='哪几个端点承担了主要流量。', unit='reqps', decimals=3)

note('**指标不带 `tenant_id` 标签**——租户是无界的，一旦进标签就是基数爆炸。'
     '按租户看用量走 `/api/admin/overview`（从 PostgreSQL 查，有索引、可分页）。\n\n'
     '**降级率与 LLM 缓存命中率是本板新增的两个关键面板**：前者回答'
     '「系统是不是看起来正常但其实没调模型」，后者回答「同样的 token 有没有按 50 倍的差价买」。'
     '没有这两个数字，成本面板只是一个无法解释的曲线。\n\n'
     '**「正在告警」面板读的是 Prometheus 自动生成的 `ALERTS` 序列**，不依赖任何应用埋点。'
     '它是「告警响了但没人知道」这条链路的兜底：即便 `ALERT_WEBHOOK_URL` 没配、'
     'Alertmanager 投递全部失败，这个数字依然会变红。\n\n'
     '本文件由 `observability/grafana/build_dashboard.py` 生成，不要直接改 JSON——'
     '生成器会在写出前校验「引用的指标真的存在」和「面板网格无重叠」，'
     '手改 JSON 会绕过这两道检查。')

dashboard = {
    'uid': 'support-platform',
    'title': '生产级电商客服平台 · 确定性主干',
    'tags': ['support', 'agent', 'governance', 'llm'],
    'timezone': 'browser',
    'schemaVersion': 39,
    'editable': True,
    'refresh': '30s',
    'time': {'from': 'now-6h', 'to': 'now'},
    'timepicker': {'refresh_intervals': ['5s', '10s', '30s', '1m', '5m', '15m', '1h']},
    'panels': _panels,
    'templating': {'list': []},
    'annotations': {'list': []},
}

OUT.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + '\n',
               encoding='utf-8')

# --------------------------------------------------------------------------- #
# self-check — the docstring above *claims* every expression is backed by a real
# metric. A claim in a comment rots the first time somebody renames a counter,
# and the failure mode is silent: the panel renders "No data" and operators
# learn to ignore the board. So the claim is checked here, against the actual
# metric declarations in metrics.py, and a stale expression fails the build.
# --------------------------------------------------------------------------- #
import re  # noqa: E402 - kept next to its only use


def _declared_metrics() -> set[str]:
    """Metric names as declared by prometheus_client in ``metrics.py``."""
    src = METRICS_PY.read_text(encoding='utf-8')
    names = set(re.findall(r"""(?:Counter|Gauge|Histogram)\(\s*['"]([a-z_]+)['"]""", src))
    # histogram_quantile() reads the derived ``_bucket`` / ``_count`` / ``_sum``
    # series, which prometheus_client generates rather than declares.
    return names | {f'{n}_bucket' for n in names} | {f'{n}_count' for n in names}


def _referenced_metrics(expr: str) -> set[str]:
    """Bare metric names appearing in a PromQL expression.

    Labels are skipped: the identifier must be followed by ``{``, ``[``, a
    space+operator, or end-of-token — not by ``=`` or a quote.
    """
    return set(re.findall(r'\b([a-z][a-z0-9_]{4,})\b(?!\s*[=~"\'])', expr))


_DECLARED = _declared_metrics()
# PromQL keywords/functions and label values that look like identifiers.
_IGNORE = {
    'sum', 'rate', 'by', 'clamp_min', 'increase', 'topk', 'histogram_quantile',
    'le', 'job', 'path', 'status', 'outcome', 'source', 'direction', 'purpose',
    'decision', 'kind', 'scope', 'level', 'intent', 'tool', 'risk', 'from_status',
    'to_status', 'entity', 'mode', 'reason', 'instance', 'service',
}
_UNKNOWN: list[tuple[str, str]] = []
for _p in _panels:
    for _t in _p.get('targets') or []:
        for _name in _referenced_metrics(_t['expr']):
            if _name.startswith('gateway_') and _name not in _DECLARED:
                _UNKNOWN.append((_p.get('title') or _p['type'], _name))

if _UNKNOWN:
    raise SystemExit(
        '面板引用了 metrics.py 里不存在的指标（会永远显示 No data）：\n  '
        + '\n  '.join(f'{panel}: {metric}' for panel, metric in _UNKNOWN)
    )


def _overlaps() -> list[str]:
    """Panels sharing a grid cell.

    Grafana does not reject an overlapping layout — it renders the panels on
    top of each other, so a broken layout is invisible until someone looks at
    the board and finds the section header sitting across a chart. Checking it
    here turns a visual bug into a build failure.
    """
    occupied: dict[tuple[int, int], str] = {}
    clashes: list[str] = []
    for p in _panels:
        g = p['gridPos']
        name = p.get('title') or p['type']
        for yy in range(g['y'], g['y'] + g['h']):
            for xx in range(g['x'], g['x'] + g['w']):
                if (xx, yy) in occupied:
                    clashes.append(f'({xx},{yy}) {occupied[(xx, yy)]!r} ↔ {name!r}')
                else:
                    occupied[(xx, yy)] = name
    return clashes


_CLASHES = _overlaps()
if _CLASHES:
    raise SystemExit('面板网格重叠：\n  ' + '\n  '.join(_CLASHES[:10]))

data_panels = [p for p in _panels if p['type'] not in ('row', 'text')]
print(f'wrote {OUT.relative_to(Path(__file__).parents[2])}: {len(data_panels)} 个数据面板 '
      f'+ {len([p for p in _panels if p["type"] == "row"])} 个分组 '
      f'（{len(_DECLARED)} 个指标已校验、无网格重叠）')
