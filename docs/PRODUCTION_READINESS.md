# 生产级差距评审

> 评审时间：2026-09-24　对应版本：`v2.2.0`
> 评审对象：`https://124.222.190.3/support/` 上正在运行的这套系统

这份文档回答一个问题：**从「能演示」到「敢上线真实数据」，还差什么？**
按「能不能现在就做掉」分四类，每项都注明**判定依据**——不是凭感觉列的清单。

---

## 一、结论

上一轮（v2.1.0）解决了「模型在控制回路里」的问题，主干是确定性的，
但**运维底座是空的**：schema 只能建不能改、告警响了没人收、备份脚本是
Dify 时代残留且会静默假装成功。这三件事在演示阶段都不影响观感，
在真实数据上都是事故。

本轮把这三件 + 观测看板一起补掉了：

| # | 项目 | 状态 | 判定依据（不是"我觉得"） |
|---|---|---|---|
| 1 | Alembic 迁移 | ✅ | `alembic check` → **No new upgrade operations detected.**（基线精确复现线上 schema） |
| 2 | 告警投递 | ✅ | `activeAlertmanagers` 非空、10 条规则已加载、`LowCompletionRate` 实测进入 `pending` |
| 3 | 备份与还原校验 | ✅ | 还原到临时库后 **18/18 张表行数精确一致** |
| 4 | 备份/校验定时 | ✅ | systemd timer 已 `enable --now`，手动触发实测 `Deactivated successfully` |
| 5 | 观测看板 | ✅ | 32 面板 / 8 分组，生成器自证「66 个指标存在」「网格无重叠」 |
| 6 | CI 门禁 | ✅ | `release_gate.py` 不再在 `LIVE_ACCEPTANCE=0` 时静默跳过全部真实检查 |

**剩余差距集中在两类**：① 需要**外部输入**才能做的（域名、真实通知渠道、
真实评测集）；② 需要**架构决策**的（拆库、多副本、pgvector 迁移）。
没有一项是「还没写」的技术债。

---

## 二、差距矩阵

### A. 已解决（本轮 + 上轮）

| 项 | 之前的状态 | 现在 |
|---|---|---|
| 数据库变更 | `create_all()` 只建不改，加列**静默失效** | Alembic 基线 + `migrate.sh`（自动 stamp 既有库） |
| 告警送达 | 规则在响，`activeAlertmanagers` 为空 | Alertmanager + 分级路由 + 抑制规则 |
| 备份 | 缺环境变量就**静默跳过 pg_dump 却打印成功** | 归档内容校验（18 张表）+ sha256 MANIFEST |
| 还原 | 从未验证过备份能不能用 | `--verify` 还原到临时库逐表比对 |
| 看板 | 手写 JSON，分组标题压住图表 | 生成器 + 写出前两条自校验 |
| 门禁 | `LIVE_ACCEPTANCE=0` 时只跑 pytest | 配置 + 生成物一致性 + 迁移链 + pytest + 真实链路 |
| 前端指标页 | 单一静态快照，无时间维度 | 24h/7d/30d 切换 + 趋势 + 缓存命中 + 来源拆解 |

### B. 可以直接做（无外部依赖，本轮没做是因为超出当前范围）

这些**不需要域名、不需要外部账号、不需要花钱**，是纯粹的工程量：

