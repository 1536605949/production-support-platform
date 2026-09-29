# 架构说明

本文回答两个问题：**目标架构的十项能力各自落在哪段代码里**，以及**为什么这样切**。

> **产品视角的完整文档见 [`docs/PRODUCT.md`](PRODUCT.md)**（定位、场景、功能清单、
> 数据模型、接口契约、验收结果、已知限制）。运维操作见 `docs/OPERATIONS.md`。
> 本文只讲架构与取舍。

---

## 0. 一句话概括

**确定性主干，LLM 只挂在两个节点上，治理层卡在所有写操作前面。**

意图识别、工具选择、状态迁移、金额计算全部由可读的规则与数据库约束完成；
模型只做两件事：低置信时的意图兜底，以及有依据时的措辞组织。它**不能**选择工具、
**不能**决定是否批准、**不能**改变状态。

这样切的理由是：客服系统的失败代价不对称。答得平淡只是体验问题，
而「模型自作主张退了一笔款」是资金事故。把模型关进两个只读节点，
它的输出最多让回答变差，不会让系统做错事。

---

## 1. 运行时拓扑

```
浏览器
  │
  ▼
Caddy ── /api/* ──▶ FastAPI Gateway ──▶ 确定性主干（工作流）
  │                      │
  │                      ├─▶ PostgreSQL  会话/消息/审批/工作流检查点/幂等键
  │                      ├─▶ Redis       限流与配额计数
  │                      └─▶ Business Service  订单/工单/退款（独立鉴权）
  │
  └── /* ──▶ 静态前端
```

TLS 由宿主机上的共享边缘 nginx 终结（80/443 已被另一个项目占用，
安全组也只放行 22/80/443），本项目的 Caddy 只做应用层路由。
详见 `docs/OPERATIONS.md` 第 1 节。

### 为什么网关和业务服务是两个进程

不是为了微服务好看，是因为**凭据必须分开**。Agent 侧持有 `TOOL_API_KEY`，
它能创建退款申请，但业务服务拒绝用它批准退款；批准需要 `REVIEWER_API_KEY`，
而那个 Key 不在 Agent 的进程里。合成一个进程，这个边界就只剩注释了。

---

## 2. 十项能力的落点

| # | 能力 | 主要实现 | 状态 |
|---|---|---|---|
| 1 | 多租户 + 鉴权 + 配额 | `core/tenant.py` `core/auth.py` `security.py` `models.py` | ✅ |
| 2 | 对话持久化 | `core/conversation.py` `models.py` `routers/conversations.py` | ✅ |
| 3 | 意图路由 + LLM 兜底 + 三级转人工 | `core/intent.py` `core/engine.py` `core/llm.py` | ✅ |
| 4 | 知识库混合检索 | `core/knowledge.py` | ⚠️ 仅 BM25 通道 |
| 5 | 治理：白名单/参数/风险/审批/幂等 | `core/governance.py` | ✅ |
| 6 | 规划校验 | — | ❌ 未实现 |
| 7 | 持久化工作流外壳 | `core/workflow.py` `models.py` | ✅ |
| 8 | 可观测 | `core/observability.py` `metrics.py` `observability/` | ✅ |
| 9 | 评测闭环 | `tests/` `scripts/` `evaluation/` | ✅ 含回归门禁（已接 CI） |
| 10 | 结构化业务事实 | `business_service/app/` | ✅ 库存/价格/退货资格查表；资格三值 |

---

## 3. 逐项说明

### 3.1 多租户与鉴权

**两级隔离**：`tenant_id`（计费与数据边界）+ `store_id`（同租户多店铺）。
`tenant_id=''` 是保留值，表示**平台全局**文档——所有租户可读，无人可改。
这是新租户第一天就有内容可用的原因。

**越权过滤先于打分**：`knowledge._visible_documents()` 在检索**之前**按
`(tenant_id, store_id)` 过滤候选集，而不是检索完再筛。顺序反了就会出现
「越权文档拿到高分、又被过滤掉」，表现为召回率莫名下降——排查时很难想到是权限问题。

**JWT 是自己实现的**（`core/auth.py`，HS256 + PBKDF2 200k）：
不是为了炫技，是为了零加密依赖。关键细节是**先验 `alg` 再验签**——
不验算法就等于允许 `alg=none` 绕过签名。

**令牌里的租户是唯一的租户来源**。请求头里带 `X-Demo-Tenant` 只会被用来
**比对**，不能用来拓宽范围；不匹配直接 403。

**配额与限流是两件事**：Redis 做每分钟限流（突发），PostgreSQL `quota_usage`
做每日消息配额（累积）。前者丢了不影响正确性，后者必须持久。

### 3.2 对话持久化

