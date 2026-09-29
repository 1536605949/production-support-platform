# 生产级电商客服 Agent 平台

> 把 RAG 客服原型推进为可部署、可观测、可审批、可回归的企业级 AI Application。

**线上演示：** https://124.222.190.3/support/ 　（自签名证书，浏览器首次访问会提示风险）

演示账号：`customer@demo.local` / `DemoCustomer123`（租户填 `tenant-demo`）

---

## 这个项目在解决什么

不是「给聊天机器人套个前端」。核心问题是：

> **当 LLM 开始调用订单、工单、退款这类真实业务能力时，系统边界该划在哪？**

答案不是「加个 prompt 让它别乱来」，而是**把模型从控制回路里拿出来**：

```
确定性主干（规则 + 数据库约束）
   ├── 意图识别 ── 加权关键词打分
   ├── 工具选择 ── 代码里的映射表，不是模型输出
   ├── 状态迁移 ── 数据库条件 UPDATE
   └── 金额计算 ── 业务服务
        │
        └── LLM 只挂两个只读节点
              ├── 低置信时的意图兜底
              └── 有依据时的措辞组织
```

模型**不能**选工具、**不能**决定是否批准、**不能**改状态。
它的输出最多让回答变平淡，不会让系统做错事。这个取舍的依据是失败代价不对称：
答得差是体验问题，自作主张退了一笔款是资金事故。

---

## 十项能力

| 能力 | 实现要点 |
|---|---|
| **1 多租户** | `tenant_id`（计费/数据边界）+ `store_id`（多店铺）；行级权限**在打分之前**过滤候选集；JWT 自实现（先验 `alg`）；令牌里的租户是唯一租户来源 |
| **2 对话持久化** | PostgreSQL `conversations`/`messages`，`(conversation_id, seq)` 唯一约束；消息行记录 TTFT/延迟/token/成本 |
| **3 意图路由** | 加权关键词优先，低置信才调 LLM，三级转人工；挫败感检测前置 |
| **4 知识库** | BM25 + 向量 RRF 融合；中文友好分词（ASCII 词 + CJK 单字 + bigram），无分词库依赖 |
| **5 治理** | 五道闸：白名单 → 参数 Pydantic → 角色门 → 风险门 → 结果契约；高危进人工审批；幂等键是表不是约定 |
| **6 规划校验** | ⚠️ 仅白名单，无环 DAG 与类型契约待做 |
| **7 持久化外壳** | PostgreSQL 工作流 + checkpoint，接口对齐 Temporal 子集，预留平滑切换 |
| **8 可观测** | `contextvars` 全链路 trace、结构化日志、32 个数据面板 / 8 个分组的 Grafana、10 条告警规则 + Alertmanager 投递（含抑制规则） |
| **9 评测闭环** | 315 个 pytest（6 项显式跳过并写明依赖）+ 分层验收（端到端 55 / LLM 链路 30 / 运营台 UI 17 / C 端 UI 60 / 观测链路 11）+ 18 条 badcase 回归门禁，均已接入发布门禁与 CI |
| **10 结构化业务事实** | 商品目录 `sku` + 售后规则 `aftersale_rule` 落表；库存、价格、退货资格一律**查表**得出。退货资格返回**三值** `true`/`false`/`null`，`null` 表示「无法判定」而**不是拒绝** |

第 10 项和第 4 项是刻意的双轨：退货政策在知识库里回答「规则是怎样的」（自然语言、可召回），
在 `aftersale_rule` 里回答「**这一单**能不能退」（可硬校验）。只放向量库的规则拒绝不了任何东西。

---

## 架构

```
浏览器
  │  https://124.222.190.3/support/
  ▼
共享边缘 nginx (:443)          ← 宿主已有，本项目不占 80/443
  │  /support/ → support-caddy:8080
  ▼
Caddy ── /api/*  ──▶ FastAPI Gateway ──▶ 确定性主干
  │                      │
  │                      ├─▶ PostgreSQL   会话/审批/工作流检查点/幂等键
  │                      ├─▶ Redis        限流与配额
  │                      └─▶ Business Service  订单/工单/退款/商品/售后规则（独立凭据）
  │
  └── /*  ──▶ 静态前端（两个）
```

### 两个前端，一个后端