| 项 | 具体做什么 | 为什么值得 |
|---|---|---|
| **badcase 回归门禁接 CI** | `evaluation/` 现在只有 **7 条**（5 golden + 2 regression），且 `run_eval.py` 没进 CI。要做的：扩充到覆盖 10 条告警规则对应的失败场景，把 `run_eval.py` 挂进 `release_gate.py` 与 GitHub Actions | 现在 CI 能证明「代码没坏」，不能证明「**答得没变差**」。7 条样本连 smoke 都算不上 |
| **Plan Verification 的无环 DAG + 类型契约** | 现在只有工具白名单。补：步骤依赖图做环检测、每步的输入输出类型与 `ToolSpec` 契约比对 | 白名单能防「调了不该调的工具」，防不住「调对了工具但顺序错 / 类型不匹配」 |
| **结果 + 轨迹双评** | 现在只看最终答案。补：对 `workflow_checkpoints` 里的中间步骤也打分（工具选对没、检索命中没、该转人工有没有转） | 答案对可能是蒙的；轨迹错迟早出事 |
| **`/api/admin/overview` 的分页与时间下界** | 现在 `since` 是固定窗口，数据量上来后 `sum(... for m in assistant)` 会全表扫进内存 | 是明确的性能悬崖，数据量翻十倍就会撞上 |
| **PostgreSQL 连接池与慢查询日志** | 加 `pool_size` / `pool_pre_ping`、开 `log_min_duration_statement=500ms` | 4 核 3.6G 的机器上，一条慢查询就能拖垮整个服务 |
| **`observability_check.py` 接进 CI 或定时** | 现在要手动跑。可挂 systemd timer 每小时跑一次，失败即告警 | 检查脚本只有被跑才有价值 |

### C. 需要外部依赖或决策

| 项 | 卡在哪 | 决定后多久能做完 |
|---|---|---|
| **真实通知渠道** | 需要一个企业微信/钉钉/飞书机器人 URL 填进 `ALERT_WEBHOOK_URL`。**现在填的是占位地址**，检查脚本会报 WARN（不是 PASS） | 5 分钟 |
| **域名 + Let's Encrypt** | 需要域名解析权限。现在是自签名证书，浏览器告警 | 半小时（改 Caddyfile + 自动签发） |
| **pgvector 真列迁移** | 需要决定 embedding 模型与维度（现在 `EMBEDDING_ENABLED=0`，向量存 JSON） | 半天（含回填历史数据） |
| **拆库 / 拆 schema** | 需要决定是否真的要隔离。现在是刻意共库，靠 `test_schema_namespaces.py` 守表名唯一 | 1~2 天 |
| **多副本** | 需要决定是否上编排。现在单副本，`/metrics` 的租户标签已刻意去掉（防基数爆炸） | 取决于编排选型 |
| **数据保留策略** | 需要业务决定 `messages` / `workflow_checkpoints` 留多久。现在只增不减 | 半天（加清理任务） |

### D. 明确不做（是决策，不是遗漏）

Kubernetes、Kafka、Temporal Server、多地域、完整 IdP。

理由：当前是**单机 4 核 3.6 GB**。这些组件解决的是「多机调度 / 跨机房一致性 /
企业统一身份」的问题，而本项目在这个规模上遇到的是**内存和单点**问题——
引入它们的运维复杂度远大于收益。真要往上走，顺序应该是
**先垂直扩容 → 再拆读副本 → 最后才谈编排**。

---

## 三、验证方式（四层，缺一不可）

```bash
# ① 进程内逻辑 —— 172 项
.venv/Scripts/python.exe -m pytest -q

# ② 公网真实链路 —— 55 项（鉴权/路由/审批/持久化/SSE/指标）
python3 scripts/acceptance_probe.py https://124.222.190.3/support

# ③ 模型是否真在链路上 —— 30 项（不是静默降级）
python3 scripts/llm_probe.py https://124.222.190.3/support

# ④ 真实页面 DOM 交互 —— 17 项
NODE_PATH="C:/Users/发财/.workbuddy-ai/binaries/node/workspace/node_modules" \
  NODE_TLS_REJECT_UNAUTHORIZED=0 node scripts/ui_smoke_test.mjs https://124.222.190.3/support/

# ⑤ 观测链路（抓取目标 / 告警注册 / 看板挂载）—— 11 项
python3 scripts/observability_check.py
```

本轮实测结果：

| 层 | 结果 |
|---|---|
| pytest | **172 passed**（原 154，+18） |
| acceptance_probe | **55 / 0** |
| llm_probe | **30 / 0**（245 in / 45 out tokens，`degraded=False`） |
| ui_smoke_test | **17 / 0** |
| observability_check | **11 / 0 + 1 WARN**（webhook 占位） |