`conversations` + `messages`，`messages` 上有唯一约束 `(conversation_id, seq)`。
唯一约束不是洁癖：它让「同一轮写入两次」变成数据库层面的冲突，
而不是两条看起来都正常的重复消息。

消息行同时记录 `ttft_ms` / `latency_ms` / `tokens_in` / `cost_usd` / `sources`。
这样「首字延迟」「单轮成本」这类指标可以直接从会话回读，不依赖监控系统。

### 3.3 意图路由

```
加权关键词打分
   │
   ├─ 置信度 ≥ 0.55 ────────────▶ 直接用（deterministic）
   │
   ├─ 0.35 ≤ 置信度 < 0.55 ─────▶ 调 LLM 兜底（llm）
   │                                └─ 模型也不确定 → 建议转人工（level 1）
   │
   └─ 置信度 < 0.35 ────────────▶ 自动转人工（level 2）
```

**挫败感检测被提到置信度判断之前**。原因是：用户连着问三次「到底什么时候退」，
第三次的关键词可能匹配得很好、置信度很高，但正确的动作是转人工。
把挫败感判断放在置信度快捷返回之后，这条路径永远不会触发。

**LLM 只在低置信时被调用**，且失败时静默降级为模板回答（熔断器 5 次）。
降级不是故障：没有模型时整条主干依然可用，这是刻意的。

### 3.4 知识库

`tokenize()` 是中文友好的，且**不依赖分词库**：ASCII 词 + CJK 单字 + CJK 二元组。
「退款到账」会被切成 `退款`/`到账`/`退款到账`，兼顾精确与召回。

排序用 **RRF（Reciprocal Rank Fusion）** 融合两路：`score(d) = Σ 1/(k+rank)`。
用排名而不是分数，是因为 BM25 分和余弦相似度量纲完全不同，直接加权需要调参，
而排名融合无需归一化。

⚠️ **当前只跑了 BM25 一路**：`EMBEDDING_ENABLED=0`，向量通道代码在但未启用，
`embedding` 字段也还是 JSON 而非真正的 `vector` 列。

### 3.5 治理（核心）

一次工具调用要过五道闸（`governance.invoke`）：

| 闸 | 检查 | 失败结果 |
|---|---|---|
| 1 | 工具在白名单 `TOOL_REGISTRY` 里 | `tool_not_whitelisted` |
| 2 | 参数过 Pydantic 模型 | 400，且**不会**透传原始参数 |
| 3 | 角色门（可选） | 403 |
| 4 | 风险门：高危 → `waiting_approval` | 停在等待人工 |
| 5 | 结果过 `validate_result` | 契约违规，不进上下文 |

**审批只能消费一次**，靠条件 UPDATE 而不是先读后写：

```python
claimed = db.query(ToolInvocation).filter(
    ToolInvocation.id == invocation_id,
    ToolInvocation.status == 'approved',      # ← 条件写进 UPDATE
).update({'status': 'executed'})
if claimed != 1:
    raise ToolDenied('approval_already_consumed')
```

并发下两个请求同时进来，只有一个能把 `approved` 改成 `executed`（影响 1 行），
另一个影响 0 行直接退出。先读后写做不到这一点。

**审批结论必须回写到业务系统。** 这是本次部署踩出来的坑：
网关自己的 `approvals` 表**不是**订单系统的审批状态机。
只在本侧标记 `approved`，订单系统仍然回 `approval_required`，退款永远不执行——
而且网关这一侧看起来一切正常。所以 `execute_refund` 的 `ToolSpec` 上多了
`review_path='/refunds/review'`：执行前先用**审阅凭据**把人工结论送过去。

**幂等键是表，不是约定**：`idempotency_keys` 有唯一约束，
并发插入靠捕获 `IntegrityError` 后回读获胜者，而不是先查后插。

**身份映射是显式的**（`users.business_customer_id`）。
平台用户 id 是 `U-customer`，订单系统客户 id 是 `user001`，两者不是一回事。
早期代码直接把平台 id 当工具参数传下去，在 dev 模式下恰好能跑
（那里默认身份就叫 `user001`），一换成真实 JWT 登录，每次订单查询都变成
「越权拒绝」——看起来像权限 bug，实际是映射 bug。

### 3.6 规划校验（未实现）

目标是三层：白名单（已有）、**无环 DAG**、**类型契约**。
当前只有第一层。这一项是第二阶段的首要工作。

### 3.7 持久化工作流外壳

`workflow_runs` + `workflow_checkpoints` 用 PostgreSQL 实现，
接口对齐 Temporal 的一个子集：`start` / `run` / `signal` / `query` /
`get_result` / `cancel`。

节点序列：

```
ingest → route → retrieve → plan_tools → compose → persist
                                              │
                                    ┌─────────┴─────────┐
                                    ▼                   ▼
                              await_approval ──signal──▶ notify
                                 (park)
```

