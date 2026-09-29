# 面试指南：Dify Production Support Platform

> 目标不是背项目，而是让面试官在 30 秒内知道项目价值，在 10 分钟内看到真实工程能力，并且任何追问都能落到代码、测试或设计取舍。

## 1. 先统一口径

推荐定位：

> 这是一个 production-oriented 的企业 AI 客服工程化项目。它证明了如何把一个 Dify RAG 原型接入真实业务系统，并补上身份、授权、高风险写操作、观测、评估和发布门禁。

不要把它说成：

- “我训练了一个大模型。”项目重点是 AI Application 工程，不是模型训练。
- “已经承载真实生产流量。”仓库具备生产化结构，但公开演示不等于公网生产运营。
- “Dify 已经帮我保证安全。”Dify 负责编排；真正的权限和业务状态由 Gateway 与 Business Service 强制校验。

最可信的表达方式是主动说明边界：**代码能力、测试证据、部署结构和真实上线之间有什么区别。**

## 2. 30 秒电梯讲法

“我做的不是普通聊天机器人，而是一套企业 AI 客服后端。FastAPI Gateway 管身份、密钥、限流、trace 和统一错误；Dify 负责意图路由、RAG、Grounding 和工具编排；Business Service 管订单、工单和退款状态机。最关键的是退款：Agent 只能创建申请，不能批准；人工审核使用独立凭据；业务服务只执行数据库里已经 approved 的申请，并保证一次消费。项目还有 pytest、OpenAPI 合同、DSL 静态检查、发布门禁、Golden/Regression 数据集和 Docker Compose。”

这段里必须保留三个关键词：

1. **三层边界**
2. **独立审批凭据**
3. **可验证证据**

## 3. 3 分钟架构讲法

### 问题

直接把 Dify API 暴露给浏览器会出现四个问题：

- App API Key 暴露，调用额度和应用配置不受保护。
- Dify 只知道 API Key，不知道请求背后的真实用户是谁。
- 订单状态、退款资格和审批结果不能交给 LLM 猜。
- Prompt Injection 可能诱导模型调用高风险工具。

### 解法

把系统拆成三个边界：

| 边界 | 组件 | 负责什么 | 不负责什么 |
|---|---|---|---|
| 控制面 | FastAPI Gateway | JWT/Dev identity、reviewer role、限流、request_id、trace_id、secret boundary、日志和指标 | 不负责 RAG 和业务状态 |
| 智能面 | Dify Workflow | 意图分类、查询改写、RAG、Grounding、固定工具节点、HITL 分支 | 不能直接决定退款是否合法 |
| 确定性面 | Business Service | 订单归属、申请状态、审批结果、幂等执行、审计 | 不处理自然语言生成 |

### 高风险退款链路

```text
create_refund_proposal
  X-Tool-API-Key
  pending_review
        |
        v
review_refund
  X-Reviewer-API-Key
  approved / rejected
        |
        v
execute_refund
  X-Tool-API-Key
  approved -> executed exactly once
```

这里有一个非常值得主动讲的点：**我没有把 Human Input 的 approve 分支当成最终授权。** Human Input 是流程和用户体验；真正的授权事实必须是数据库里的 `approved` 状态，而且只有 reviewer credential 能写这个状态。

### 收尾

项目还补了可观测和回归：结构化日志带 request_id/trace_id，Prometheus/Grafana 暴露请求和 Dify 调用指标，Golden/Regression 数据集做 prompt 和 RAG 回归，CI 运行静态合同和 pytest。最后用 Docker Compose 把 Gateway、Business Service、PostgreSQL、Redis、Nginx、Prometheus、Grafana 串起来。

## 4. 10 分钟现场演示

建议严格按以下顺序，不要从代码开始：