一次跑完全部：`LIVE_ACCEPTANCE=1 python3 scripts/release_gate.py`
→ `RELEASE GATE PASS（静态 + 单元 + 真实链路）`

本地 ↔ 服务器 6 个关键文件 sha256 **逐一相同**。

### 为什么必须是四层

本项目累计 **20 个真实缺陷**，其中多个在 pytest 全绿时依然存在：

- 身份未映射到订单系统 → dev 模式恰好能跑，JWT 下每次订单查询变「越权」
- 审批结论未回写业务系统 → 网关记「已批准」，退款**静默**不发生
- `/metrics` 的 307 逃出 `/support/` 前缀 → 落到隔壁站点，返回别人的登录页，状态码还是 200
- 告警规则在响但 `activeAlertmanagers` 为空 → UI 显示 firing，**没人被通知**
- MANIFEST 把正在写入的自己算进哈希 → 校验永远 FAILED
- 备份行数比对用 `n_live_tup`（估算值）→ 产生与真实丢数据**无法区分**的假告警

**容器全 Up、接口全 200，不等于系统能干活。**

---

## 四、观测数据分析页优化

### Grafana 看板（32 个数据面板 / 8 个分组）

从手写 JSON 改为 `observability/grafana/build_dashboard.py` 生成。
28 个面板的 PromQL 原始 JSON diff 无法 review，必然腐坏；生成器能在写出前自证：

1. **引用的指标真实存在** —— 从 `gateway/app/metrics.py` 正则提取所有
   `Counter` / `Gauge` / `Histogram` 名，逐面板扫 `expr`，引用不存在的
   `gateway_*` 指标即非零退出。否则面板永远显示 `No data`，且没人知道为什么。
2. **网格无重叠** —— 逐格占用检查。原布局引擎每个分组标题都压在上方图表上
   （游标从未推进 band 高度），检查抓到 **44 处冲突**后重写布局引擎。

分组结构（⓪ 刻意放在最前）：

```
⓪ 告警 · 现在有没有人在被叫    ← ALERTS{alertstate="firing"}，不依赖应用埋点
① 流量与结果
② 延迟（TTFT / 端到端）
③ 意图与路由（确定性 vs LLM 兜底）
④ 工具与审批
⑤ 知识库检索
⑥ 成本
⑦ 依赖可用性（4 个 job：gateway / business_service / prometheus / alertmanager）
```

⓪ 分组读的是 Prometheus 为每条规则**自动生成**的 `ALERTS` 序列，
不依赖应用埋点——这正是「告警响了没人知道」的兜底。

### 前端指标页（`demo_client/index.html`）

| 改动 | 细节 |
|---|---|
| 时间范围切换 | 24h / 7d / 30d，带手动刷新与数据时间戳 |
| 每日趋势 | 迷你柱图；**降级量画成柱内橙色子集**，不是另一根柱——降级是同一批请求的一部分，画成并列柱会误导 |
| 缓存命中 | 新增卡片；`route_cache_hit` / `compose_cache_hit` 贯穿到持久化 `meta.cache_hit_tokens`，刷新后仍能解释「这次为什么便宜」 |
| 来源拆解 | 意图/措辞分别拆成「确定性 / LLM 兜底 / 模板」 |
| 逐日补齐 | `/api/admin/overview` 的 `daily` 从起始日到今日**逐日补齐**，空日填 0。否则前端柱图会把没数据的日子直接跳过，**时间轴是错的** |

---

## 五、下一步建议顺序

1. **填 `ALERT_WEBHOOK_URL`**（5 分钟）—— 让检查脚本从 WARN 变 PASS，告警真正送达
2. **扩充评测集 + 接 CI**（1 天）—— 把「答得没变差」也纳入门禁
3. **绑域名换 Let's Encrypt**（半小时）—— 消掉浏览器证书告警
4. **`/api/admin/overview` 加分页**（半天）—— 消掉明确的性能悬崖
5. 再往后才是 pgvector 迁移、拆库、多副本
