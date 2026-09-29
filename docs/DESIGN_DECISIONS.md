# 关键设计决策

这份文档用于面试中的“为什么这样设计”追问。每条采用同一个结构：背景、决策、替代方案、代价和验证方式。

## ADR-001：Dify 负责 AI 编排，不负责业务事实

### 背景

Dify 很适合组织 Intent、RAG、LLM、Tool 和 Human Input，但订单状态、退款资格和审批事实需要事务、归属校验和审计。

### 决策

- Dify 负责自然语言理解、知识检索、答案生成和工作流分支。
- Business Service 负责订单、账户、工单、退款状态和审计。
- 静态知识进入 RAG；实时状态通过 Tool API 查询。

### 替代方案

把所有业务数据写入知识库，让模型通过 RAG 查询。

### 代价

系统多了 API 合同、网络调用和工具错误处理，但获得了可验证的事实来源。

### 验证

未知订单不会补全字段；跨用户订单返回 404；退款执行必须看到数据库中的 approved proposal。

## ADR-002：客户端只访问 Gateway

### 背景

Dify App API Key 不能暴露给浏览器。Dify 也不提供企业用户和 reviewer role 的完整身份模型。

### 决策

- Browser 只访问 Gateway。
- Gateway 持有 Dify API Key，并在服务端调用 Dify。
- Gateway 从 JWT 或 dev header 解析 principal。
- reviewer 操作要求 `reviewer` role。

### 替代方案

浏览器直接调 Dify，再由前端逻辑限制高风险操作。

### 代价

Gateway 成为入口依赖，需要处理超时、限流和多副本扩展；但可以统一 secret 和策略。

### 验证

`gateway/app/security.py` 检查 HS256 签名、issuer、exp 和角色；`/api/hitl/{form_token}` 需要 reviewer role。

## ADR-003：Agent Tool Key 与 Reviewer Key 分离

### 背景

如果审批接口和普通工具共用 credential，Agent 被 Prompt Injection 影响后可能直接调用 approve 接口。仅依赖 Human Input 分支不是强授权。

### 决策

- `X-Tool-API-Key` 允许订单、账户、工单和创建退款 proposal。
- `X-Reviewer-API-Key` 只允许审核退款。
- 两套 OpenAPI 和两个 Dify provider 独立配置。
- reviewer 能力不挂到自治 Agent 节点。

### 替代方案

执行接口接收 `approval_action=approve`，由 Dify Human Input 分支负责决定是否传该参数。

### 代价

Dify Workspace 要配置两个 provider 和两个 credential，绑定步骤更复杂。

### 验证

`tests/test_business_auth.py` 验证两种 key 不能互相调用；`tests/test_openapi_contract.py` 验证 Agent OpenAPI 不包含 reviewer 操作；`tests/test_dsl_static.py` 验证模板包含 review 和 execute 节点。

## ADR-004：退款使用显式状态机与一次消费

### 背景

高风险写操作必须支持拒绝、审批、重复执行和并发请求。

### 决策

状态流转固定为：

```text
pending_review -> approved -> executed
pending_review -> rejected
```

执行时只查询 `approved` proposal，并用 `proposal_id + status=approved` 做条件更新。更新行数不是 1 就不能继续推进订单。

### 替代方案

执行接口接受 `approval_action`，遇到 approve 就直接改订单状态。

### 代价

需要维护状态和审计表，并需要为未来数据库迁移建立版本管理。

### 验证

单元测试覆盖：未审批执行失败、拒绝后执行失败、批准后成功、成功后重放失败。

## ADR-005：Redis 限流 + 可解释降级

### 背景

本地 demo 可能没有 Redis，但生产多副本要求共享限流状态。

### 决策

- 正常路径使用 Redis `INCR` 和过期时间。
- Redis 不可用时短暂冷却，回退到单进程内存桶，避免每个请求都等待连接失败。

### 替代方案

所有请求都直接走内存限流。

### 代价

回退期间限流不是全局的，不能用于严格的多副本生产保障。

### 验证

`gateway/app/rate_limit.py` 和 Gateway rate-limit 测试；生产演进需要 Redis HA 和 fail-open/fail-closed 策略。

## ADR-006：为什么没有引入 Kubernetes、Kafka 或 Temporal

### 背景

面试项目容易为了“看起来复杂”增加组件，但无法解释每个组件解决的真实问题。

### 决策

当前单机 Docker Compose 足够展示服务边界、依赖健康检查、数据库、Redis、Nginx、Prometheus 和 Grafana。没有跨服务 saga 或强一致的异步退款需求时不引入 Temporal/Kafka。

### 替代方案

提前使用微服务、消息队列和工作流引擎。

### 代价

系统缺少大规模集群调度和分布式事务能力，但更容易复现、讲解和验证。

### 面试表达

“我会先让单机闭环真实可跑，再根据故障和规模引入组件。组件数量不是生产级，能够解释边界和失败模式才是。”

## ADR-007：模板化 Dify DSL，而不是硬编码 Workspace ID

### 背景

Dataset ID、Tool Provider ID 和模型凭据属于具体 Workspace，直接写进仓库会导致跨环境不可复现。

### 决策

仓库维护 DSL template，保留：

- `__SUPPORT_KNOWLEDGE_DATASET_ID__`
- `__BUSINESS_TOOL_PROVIDER_ID__`
- `__REVIEWER_TOOL_PROVIDER_ID__`

发布时通过 `scripts/patch_dify_dsl.py` 绑定，并在 Dify Studio 执行 Publish Check 后重新 Export。

### 代价

模板不是最终可导入生产 DSL；每次发布需要资源绑定步骤。

### 验证

静态测试检查占位符和节点类型；文档明确最终权威是目标 Workspace 的 Publish Check 与 re-export DSL。

## ADR-008：观测和评测分开

### 背景

“接口返回 200”不能证明 AI 回答正确；模型回答正确也不能说明系统没有隐藏故障。

### 决策

- 技术观测：Prometheus、Grafana、结构化日志、trace_id。
- 业务审计：ticket、proposal、review、execute 事件。
- 质量回归：Golden、Regression、Security 数据集和 critical hard gate。

### 代价

需要维护数据集、标签和报告，不能只依赖人工试用。

### 下一步

引入 OpenTelemetry 全链路 trace、语义评分、人工标注和按场景分层指标。

## ADR-009：生产边界以可验证条件表达

### 背景

作品集最容易失分的地方是宣称“生产级”，但无法回答上线条件和失败处理。

### 决策

在 README 和运行手册中明确：

- Dify template 尚未绑定真实 Workspace 资源时，不算已发布。
- reviewer API Key 是 demo 级身份抽象，生产应接 OIDC/SSO/RBAC。
- SQLite 只用于本地；Compose 使用 PostgreSQL；Alembic 是下一步。
- Redis 回退不是多副本生产方案。
- RAG 质量取决于文档和检索参数，不等同于链路正常。

### 面试价值

主动说出边界和下一步，比隐藏问题更能证明工程判断。