1. 展示架构图：先讲三个边界。
2. 打开 README：展示退款安全不变量和 API。
3. 启动 Business Service 与 Gateway Mock 模式。
4. 请求 `/api/readyz` 和 `/api/chat`，展示 request_id/trace_id。
5. 连续请求触发 429，展示 Redis/内存回退限流。
6. 用 user001 查询 ORD-2001，展示跨用户 404。
7. 创建退款 proposal。
8. 不审核直接 execute，展示 409 `approval_required`。
9. 使用 `X-Reviewer-API-Key` 审批。
10. 再次 execute，展示 executed；重复执行再次 409。
11. 打开 `tests/test_business_rules.py` 和 `service.py`，对应现场效果。
12. 最后运行 `pytest -q` 或展示 CI 配置。

一句收尾：

> 这个项目最重要的不是“模型能回答”，而是模型提出高风险动作之后，系统仍然有独立的授权、状态和证据链。

## 5. 代码走查顺序

面试官让你“带我看看代码”时，按业务信任链走，不要按目录树走。

| 顺序 | 文件 | 看点 |
|---|---|---|
| 1 | `gateway/app/security.py` | JWT 校验、用户身份与 reviewer role 完全由服务端解析 |
| 2 | `gateway/app/main.py` | secret boundary、trace、限流、502/504 映射 |
| 3 | `business_service/app/security.py` | Tool Key 与 Reviewer Key 分离 |
| 4 | `business_service/app/service.py` | proposal/review/execute 状态机与幂等 |
| 5 | `business_service/app/main.py` | 授权依赖如何绑定到具体路由 |
| 6 | `tools/business_api.openapi.yaml` | Agent 工具能力边界，没有 reviewer 操作 |
| 7 | `tools/reviewer_api.openapi.yaml` | 独立 reviewer provider 和独立 credential |
| 8 | `tests/test_business_rules.py` | 拒绝、批准、执行、重放测试 |
| 9 | `.github/workflows/ci.yml` | Python 3.11/3.12 matrix + release gate |
| 10 | `deployment/docker-compose.yml` | 服务拓扑、依赖关系和数据库/Redis |

## 6. 深挖问题与建议回答

### Q1：为什么不让客户端直接调 Dify？

一句话：客户端不能持有 App API Key，Dify 也不负责企业用户身份。

展开：Gateway 持有 Dify key，把认证主体解析成可信 `user`，在服务端调用 Dify。这样浏览器无论换 header、改 body 还是伪造 conversation_id，都不能直接获得 Dify 的调用权。Dify App Key 一旦放到浏览器，调用额度、知识库和工作流配置都会暴露。

### Q2：为什么 LLM 不能直接退款？

一句话：LLM 适合理解意图和填参数，不适合成为最终业务裁决者。

展开：退款涉及归属、退款资格、是否已有申请、是否已审核、是否已执行。这些必须由带事务和状态机的服务处理。模型可能产生幻觉、被 Prompt Injection 影响，也可能重复调用。Business Service 会重新查询 order ownership 和 proposal state，不信任 `user_id`、`order_id` 以外的任何客户端审批标志。

### Q3：你的 HITL 为什么算“真 HITL”？

一句话：Human Input 只是编排，真正的授权写入需要独立 reviewer credential。

展开：Agent Tool 使用 `X-Tool-API-Key`，只能创建 proposal。`/refunds/review` 使用 `X-Reviewer-API-Key`，并且 reviewer identity 来自服务端配置，不来自请求 body。`/refunds/execute` 不接受 `approval_action=approve`，只查找 `approved` 状态的 proposal。这样即使 Agent key 泄露，也不能自行审批。

### Q4：如果 approve 请求被重放怎么办？

一句话：审批和执行是有限状态机，执行通过条件更新消费 approval。

展开：`execute_approved_refund` 要求调用方**指名** `proposal_id`，先按 `proposal_id + user_id + order_id + status=approved` 精确取那一行，再用 `proposal_id + status=approved` 做条件更新，更新行数不是 1 就返回冲突。只有成功消费 approval 的请求才能推进订单状态并写审计。这样同一条 approve 不能无限执行；而且「人批准的那条 proposal」和「这次调用消费的那条」可证明是同一行——不是靠「取最新一条 approved」猜出来的。

### Q5：Prompt Injection 怎么防？

一句话：不能只靠 system prompt，必须是多层防线。