| 页面 | 路径 | 给谁 | 显示什么 |
|---|---|---|---|
| **客服工作台** | `/support/` | 开发 / 运营 | 对话 + 审批队列 + 指标，**外加决策检查器**（意图、置信度、召回来源、工具调用、耗时、trace_id） |
| **C 端页面** | `/support/customer.html` | 用户 | 只有对话。业务事实（商品、订单）渲染成卡片，**意图/来源/工具/降级标记一律不显示** |

两者调同一组 `/api/chat/stream`，差别只在「把哪些字段画出来」。
C 端页面把业务事实做成卡片、把管线内部信息全部丢掉，是有意的：
用户需要知道「这个商品有货」，不需要知道「意图分类置信度 0.85」。

**完整产品文档见 [docs/PRODUCT.md](docs/PRODUCT.md)**（定位、场景、功能清单、数据模型、
接口契约、验收结果、已知限制——从设计到实际，含如实标注的缺口）。
设计取舍见 [docs/architecture.md](docs/architecture.md)，运维见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

### 为什么网关与业务服务是两个进程

不是为了微服务好看，是**凭据必须分开**。Agent 持有 `TOOL_API_KEY`，
能创建退款申请但批不了；批准需要 `REVIEWER_API_KEY`，那个 Key 不在 Agent 进程里。
合成一个进程，这条边界就只剩注释了。

---

## 退款信任链

```
TOOL_API_KEY     → 创建提案        → pending_review
REVIEWER_API_KEY → 审核提案        → approved / rejected    ← 只有人能到这步
TOOL_API_KEY     → 执行已批准提案  → executed（恰好一次）
```

三处关键约束：

1. **执行读数据库状态，不读请求参数**。调用方传 `approved=true` 没有任何作用。
2. **审批只消费一次**，靠条件 UPDATE（`filter(id, status=='approved').update()`），
   并发下只有一方影响 1 行，另一方直接退出。先读后写做不到。
3. **执行失败不退回可重试状态**。审批已被消费，失败即 `failed`，
   要再来必须重走人工审批——否则「失败重试」就是绕过审批的后门。

还有一处容易被忽略：**审批结论必须回写业务系统**。
网关自己的 `approvals` 表不是订单系统的审批状态机，只在本侧标记通过，
订单系统仍回 `approval_required`，退款永远不执行，而网关侧看起来一切正常。
这个坑是上线时端到端探针抓出来的。

---

## 退货资格：为什么规则要落表，以及为什么判定是三值

退款链路上加了一道只读闸门，在 `plan_tools` **之前**跑：

```
意图=refund 且 有订单号
      │
      ▼
check_aftersale_eligibility(order_id)      ← 查 aftersale_rule 表
      │
      ├── eligible = false  →  直接拒绝，不创建申请、不挂审批
      ├── eligible = true   →  照常走「创建申请 → 人工审批 → 执行」
      └── eligible = null   →  照常走（**不拦**）
```

两个设计点值得单独说：

**一、规则落表，而不是只放知识库。** 退货政策在知识库里是散文，用来回答
「规则是怎样的」；在 `aftersale_rule` 里是行，用来回答「这一单能不能退」。
只放向量库的规则**拒绝不了任何东西**——它只能被召回，不能被校验。

**二、`null` 不等于 `false`。** 规则引擎返回三值：

| 值 | 含义 | 处理 |
|---|---|---|
| `true` | 找到规则，且满足 | 放行 |
| `false` | 找到规则，且违反 | **拒绝** |
| `null` | **无法判定**（订单没有 SKU / 类目没有规则 / 没有签收时间） | **放行** |

把 `null` 折叠成 `false` 会让系统开始拒绝它本该受理的退货，而且看起来像「严格」而不是 bug。
所以这道闸门只在**有确凿不通过证据**时才拦。规则服务连不上时同理——**读超时不是拒绝**。

拒绝本身也**不交给模型措辞**：`status='blocked'` 直接走确定性回答，理由引用规则行原文。
否则用户就有了一个回合去把它劝成别的答案。

---

## 快速开始