`persist` 是 `proceed` 而不是 `done`——这是刻意的：如果它在 `await_approval`
之前就结束，恢复后的 `notify` 就落不了库，用户永远看不到审批结果。

**故意不装 Temporal Server**：第一版只要接口对齐，
以后要换真 Temporal 时改的是实现而不是调用方。3.6 GB 内存的机器上
多跑一个 Temporal 集群不划算。

**等待中的 run 拒绝无信号推进**：`run()` 遇到 `waiting` 且没有 `_signals` 时直接返回。
少了这条，轮询或重试会把停在审批的 run 直接推过去。

### 3.8 可观测

`TraceContext` 是 frozen dataclass + `contextvars`，在中间件里绑定，
之后每个日志行、每条落库记录都带着 `trace_id`。一个 grep 能重建一次请求。

**指标不带 `tenant_id`**。租户是无界的，进标签就是基数爆炸，
Prometheus 会在真实流量下先扛不住。需要租户维度时走 `/api/admin/overview`
（查 PostgreSQL，有索引、能分页），而不是查监控。

指标覆盖：TTFT、完整延迟、完成率、转人工率、意图来源占比、知识库命中率、
LLM 调用/token/成本、工具调用、审批决策、非法状态迁移、幂等重放、配额拒绝、鉴权失败。
共 26 个指标族；仪表盘 **32 个数据面板 / 8 个分组**，**10 条告警规则分 4 组**
（可用性/质量/治理/成本）。

### 3.9 评测闭环

- **单元/集成**：**238 个 pytest**（6 项显式跳过并写明依赖），覆盖意图路由、治理、
  租户鉴权、知识库、工作流、迁移树、商品目录与售后规则、数据集。
- **分层验收**：端到端 55 / LLM 链路 30 / 运营台 UI 17 / C 端 UI 60 / 观测链路 11 / 评测 18。
- **回归门禁**：`evaluation/run_eval.py` 与基线比对，18 条 badcase 断言**轨迹**而不只是答案；
  `tests/test_eval_dataset.py` 让它在每次提交都在进程内跑一遍。

完整清单与最近一次验收结果见 `docs/PRODUCT.md` 第 11 章。

**为什么单独写端到端探针而不是只跑 pytest**：
pytest 打的是进程内 `TestClient`，它看不见边缘 nginx 的前缀重写、
Caddy 的 SSE 缓冲、以及 `/metrics` 那个会逃出前缀的 307 跳转。
本次部署中三个真实缺陷全部是探针抓出来的，pytest 全绿：

1. 平台身份没映射到订单系统身份 → 订单查询 422
2. 审批结论没回写业务系统 → 退款永远不执行
3. `/metrics` 307 跳到隔壁站点 → 返回别人的登录页，状态码还是 200

---

### 3.10 结构化业务事实（商品目录与售后规则）

知识库与规则表是**刻意的双轨**：同一份退货政策，一个回答「规则**是**怎样的」，
另一个回答「**这一单**能不能退」。**只放向量库的规则拒绝不了任何东西**——
它只能被召回，不能被校验。

`business_service` 因此多了 `sku` / `aftersale_rule` 两张表，`orders` 多了
`sku_id` / `signed_at` 两列（**可空且无默认值**：在商品表存在之前下的单没有 SKU，
给它补一个就是编造事实；缺签收时间不能读成「已过期」）。

对应的只读工具 `get_sku` / `check_aftersale_eligibility` 走既有的五道闸，
因此天然进审计、幂等与可观测。退货资格的判定是**三值**的，详见
`docs/PRODUCT.md` 第 6.6 节——`null` 表示「无法判定」而**不是拒绝**。

---

## 4. 信任链（退款）

```
TOOL_API_KEY   → 创建提案        → pending_review
REVIEWER_API_KEY → 审核提案      → approved / rejected     ← 只有人能到这步
TOOL_API_KEY   → 执行已批准提案  → executed（恰好一次）
```

三处关键约束：

1. **审阅接口用另一套 OpenAPI 与另一把 Key**，Agent 的 Key 打不通它。
2. **执行读数据库状态，不读请求参数**。调用方传 `approved=true` 没有任何作用。
3. **执行失败不退回可重试状态**。审批已被消费，失败就是 `failed`，
   要再来一次必须重新走一遍人工审批——否则「失败重试」会变成绕过审批的后门。

---

## 5. 明确的非目标

以下都是**故意不做**，而不是遗漏：

- **不引入 Kubernetes / Kafka / Temporal**：当前是单机 4 核 3.6 GB，
  这些组件带来的运维复杂度远大于收益。
- **不做多地域**：没有这个需求，做了也只是纸面架构。
- **不做完整 IdP**：自建 JWT 够用，接 SSO 需要真实身份源。
- **不把模型放进控制回路**：这是核心立场，不是阶段性妥协。