展开：

- Dify 层做 scope guard、意图路由和 Grounding，降低错误分支概率。
- Gateway 把身份固定在 JWT 或服务端 header，不允许模型改用户。
- Business Service 做资源归属和状态机校验。
- Reviewer 能力使用独立 provider 和独立 key，不挂到自治 Agent。
- RAG 文档被当作证据，不被当作指令。

结论：prompt 是行为约束，代码是安全和授权约束，两者不能互换。

### Q6：RAG 和 Tool 怎么分工？

一句话：静态知识走 RAG，实时事实走 Tool。

政策、FAQ、操作手册可以检索；订单、账户、工单和退款状态必须查询业务系统。把订单状态 embedding 化会产生陈旧数据和严重幻觉，即使检索到也不能替代真实数据库状态。

### Q7：Grounding 做了什么？

一句话：回答前要求有证据，无法证明就降级或拒答。

展开：检索后进入 Grounding Validator，校验器输出 `grounded`、`confidence`、`reason` 和最终答案。验收关注的是“是否编造”，而不只是 HTTP 200。测试集包含未知订单、越权订单、提示词泄露和未经确认的退款等 must-not-contain 约束。

### Q8：为什么需要 Gateway 指标和业务审计？

一句话：系统问题和技术问题必须能分别定位。

展开：Prometheus 记录 Gateway 请求、状态码和 Dify 调用延迟；结构化日志带 request_id/trace_id；业务审计记录 ticket_created、refund_reviewed、refund_executed_after_human_review 等事件。模型回答错了、网关挂了、Redis 限流了、业务 state 拒绝了，应该能从不同信号定位。

### Q9：Redis 挂了限流怎么办？

一句话：当前有单进程内存回退，但它只适合开发和降级，不适合多副本生产。

展开：Redis 可用时用 `INCR + EXPIRE` 做共享计数；不可用时进入短暂冷却并回退内存桶，避免每次请求都承担连接失败成本。生产多副本必须使用高可用 Redis，并明确降级策略：继续服务但全局限流失效，还是 fail-closed 拒绝高风险流量。

### Q10：为什么不用 Kafka/Temporal/Kubernetes？

一句话：当前问题规模不需要引入额外分布式复杂度。

展开：单机 Compose 已经能展示完整边界和失败隔离。只有当退款需要跨服务 saga、异步重试或严格工作流持久化时，才值得引入 Temporal 这类工作流引擎。不要为了“看起来生产级”而增加不能解释的组件。

### Q11：Cloud 到 Self-host 怎么迁移？

一句话：业务服务和前端不感知迁移，只替换 Dify 的 base URL、API key 和资源绑定。

展开：Gateway 封装 Dify Service API；DSL 用 Dataset/Tool Provider 占位符，不在仓库写死 Workspace ID；Cloud 与 Self-host 重新绑定模型、知识库和工具后，对外 API 合同保持不变。

### Q12：你怎么知道优化没有把别的 case 弄坏？

一句话：Golden、Regression、Security 数据集和 critical hard gate。

展开：每次修改 prompt、检索参数或 DSL 后重跑数据集；比关键词 must_contain / must_not_contain、HTTP success、tools success 和 critical failure。发布门禁在 critical case 失败时直接失败，而不是只看平均分。

## 7. 常见追问与陷阱

### “这些都是 Dify 做的吧？”

回答重点：Dify 做的是编排，项目实现的是接入边界、权限模型、业务状态机、故障映射、观测、评测和发布门禁。没有 Gateway 和 Business Service，Dify 无法独立保证企业用户身份和退款授权。

### “Human Input 不就能审批吗？”

回答重点：Human Input 能暂停流程，但没有“谁有权审批”和“审批结果是否已持久化”的业务保证。因此我把审批事实放到独立 key 保护的 Business API，Dify 只负责组织和展示。

### “为什么接口里还有 user_id？”

回答重点：user_id 仍然作为业务输入用于查询资源，但服务端不会因为请求声称自己是某用户就返回数据；它会在 `get_owned_order`、account ownership 和 proposal state 上重新校验。生产环境应该进一步用 JWT subject 替换显式 user_id 参数，这是可演进点。