```bash
# 本地起全套（需要 Docker）
cd deployment
cp .env.example .env          # 填上所有 REPLACE_*
docker compose up -d --build

# 跑测试
pip install -r requirements-dev.txt
pytest -q                     # 315 passed, 6 skipped

# 分层验收（后几层对着真实部署跑）
python3 scripts/acceptance_probe.py https://124.222.190.3/support   # 55 项：鉴权/路由/审批/持久化/SSE/指标
python3 scripts/llm_probe.py        https://124.222.190.3/support   # 30 项：模型是否真的在链路上（不是静默降级）
bash scripts/ui_smoke.sh            https://124.222.190.3/support   # 17 项：运营台真实页面 DOM 交互
bash scripts/ui_smoke.sh https://124.222.190.3/support \
     scripts/ui_smoke_customer.mjs                                  # 60 项：C 端页面，并断言内部字段一个都不渲染
python3 scripts/observability_check.py                              # 11 项：抓取目标 / 告警注册 / 看板挂载

# badcase 回归门禁（答案文本 + 轨迹，与基线比对）
python3 evaluation/run_eval.py https://124.222.190.3/support        # 18 条：意图/工具/转人工/降级/注入
```

UI 冒烟需要 `jsdom`，而本仓库没有 `package.json`，所以它得知道 jsdom 在哪。
`scripts/ui_smoke.sh` 是唯一负责这件事的地方（`make ui` 和 `release_gate.py` 都走它）：

```bash
npm i jsdom                                   # 方案 1：装在项目里，之后无需任何变量
JSDOM_HOME=/path/to/node bash scripts/ui_smoke.sh <url>   # 方案 2：指向已装 jsdom 的目录
NODE_PATH=/path/to/node_modules bash scripts/ui_smoke.sh <url>   # 方案 3
```

找不到时它会**把这三个解法打出来**再退出，而不是丢一个 `exit 2` 让人以为是 UI 回归。

`pytest` 里那 6 项跳过是**刻意**的：它们断言的行为需要订单服务或模型在链路上，
进程内跑会**因为错误的原因通过**（工具调用一律 failed，于是「越权被拒」也成立）。
数据集里用 `live_only` 标出依赖，并有一条测试守着「能在 CI 跑」的用例数下限——
否则「逐条都合理」的跳过会把覆盖率悄悄降到零。

### 数据库迁移与备份

```bash
bash scripts/migrate.sh upgrade       # 应用迁移（首次会从既有 schema 自动 stamp 基线）
bash scripts/migrate.sh check         # 模型与库是否漂移
make revision M="add refund audit column"   # 生成迁移（自动编号 + 断言文件落盘）
bash scripts/backup.sh                # pg_dump + Redis BGSAVE + sha256 MANIFEST
bash scripts/verify_latest_backup.sh  # 还原到临时库逐表比对行数（不碰线上库）
```

> 两个迁移脚本都以 `-v "$ROOT:/app"` 运行 alembic。**这不是方便，是必需**：
> 源码是 COPY 进镜像的、`gateway` 服务没有 bind mount，不挂载的话
> `upgrade` 会「成功」但不应用任何迁移，`check` 会**恰好在有漂移时报「无漂移」**。
> 详见 `docs/OPERATIONS.md`「改 schema 的正确姿势」。

备份/校验已注册为 systemd timer：每天 `03:20` 备份，每周日 `04:30` 校验，
均 `Persistent=true`（关机错过的会在开机后补跑）。

### 当前部署状态

| 项 | 值 |
|---|---|
| 主机 | 腾讯云 `124.222.190.3`（Ubuntu 24.04 / 4 核 / 3.6 GB） |
| 入口 | 共享边缘 nginx + 项目内 Caddy，安全组仅放行 22/80/443 |
| 引擎 | `builtin`（确定性主干），Dify 保留为可选模式 |
| LLM | **DeepSeek `deepseek-flash`**（`LLM_ENABLED=1`，thinking 关闭）。失败静默回退，主干独立可用 |
| 鉴权 | `jwt` |
| 迁移 | **Alembic**（基线精确复现线上 schema，`alembic check` 无漂移；迁移工具挂载工作副本运行） |
| 监控 | Prometheus + Grafana + Alertmanager，仅 `127.0.0.1`，走 SSH 隧道 |
| 连接池 | `DB_POOL_SIZE=5` + `DB_MAX_OVERFLOW=10`；每条连接带 `idle_in_transaction_session_timeout=300000` |

---

## 本次上线修复的缺陷

以下问题在 pytest 全绿时**依然存在**，全部由端到端探针或实际运行抓出：

