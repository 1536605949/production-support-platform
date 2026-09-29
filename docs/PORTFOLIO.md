# Portfolio Copy

这份文档提供可直接改写的 GitHub、简历和面试介绍文案。原则是突出工程判断与可验证能力，不虚构线上流量或商业指标。

## GitHub 仓库信息

### Description

企业级 Dify AI 客服后端示例：FastAPI Gateway、RAG、Tool Calling、独立人工审批、退款状态机、可观测性、评测与 CI。

### Topics

```text
dify
rag
ai-agent
fastapi
python
prompt-engineering
human-in-the-loop
llmops
observability
docker-compose
```

### 仓库封面短句

> AI may propose. Deterministic systems authorize.

## 一句话项目介绍

**Dify Production Support Platform** 是一个面向企业客服场景的 AI Application 项目：它将 Dify Workflow/RAG 与 FastAPI Gateway、实时业务工具和退款审批状态机结合，重点解决 LLM 接入真实业务后的身份、授权、审计、观测和回归问题。

## 30 秒介绍

“这个项目的重点不是让模型会聊天，而是让模型安全地接业务。Gateway 负责身份、限流、密钥和 trace，Dify 负责 RAG 与工作流编排，Business Service 负责订单和退款等确定性状态。退款必须经过独立的 reviewer credential 审批，Agent key 即使泄露也不能直接批准。项目用 pytest、OpenAPI contract、DSL 静态校验、评测数据集和 CI 发布门禁验证这些约束。”

## 90 秒介绍

“我把它设计成三层。第一层是 FastAPI Gateway，客户端不接触 Dify App Key；Gateway 解析用户和 reviewer role，做限流、request_id/trace_id、结构化日志和统一错误映射。第二层是 Dify，负责意图路由、知识检索、Grounding、固定 Tool 节点和 Human Input 分支。第三层是 Business Service，订单归属和退款状态都在数据库里校验。

最值得讲的是退款链路。Agent 只能创建 proposal，审批接口使用独立的 Reviewer Key，执行接口不接受模型传来的 approve 参数，只读取数据库里的 approved 状态，并通过条件更新保证一次消费。这样即使 Prompt Injection 或 Tool Key 泄露，也不能直接完成退款。

工程侧还有 PostgreSQL/Redis/Docker Compose、Prometheus/Grafana、Golden 与 Regression evaluation、release gate，以及 Python 3.11/3.12 CI。”

## 简历项目条目

### 版本 A：偏后端 / AI 工程

**Dify Production Support Platform｜个人项目**

- 设计 Gateway / Dify / Business Service 三层架构，将 Dify RAG 原型封装为带身份、限流、trace、统一错误和指标的企业 API。
- 实现退款 Proposal → Human Review → Execute 状态机，使用独立 Reviewer 凭据、资源归属校验和条件更新保证高风险写操作不可绕过且只执行一次。
- 建立 Agent/Reviewer 两套 OpenAPI 合同和 Dify DSL 绑定模板，避免把人工审批能力暴露给自治 Agent。
- 使用 pytest、OpenAPI contract、DSL 静态检查、Golden/Regression 数据集和 GitHub Actions release gate 做工程回归。

### 版本 B：偏 AI Application / LLMOps

**Dify Production Support Platform｜个人项目**

- 构建 Dify Workflow + RAG + Grounding + Tool Calling 的企业客服应用，覆盖政策问答、订单/账户查询、工单创建和退款。
- 通过 FastAPI Gateway 建立 secret boundary、JWT/reviewer role、Redis rate limit、request_id/trace_id 和 Prometheus 指标。
- 设计 HITL 安全闭环：Agent Tool Key 只能创建退款申请，Reviewer Key 独立审批，业务服务按数据库状态一次执行。
- 建立 Golden/Regression/Security 数据集、Docker Compose 运行环境和 CI 发布门禁，支持 Cloud/Self-host Dify 迁移。

### 版本 C：项目经历压缩到两行

- 将 Dify RAG 客服原型演进为生产化 FastAPI AI Application，落地身份、限流、trace、可观测、Docker Compose 和自动化发布门禁。
- 设计 Tool Key / Reviewer Key 分离的退款 HITL 状态机，确保 LLM 无法自行批准高风险写操作，并通过测试与 OpenAPI 合同固化安全边界。

## LinkedIn / 个人主页项目描述

> Built a production-oriented Dify support platform that separates AI orchestration from deterministic business state. The system uses a FastAPI gateway for identity, rate limiting, tracing and secret boundaries; Dify for RAG and workflow orchestration; and a stateful business service for orders, tickets and refunds. High-risk refunds require a separate reviewer credential and can be executed only once. The repository includes OpenAPI contracts, automated tests, evaluation datasets, Docker Compose and a CI release gate.

## 面试时的项目价值总结

可以按“工程判断”而不是“功能数量”总结：

1. **边界判断**：知道什么应该由 LLM、编排平台、API Gateway 和确定性服务分别负责。
2. **安全判断**：不会把 prompt 约束当成授权机制，不会让 Agent 自己批准高风险操作。
3. **可靠性判断**：考虑超时、断连、限流、状态迁移和重复执行。
4. **验证判断**：用合同、测试、数据集和 CI 让改动可回归。
5. **沟通判断**：能明确说清真实边界、技术债和下一步，而不是过度包装。

## 不建议写的项目描述

避免以下表达：

- “基于大模型打造全自动企业客服，支持千万级用户。”
- “使用 Dify 实现了完整生产系统。”
- “通过 Prompt 保证退款安全。”
- “模型准确率 99%，QPS 10000。”
- “已接入真实支付和退款。”除非你确实有可公开验证的授权和证据。

更好的表达是：**实现了哪些边界、用什么代码约束、测试能证明什么、还有哪些未完成条件。**

## Demo 录屏封面文案

主标题：

> AI can propose. The system decides.

副标题：

> 从 Dify RAG 原型到可审批、可观测、可回归的企业 AI Application。