### “你这个评测是不是太简单？”

回答重点：承认它是工程门禁，不是学术 benchmark。它的价值是让常见回归可重复、critical case 可阻断发布，而不是声称覆盖所有语言现象。下一步可加入 LLM-as-judge、人工标注、置信区间和分意图指标。

### “为什么 SQLite 还在？”

回答重点：SQLite 只用于零依赖本地 demo 和单元测试；Compose 使用 PostgreSQL。生产演进下一步是 Alembic migration、连接池参数、备份恢复演练和 schema 版本管理。

## 8. 面试官指出不足时怎么接

成熟的回答结构是：**承认事实 → 说明当前取舍 → 给出明确的下一步 → 指出验证方法。**

| 追问 | 建议回答 |
|---|---|
| 没有真实流量数据 | “这是作品集项目，目前证据是自动化测试和本地验收，不声称线上 SLA。上线后会先做 shadow traffic、灰度和小流量压测。” |
| Reviewer 还是 API Key | “这是可运行的权限边界模型。生产应改成 OIDC/SSO + RBAC，并把 reviewer 身份写入不可篡改审计。” |
| 没有 Alembic | “本地 SQLite 和 Compose 能跑，但 schema 演进确实是下一步。迁移前应先建立版本基线和回滚演练。” |
| Dify 版本绑定 | “DSL 不能跨 Workspace 直接复用，所以仓库保存模板和绑定脚本，最终发布以 Publish Check 和 re-export 为准。” |
| 评测是关键词 | “这是 release regression proxy，不是模型能力全面评测。下一步应加语义评分、人工标注和按意图分层指标。” |
| Redis 仅单实例 | “当前限流逻辑正确但部署是演示级。多副本需要 Redis HA、超时预算和 fail-open/fail-closed 策略。” |

## 9. 简历项目写法

### 精简版

**Dify Production Support Platform｜个人项目**
- 将 Dify RAG 客服原型封装为 FastAPI 企业应用，设计 Gateway / Dify / Business Service 三层边界。
- 实现 JWT 身份、Redis 限流、request_id/trace_id、Prometheus 指标和统一 502/504 故障映射。
- 设计退款 Proposal → Human Review → Execute 状态机，使用 Tool Key / Reviewer Key 分离与条件更新实现授权隔离和幂等。
- 建立 OpenAPI contract、DSL 静态校验、Golden/Regression/Security 数据集、pytest 和自动化 release gate。

### 面试时补的量化口径

只说自己能证明的数字：

- 当前本地测试：`pytest -q` 通过。
- CI：Python 3.11/3.12 matrix 执行 release gate。
- 验收：acceptance 脚本检查健康、知识问答、交叉用户访问和退款授权边界。
- 不虚构 QPS、用户量、线上 SLA 或模型准确率。

## 10. 20 分钟讲法分配

| 时间 | 内容 |
|---|---|
| 0:00–0:30 | 一句话定位与三层架构 |
| 0:30–4:00 | 业务问题、架构图和边界 |
| 4:00–8:00 | 退款授权闭环代码走查 |
| 8:00–12:00 | 现场演示 409 → reviewer → executed |
| 12:00–15:00 | 观测、评测、CI、部署 |
| 15:00–18:00 | 追问：RAG、Prompt Injection、并发、迁移 |
| 18:00–20:00 | 诚实边界与下一步演进 |

## 11. 面试前 30 分钟检查

- 能画出 3 个边界和退款 4 状态。
- 能解释为什么 Reviewer Key 必须独立。
- 能打开 `service.py` 找到 approved 条件更新。
- 能解释 `user_id` 为什么仍是不可信输入。
- 能说清 Dify、Gateway、Business Service 三者的故障表现。
- 能说清 31 个测试覆盖什么，不夸大为“全量生产验证”。
- 能主动讲出 Reviewer Key、SQLite、Alembic、Redis HA 这些真实演进点。
- 演示前使用干净数据库，避免退款订单已被消费。