| 缺陷 | 为什么危险 |
|---|---|
| 平台身份未映射到订单系统身份 | dev 模式恰好能跑（默认身份就叫 `user001`），换成 JWT 登录后每次订单查询都变成「越权拒绝」，看起来像权限 bug |
| 审批结论未回写业务系统 | 网关记录「已批准」，订单系统仍拒绝执行，退款**静默**不发生 |
| `/metrics` 的 307 逃出 `/support/` 前缀 | 落到本机另一个站点，返回别人的登录页，状态码还是 200 |
| GET 工具参数被丢弃 | 业务服务收到缺 `user_id` 的请求直接 422，所有权检查根本没跑 |
| 两服务共用库且表名冲突 | `create_all` 按名字幂等，第二个声明静默失效，模型从此不可用 |
| Grafana `GF_SERVER_HTTP_ADDR=0.0.0.0:3000` | 被当成主机名解析，容器重启循环 |
| 迁移工具读的是镜像不是工作副本 | `upgrade` 打印「完成」却一条迁移都没应用；`check` 恰好在有漂移时报「无漂移」；`make revision` 生成的文件随 `--rm` 容器一起消失 |
| 确定性回答读错了金额字段 | `get_order` 返回 `amount`（元），代码读 `amount_cents`；`int(None)` 被 `except` 吞掉，于是回答里印出「金额 **None**」。只在**模型不可用或刚失败**时才走到这条路径——也就是最不能出错的那条 |

教训：**容器全 Up、接口全 200，不等于系统能干活。** 唯一可信的验收方式是走真实链路跑端到端。

---

## 诚实的边界

**已实现**：多租户、鉴权、持久化、意图路由、知识库（BM25 单路）、治理与审批、
工作流外壳、可观测、冒烟与集成测试、完整部署、**Alembic 迁移**、**告警投递**、
**备份与还原校验**、**badcase 回归门禁接 CI**、**连接池与空闲事务护栏**、
**商品目录与结构化售后规则**、**C 端用户页面**。

**未实现**：
- **规划校验只有白名单**，无环 DAG 与类型契约待做。
- **向量检索未启用**，`EMBEDDING_ENABLED=0`，embedding 仍存 JSON 而非 `vector` 列。
- **结果 + 轨迹双评**未做（现在的评测断言轨迹字段，但还没有独立的 LLM 评审）。
- **自签名证书**，浏览器会警告；绑域名后换 Let's Encrypt 即可。
- **单副本**：无多实例；PostgreSQL/Redis 未做主从。
- **告警没有真正的接收端**：`ALERT_WEBHOOK_URL` 仍是占位地址，规则会路由、
  通知发不出去（`observability_check.py` 对此报 WARN 而不是 PASS）。
- **`0002` 索引在当前数据量下不会被用到**，这是对的（365 行全部落在窗口内）；
  它的收益要到「租户历史远大于窗口」时才出现，实测见 `CHANGELOG.md`。
- **售后规则只覆盖无理由退货**（`rule_type='return_no_reason'`）。质量问题、仅退款、
  运费赔付这些规则类型表结构支持，但没有种子数据，因此查不到规则时会返回
  `eligible=null` 而不是编一条出来——这是有意的保守，不是遗漏。
- **C 端页面没有会话历史列表**：刷新即开新会话。后端 `/api/conversations` 已有，
  补一个列表是纯前端工作。

**明确不做**（不是遗漏）：Kubernetes、Kafka、Temporal Server、多地域、完整 IdP。
当前是单机 4 核 3.6 GB，引入这些的运维复杂度远大于收益。

---

## 目录结构

```
gateway/            FastAPI 网关 + 确定性主干
  app/core/         intent / knowledge / governance / workflow / engine / tenant / auth
  app/routers/      auth / chat / conversations / approvals / admin
business_service/   订单、工单、退款、商品目录、售后规则的确定性状态机（独立凭据）
demo_client/        演示前端：index.html 客服工作台 + customer.html C 端页面（均单文件、SSE 流式）
migrations/         Alembic 迁移（env.py + versions/，基线复现线上 schema）
deployment/         compose + Caddyfile + 边缘 nginx 片段 + systemd 定时单元
observability/      Prometheus 规则 + Alertmanager 路由 + Grafana provisioning
  grafana/build_dashboard.py   看板生成器（写出前自校验指标存在性与网格无重叠）
scripts/            边缘注入、端到端验收、LLM 探针、迁移、备份/还原校验、发布门禁
tests/              315 个 pytest
docs/              产品文档（PRODUCT.md）、架构、运维、设计决策
```
