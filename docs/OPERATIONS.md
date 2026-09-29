# 运维手册 · 生产级电商客服平台

部署主机：腾讯云 `124.222.190.3`（Ubuntu 24.04，4 核 / 3.6 GB）
公网入口：**https://124.222.190.3/support/**
代码目录：`/home/ubuntu/support-platform`
编排目录：`/home/ubuntu/support-platform/deployment`

> 本文只写「怎么操作」。为什么这样设计，见 `docs/ARCHITECTURE.md`。

---

## 1. 拓扑与端口归属

```
浏览器
  │  https://124.222.190.3/support/
  ▼
novel-nginx  (:443, 属于另一个项目 /home/ubuntu/novel-agents)
  │  location /support/ → http://support-caddy:8080/
  ▼
support-caddy  (本项目, 只监听内网 :8080)
  ├── /api/*    → gateway:8000   (SSE 不缓冲)
  ├── /metrics  → gateway:8000   (内部 rewrite，见第 6 节)
  └── /*        → 静态页 /srv (demo_client/index.html 运营台 · customer.html 用户端)
        │
        ▼
   gateway (:8000, 仅内网)
     ├── postgres  (pgvector:pg16)
     ├── redis     (限流 / 配额)
     └── business-service (:9000, 仅内网)
```

**端口归属**（这台机器上 80/443 只有一个属主，所以必须说清楚）：

| 端口 | 属主 | 说明 |
|---|---|---|
| 80 / 443 | `novel-nginx` | 另一个项目。只做 TLS 终结与 `/support/` 转发 |
| 8080 | `support-caddy` | 只在 Docker 内网，**不发布到宿主** |
| 8000 / 9000 | gateway / business-service | 只在 Docker 内网 |
| 9090 | Prometheus | 仅 `127.0.0.1`，走 SSH 隧道 |
| 3000 | Grafana | 仅 `127.0.0.1`，走 SSH 隧道 |

安全组只放行 22 / 80 / 443，所以 **8080 之外没有别的选择**——这也是为什么
TLS 由 `novel-nginx` 终结、本项目只做应用层反代。

---

## 2. 日常操作

```bash
cd /home/ubuntu/support-platform/deployment

docker compose ps                    # 看状态
docker compose logs -f gateway       # 跟日志
docker compose restart gateway       # 重启单个服务
docker compose up -d                 # 应用改动
docker compose down                  # 停止（保留数据卷）
```

### 改代码后怎么生效

```bash
# 1) 从本地同步（在本地机器上执行）
python remote.py putdir <本地项目> /home/ubuntu/support-platform

# 2) 在服务器上重建并重启
cd /home/ubuntu/support-platform/deployment
docker compose build gateway && docker compose up -d gateway
```

> `gateway` 与 `business-service` 的代码是**打进镜像**的，所以改代码必须 rebuild。
> `Caddyfile` / `observability/` 是 bind mount，改文件即可（见下一节）。

### 三个「改了不生效」的坑（都踩过）

| 改了什么 | 为什么不会自动生效 | 正确做法 |
|---|---|---|
| `deployment/Caddyfile` | 它是 bind mount，容器配置哈希不变，Compose 不会重建 | 已加 `--watch`，Caddy 会自己热加载。若没生效：`docker exec support-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile` |
| `observability/grafana/**` | Grafana **只在启动时**读 provisioning 目录 | `docker compose restart grafana` |
| `observability/prometheus.yml`、`alerts.yml` | 同上，Prometheus 启动时读 | `docker compose restart prometheus` |

---

## 3. 健康检查

```bash
# 容器层
docker compose ps

# 应用层
curl -sk https://124.222.190.3/support/api/healthz
curl -sk https://124.222.190.3/support/api/readyz

# 端到端（55 项，从任何能访问公网的机器跑）
python3 scripts/acceptance_probe.py https://124.222.190.3/support

# 模型是否真的在链路上（30 项，断言 token 与成本真的动了）
python3 scripts/llm_probe.py https://124.222.190.3/support

# 前端 UI（17 项，需要 jsdom —— 由 wrapper 负责定位）
bash scripts/ui_smoke.sh https://124.222.190.3/support

# 观测链路（11 项，在服务器上跑；Prometheus/Grafana 只监听 127.0.0.1）
python3 scripts/observability_check.py

# 答案质量回归（18 条，与基线比对）
python3 evaluation/run_eval.py https://124.222.190.3/support

# 全跑（静态 + 单元 + 真实链路）
LIVE_ACCEPTANCE=1 .venv/Scripts/python.exe scripts/release_gate.py
```

`readyz` 会告诉你 `engine_mode` / `auth_mode` / `llm_enabled` / 数据库是否通。
它返回 `not_ready` 时先看 `checks` 里哪一项是 `false`。
（**LLM 状态刻意不进 `checks`** —— 模型挂了不该让服务被摘掉。）

**四层验证缺一不可：**

| 层 | 覆盖什么 | 覆盖不到什么 |
|---|---|---|
| `pytest`（315，6 项显式跳过） | 进程内逻辑：意图、治理、租户、工作流、迁移树、数据集、商品目录与售后规则、plan 校验、响应契约 | 边缘 nginx 前缀重写、Caddy 缓冲、真实 TLS、真实模型 |
| `acceptance_probe.py`（55） | 公网真实链路：鉴权/路由/审批/持久化/SSE/指标 | 前端 JS 能不能跑起来；模型是否真被调用 |
| `llm_probe.py`（30） | 模型确实被调用：token 与成本来自 provider 的 `usage` | 答案质量 |
| `ui_smoke_test.mjs`（17） | 真实 DOM 里跑真实页面：登录、流式渲染、Tab、退出 | 后端细节（它只关心页面能否用） |
| `ui_smoke_customer.mjs`（60） | C 端页面在真实 DOM 里跑：登录、流式累积、商品卡/订单卡、流中断提示、401 退回登录，并断言**意图/置信度/来源/工具名/trace_id 一个都不渲染** | 后端细节（同上） |
| `observability_check.py`（11） | 抓取目标、告警注册、看板挂载 | 告警有没有真的送到人（webhook 空时它报 WARN） |
| `evaluation/run_eval.py`（18） | 答案质量与轨迹，以及相对基线的回归 | 审批信任链（那部分归 `acceptance_probe.py`） |

只看容器 `Up` 没有意义。本项目的真实缺陷里，绝大多数**全部**是后几层抓出来的，
当时 pytest 全绿——包括迁移工具一直在读镜像而不是工作副本那一次。

**探针的两种模式**（它自己是 state-aware 的，不会假装测过）：

| 数据状态 | 结果 | 说明 |
|---|---|---|
| 干净（刚重置过） | 55 通过 / 0 失败 | 审批闭环完整跑一遍 |
| `ORD-1001` 已被退过款 | 44 通过 / 0 失败 / 1 跳过 | 审批段**显式 SKIP**并打印原因 |

跳过不是失败，但也不等于通过——要验审批闭环，先按第 7 节重置演示数据。
持久化那条断言（`消息已落库`）的下限跟着本轮实际发出的轮次走，
所以跳过审批段不会把它误报成失败，而真丢写仍然会被抓到。

---

## 4. 账号与凭据

演示账号（`AUTO_SEED=1` 时在启动阶段幂等创建）：

| 邮箱 | 口令 | 角色 | 订单系统身份 |
|---|---|---|---|
| `customer@demo.local` | `DemoCustomer123` | customer | `user001`（拥有 ORD-1001） |
| `agent@demo.local` | `DemoAgent123` | agent | — |
| `reviewer@demo.local` | `DemoReviewer123` | reviewer | — |
| `admin@demo.local` | `DemoAdmin123` | admin | — |

**上线后请立刻改掉这些口令，或把 `AUTO_SEED=0` 关掉种子。**
种子只补不改：口令一旦在库里存在就不会被覆盖。

真实密钥都在 `deployment/.env`（已加入 `.gitignore` / `.dockerignore`）：

| 变量 | 用途 | 泄露后果 |
|---|---|---|
| `JWT_SECRET` | 签发登录令牌 | 可伪造任意租户任意用户的身份 |
| `POSTGRES_PASSWORD` | 数据库 | 全量数据 |
| `TOOL_API_KEY` | 网关调业务服务的凭据 | 可**申请**退款，但**不能**批准 |
| `REVIEWER_API_KEY` | 网关回写人工审批结论的凭据 | 可**批准**退款 —— 权限比上面那个大 |
| `GRAFANA_ADMIN_PASSWORD` | 运维面板 | 指标只读 |

> `TOOL_API_KEY` 与 `REVIEWER_API_KEY` 分开是刻意的：Agent 侧拿的是前者，
> 它能发起退款申请但永远批不了；后者只在「人工审批通过」那一次被网关用来
> 把结论回写到订单系统。合成一个，等于让 Agent 自己批准自己。

---

## 5. LLM 接入（DeepSeek，已启用）

**当前接入 DeepSeek，`LLM_ENABLED=1`。** 确定性主干依然独立可用：坏 Key、断网、
超时都会静默回退，请求仍然返回 200。

### 模型只在两个只读节点上

| 节点 | 干什么 | 代码 |
|---|---|---|
| 意图兜底 | 关键词置信度低于 `INTENT_LLM_THRESHOLD` 时，在**固定候选集**里选一个 | `LLMClient.classify_intent()` |
| 措辞组织 | 已有知识库接地依据时，把依据组织成自然语言 | `LLMClient.chat_stream()` |

**模型不能做的事**（这是架构约束，不是配置项）：不能选工具、不能决定审批、
不能改状态、不能扩大候选意图集。工具由 `plan_tools()` 按意图确定性映射，
审批由条件 UPDATE 决定，模型碰不到这两条路径。

### 调用方式

OpenAI 兼容，`POST {LLM_BASE_URL}/chat/completions`：

```bash
curl https://api.deepseek.com/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -d '{"model":"deepseek-flash",
       "messages":[{"role":"system","content":"只输出 JSON"},
                   {"role":"user","content":"我要退货"}],
       "temperature":0,"max_tokens":80,
       "response_format":{"type":"json_object"},
       "thinking":{"type":"disabled"}}'
```

### 参数配置

| 变量 | 当前值 | 说明 |
|---|---|---|
| `LLM_ENABLED` | `1` | 0 = 完全走确定性主干 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | **不要带 `/v1`**，客户端自己拼 `/chat/completions` |
| `LLM_MODEL` | `deepseek-flash` | 官方文档名（DeepSeek-V4.1-Flash） |
| `LLM_API_KEY` | 已配置 | 只写在 `.env`，已被 `.gitignore` 忽略 |
| `LLM_THINKING` | `disabled` | 见下 |
| `LLM_TIMEOUT_SECONDS` | `30` | |
| `LLM_MAX_TOKENS` | `800` | 回答节点上限；意图分类固定用 80 |
| `LLM_PRICE_IN_PER_1K` | `0.0003` | 峰时牌价，USD/1K |
| `LLM_PRICE_IN_CACHED_PER_1K` | `0.000006` | 缓存命中输入，比未命中便宜 50 倍 |
| `LLM_PRICE_OUT_PER_1K` | `0.0012` | |

单价取**峰时**牌价（低谷是半价），是保守估计。DeepSeek 在
01:00–04:00 与 06:00–10:00 UTC 为峰时，其余（含周末）为低谷。

**`LLM_THINKING` 为什么默认 `disabled`** —— DeepSeek V4 默认开启 thinking，
先把 `max_tokens` 花在 `reasoning_content` 上。意图分类只有 80 token 预算，
实测会被推理吃光、`content` 返回**空字符串**，随后 JSON 解析失败、
**意图兜底静默失效**（表面看服务一切正常）。关掉它，两个节点都只需要一个短答案。

换到不认识这个字段的端点时报 400：客户端会**自动去掉该字段重试一次**并记住结果，
不会每次调用都失败一次。

### 异常处理

所有失败都返回 `LLMResult(error=...)` 而**不抛异常**，调用方回退确定性模板：

| `error` | 触发条件 | 表现 |
|---|---|---|
| `llm_disabled` | 未启用 / 无 Key | 直接走模板，不发起请求 |
| `circuit_open` | 连续失败 5 次后的 60 秒内 | 不再打上游（半开探测一次） |
| `http_4xx` / `http_5xx` | 上游返回错误码 | 记日志 + 指标，回退模板 |
| `TimeoutError` 等 | 网络异常 / 超时 | 同上 |
| `thinking_consumed_budget` | 只有 `reasoning_content`、`content` 为空 | **单独区分**，不当成正常空回答 |
| `empty_completion` | 上游返回空内容 | 同上 |

降级在三处可见，不用猜：

1. 回答气泡带 **「模板回退」** 标记，且**刷新后仍在**（存进 `messages.meta.degraded`，
   经 `MessageOut.degraded` 暴露）；
2. `gateway_llm_calls_total{status="..."}`（`ok` / `error` / `http_5xx` / `circuit_open` / …）；
3. 结构化日志 `llm_call` / `llm_error` / `llm_exception` / `llm_thinking_ate_budget`。

### 改配置

```bash
cd /home/ubuntu/support-platform/deployment
vi .env
docker compose up -d gateway          # 只重启，不用重建镜像
curl -sk https://124.222.190.3/support/api/readyz | python3 -m json.tool
```

`readyz` 的 `llm` 字段给出 `available` / `model` / `thinking` / `circuit_open` /
`consecutive_failures`。

**LLM 状态刻意不进 `checks`**：模型挂掉不该让服务变成 `not_ready` 被负载均衡摘掉——
主干本来就不依赖模型。

---

## 6. 可观测

### 走 SSH 隧道看面板

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 ubuntu@124.222.190.3
# 浏览器打开 http://localhost:3000   （admin / .env 里的 GRAFANA_ADMIN_PASSWORD）
#             http://localhost:9090  （Prometheus 原始查询）
#             http://localhost:9093  （Alertmanager：正在告警 / 静默 / 分组）
```

Grafana 里已 provision 好仪表盘「**生产级电商客服平台 · 确定性主干**」
（**32 个数据面板 + 8 个分组**）：

| 分组 | 回答的问题 |
|---|---|
| ⓪ 告警 | 现在有没有人在被叫（`ALERTS{alertstate="firing"}`、投递失败数） |
| ① SLO 概览 | 请求速率、完成率、**降级率**、转人工率、TTFT p95、5xx 率 |
| ② 依赖健康 | `up{job}` 四个目标、LLM 熔断触发、待处理审批 |
| ③ 延迟 | TTFT / 完整回答 / LLM 各自的 p50·p95·p99 |
| ④ 意图与检索 | 意图判定来源、模型兜底占比、知识库命中率、检索延迟 |
| ⑤ LLM 与成本 | 调用状态、Token 用量（含**缓存命中**）、**缓存命中率**、成本速率 |
| ⑥ 治理 | 工具调用、审批决策、非法状态迁移、幂等重放 |
| ⑦ 流量明细 | HTTP 状态码分布、Top 路径 |

**看板是生成的，不要手改 JSON。** 源文件是 `observability/grafana/build_dashboard.py`：

```bash
make dashboard            # 或 python observability/grafana/build_dashboard.py
docker compose -f deployment/docker-compose.yml restart grafana   # provisioning 只在启动时读
```

生成器在写出前会**自校验两件事**，任一不满足就非零退出：

1. **引用的指标真实存在**（对照 `gateway/app/metrics.py` 的声明）。
   面板查一个没人上报的指标会永远显示 "No data"，然后教会运维忽略这块板。
2. **面板网格无重叠**。Grafana 不会拒绝重叠的 layout，它会直接把面板画在彼此上面——
   直到有人打开看板才发现分组标题横穿了一张图。把它变成构建失败。

历史上这两类问题都真实发生过：看板曾引用过一个不存在的指标，而 8 个分组标题
**每一个**都压在上方图表上（游标没有推进过当前 band 的高度）。

### 告警：10 条规则，谁来送

`observability/alerts.yml` 里的规则由 Prometheus 求值。**但规则本身不会通知任何人**——
Prometheus 只把状态从 inactive 变成 firing，需要 Alertmanager 才有人收到。

```bash
# 看一眼当前状态
curl -s http://127.0.0.1:9093/api/v2/alerts | python3 -m json.tool | head -40
```

`ALERT_WEBHOOK_URL` **留空**时：告警仍然会出现在 Alertmanager UI 和看板的「正在告警」
面板上（那读的是 Prometheus 自带的 `ALERTS` 序列，不依赖任何投递），但不会推送出去，
Alertmanager 日志里每次触发会有一条
`no such host alert-webhook-not-configured.invalid`——**这条报错是刻意的**，
它直接点出"没配通知渠道"，比一个 localhost 的 405 有用得多。

`scripts/observability_check.py` 会读容器里**实际生效**的那个地址
（`/tmp/alertmanager-webhook-url`，不是 `.env` 里写的），若仍是占位值就报
**WARN 而不是 PASS**：「结构正常，但没在干活」。
把"有 receiver"和"receiver 能送到人"当成两件事，是因为这一整套脚本存在的理由
就是抓这种「每一层都在跑、没有一层在起作用」的状态。

填企业微信 / 钉钉 / 飞书机器人 URL（或任何接受 JSON POST 的地址）即可真正收到：

```bash
# deployment/.env
ALERT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
```

```bash
docker compose --env-file deployment/.env -f deployment/docker-compose.yml \
  up -d alertmanager
```

告警规则里还配了两条**抑制规则**：网关挂掉时，5xx / TTFT / 完成率 / 成本会一起响，
那些是**结果不是原因**，会被抑制掉，值班的人只看得到 `GatewayDown` 一条。

**刻意不从公网暴露 Grafana / Prometheus / Alertmanager**：三个都只监听
`127.0.0.1`，走 SSH 隧道访问。带登录页的运维面板没必要占用公网攻击面，
而且 Alertmanager 的静默/分组操作一旦被外人拿到就是一个静音按钮。

### `/support/metrics` 为什么存在

Prometheus 在**内网**直接抓 `gateway:8000/metrics`，不经过这里。
公网那个 `/support/metrics` 只是给人临时排查用的，里面只有计数器和直方图，不含用户数据。

⚠️ Caddyfile 里那行 `rewrite * /metrics/` 是**必须的**，删了会出怪事：
Starlette 会把 `/metrics` 以 307 跳到 `/metrics/`，而这个 `Location` 是**绝对路径**，
在 `/support/` 前缀下会逃出前缀，落到本机**另一个站点**的根上——表现为
「访问指标返回了隔壁网站的登录页，状态码还是 200」。

### 指标基数约束

**所有指标都不带 `tenant_id` 标签**。租户是无界的，一旦进标签就是基数爆炸。
需要按租户看用量，走 `/api/admin/overview`（从 PostgreSQL 查，有索引，且能分页）。

---

## 7. 重置演示数据

退款流程**故意是有状态的**：退过一次，订单就变成不可再退——这正是验收要验的性质。
代价是同一套数据只能完整演示一次。重置：

```bash
cd /home/ubuntu/support-platform
bash scripts/reset_demo_data.sh
```

脚本自己定位 `deployment/docker-compose.yml`，**从任何目录跑都可以**（早先的版本要求先
`cd deployment`，从项目根跑会以 `no configuration file provided: not found` 静默失败——
看起来像成功，其实一个字段都没改）。执行完脚本会**自校验** `ORD-1001` / `ORD-2001`
均已恢复可退款，不满足就 `RAISE EXCEPTION` 退出码非 0，不会留一个"假成功"。

只清业务服务的演示表（`refund_proposals` / `tickets` / `audit_events`），并把
`ORD-1001`、`ORD-2001` 的状态与可退款标志复位，**不动**平台的会话、审批与审计记录——
那些是演示完给人看的证据。

---

## 8. 数据库迁移（Alembic）

**为什么要有它**：服务的建表方式是 `Base.metadata.create_all()`，它**只建不改**。
给已有表加一列不会生效、也不会报错，直到第一次查询才炸 `UndefinedColumn`。
上线真实数据之后，第一次改 schema 就是一次事故。

### 日常命令

```bash
cd /home/ubuntu/support-platform

bash scripts/migrate.sh            # 应用迁移（首次会自动 stamp 基线）
bash scripts/migrate.sh check      # 模型与迁移是否漂移 —— 部署前必跑
bash scripts/migrate.sh current    # 当前数据库在哪个 revision
bash scripts/migrate.sh sql        # 只打印 SQL，不执行
```

`make migrate` / `make migrate-check` 是同样的东西。

### 首次接管已有库

生产库是 `create_all()` 建的，**没有 `alembic_version` 表**。直接
`alembic upgrade head` 会去 CREATE TABLE 一堆已存在的表，第一个冲突就中止，
留下一个"既不是旧 schema 也不是新 schema"的库。

`scripts/migrate.sh` 会先探测这个状态：

```
已有 schema 但没有 alembic_version —— 这是 create_all() 建出来的库。
先把基线标记为已应用（不执行任何 DDL），再升级。
```

`alembic stamp 0001_baseline` 只写一行版本号，不执行 DDL。

### 改 schema 的正确姿势

```bash
# 1) 改 gateway/app/models.py 或 business_service/app/models.py
# 2) 生成迁移（脚本会挂载工作副本、自动编号、并断言文件落盘）
make revision M="add refund audit column"
# 3) 看一眼生成的文件，确认 DDL 是你要的
# 4) 部署
bash scripts/migrate.sh check && bash scripts/migrate.sh upgrade
```

**autogenerate 是「模型 vs 当前数据库」求差分**，不是「模型 vs 空库」。所以数据库
必须先在 head 上，否则待应用的那条迁移会被再生成一遍（下次部署报 `already exists`）。
脚本会把这个错误翻译成一句可执行的提示。

#### `migrate.sh` 与 `new_migration.sh` 都挂载了工作副本 —— 这是必需的

两个脚本都以 `-v "$ROOT:/app"` 运行 alembic。**去掉这个挂载，两条命令都会静默地
描述镜像而不是工作副本**，实测过：

| 命令 | 去掉挂载后的行为 |
|---|---|
| `migrate.sh upgrade` | `head` 解析成镜像知道的那个 head，**一条迁移都不应用**，却照样打印「完成」——实测时 `alembic_version` 停在 `0001_baseline`，而 `0002_analytics_indexes` 就躺在磁盘上 |
| `migrate.sh check` | 拿镜像里的 `models.py` 和数据库比，于是**恰好在有漂移时报「无漂移」**——正是它存在的理由那一刻失效 |
| `new_migration.sh` | 文件写进 `--rm` 容器的可写层，随容器一起删除；命令返回 0，文件没了。且 autogenerate 对比的是镜像里的旧模型，改了 `models.py` 会生成一条**空迁移** |

原因在 `gateway/Dockerfile`：源码是 **COPY** 进去的，`gateway` 服务**没有** bind mount。
所以镜像是源码的一份**快照**。挂载工作副本让这两条命令描述的对象，等于下一次
`up --build` 会烤进镜像的那份源码——这才是一次迁移应该描述的东西。

`--user "$(id -u):$(id -g)"` 是为了别在宿主机仓库里留下 root 属主的 `__pycache__`。

`make revision` 不直接调 alembic，而是走 `scripts/new_migration.sh`：它负责自动
编号（`000N_slug`）、拒绝随机 hex、并**在跑完之后断言迁移文件真的出现在宿主机上**。
`tests/test_migration_tooling.py` 守着这几条，其中两条已用「把修复回退掉」验证过
确实会失败。

`alembic.ini` 的 `file_template` 是 `%(rev)s`，**不带 `.py`**：alembic 会自己加扩展名，
写成 `%(rev)s.py` 会产出 `0002_foo.py.py`——那不是 Python 模块，下一条 `upgrade`
找不到它刚写出来的 revision。（这个坑是拿真部署跑出来的，不是读文档读出来的。）

`migrations/env.py` 里 **`target_metadata` 是一个 list，同时包含两个服务的 Base**。
两个服务共用一个库、共用一份 schema 历史，所以只有一棵迁移树——两棵会互相抢
`alembic_version`。如果只挂网关的 metadata，autogenerate 会把业务服务的每一张表
都当成"要删掉的表"，下一条迁移就会把订单数据删了。

`compare_type` 与 `compare_server_default` **两条路径都打开**：autogenerate 只报告
被要求比较的东西，关掉之后把 `String(32)` 改成 `String(64)` 会生成一条**空迁移**，
正好是这棵迁移树要消灭的那类静默失效。

`AUTO_CREATE_SCHEMA` 在 `.env` 里已置 **0**。它开着的时候新加的模型会被
`create_all` 建出来而**不经过迁移**，等于把漂移重新引进来。只在用完就丢的
临时库上才设 1。

第一条真实迁移是 `0002_analytics_indexes`（给 `messages` / `conversations` 补
`(tenant_id, created_at)` 索引）。它值得记一笔，因为它验证了这套工具链在
**真实变更**上确实可用：`alembic check` 无漂移、`downgrade` 再 `upgrade` 可逆。
注意文件里那段「大表要用 `CREATE INDEX CONCURRENTLY`」的说明——普通
`CREATE INDEX` 会阻塞写入，现在表小所以无所谓，表大了就不是了。

第二条是 `0003_products_and_rules`（`sku` + `aftersale_rule` 两张表，
以及 `orders` 上的 `sku_id` / `signed_at` 两列）。它演示的是
**「加列必须走迁移」这条规矩本身**：

- 两张**新表**交给 `create_all()` 其实也会建出来——所以只看表存不存在，看不出问题。
- 但 `orders` 上的两列不会。`create_all()` 只建不改，一张已存在的表加列**既不生效
  也不报错**，直到第一次查这一列才炸 `UndefinedColumn`——也就是第一次有人问退货的时候。

两列都 **nullable 且没有 server default**，这是有意的。给已有数据的表加 NOT NULL 列
必须给默认值，而这里任何默认值都是撒谎：`sku_id` 的 `''` 和 `signed_at` 的 epoch
都会被读成真实值。`signed_at` 一旦被写成假时间戳，退货窗口就会按这个假起点计算，
系统会开始**拒绝它本该受理的退货**。宁可让它为 `null`，由业务层回答「无法判定」。

### 连接池与流式并发

**`/api/chat/stream` 在整个 SSE 响应期间占着一条数据库连接。** 原因是
FastAPI 的 `yield` 依赖（`get_db`）在**响应发送完毕**后才拆除，而流式响应的
"发送完毕"就是流结束。所以：

> **单个 worker 能同时服务的流式会话数 = `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`。**

默认 `5 + 10 = 15`。第 16 个并发流会先阻塞 `DB_POOL_TIMEOUT_SECONDS`（30s），
然后 500。要扩容，**必须同时调这三个**（还有一个是 PostgreSQL 的
`max_connections`，默认 100，与业务服务、Grafana、psql 共用）：

```bash
# deployment/.env
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
DB_POOL_TIMEOUT_SECONDS=30
```

`DB_POOL_RECYCLE_SECONDS=1800`：在空闲防火墙/NAT 静默掐断连接之前主动换掉。
`pool_pre_ping` 也能发现死连接，但每次取连接都要多一个往返；定期回收让
"探测"变成例外而非常态。

#### `DB_IDLE_TX_TIMEOUT_MS` 是被一次实测事故加进来的

上线后查 `pg_stat_activity` 时看到网关侧有两条连接 **`idle in transaction`
持续 9 分钟以上**（`backend_start` 正好是探针跑的时间，最后一条语句是
`SELECT workflow_runs...`）。当时**没有持有任何锁**，也自行消失了，所以不是
正在发生的事故——但它说明了失败模式：

一个没人提交的事务会**一直持有一个快照**，autovacuum 就**无法回收比它更新的
死元组**。表持续膨胀，而应用侧一切正常，没有任何指标会变红。

所以给每条连接加了服务端参数：

```python
connect_args={'options': '-c idle_in_transaction_session_timeout=300000'}
```

把无界的泄漏变成一个有界、且会记进日志的错误。**这是护栏，不是修复**——
真正的原因（流式端点持有请求级会话）还在，见上面那条容量等式。

```bash
# 随手检查有没有卡住的事务
docker compose --env-file deployment/.env -f deployment/docker-compose.yml \
  exec -T postgres psql -U support -d support -c \
  "SELECT pid, state, now()-state_change AS idle_for, left(query,80)
     FROM pg_stat_activity WHERE state='idle in transaction';"
```

### 备份与恢复

```bash
bash scripts/backup.sh                        # 备份（PostgreSQL + Redis）
bash scripts/verify_latest_backup.sh          # 把最新备份还原到临时库比对行数
bash scripts/verify_latest_backup.sh 2        # 校验第 2 新的备份
bash scripts/restore_backup.sh --yes backups/<时间戳>   # 破坏性：覆盖线上库
```

**这一版替换掉了一个 Dify 时代的残留脚本，它有三个缺陷，合起来比没有备份更糟：**

1. 要求设 `POSTGRES_CONTAINER` 环境变量；不设就**跳过 `pg_dump`**——也就是唯一
   重要的那一步——然后照样打印 "Backup written"。备份报告成功而实际什么都没导，
   这个失效模式要到**恢复的时候**才会发现。
2. 它复制 `dify/workflows`，而这条栈早就不跑 Dify 了。
3. 它假设自己从项目根运行，从别处跑会把产物写到调用者旁边的野目录。

现在这个脚本的规则是：**绝不为自己没做的事报告成功**。每一步都校验：

- `pg_dump -Fc` 在**容器内**写文件，再用 `docker compose cp` 拷出来。不通过 stdout
  重定向传二进制归档——Windows 的 CRLF 转换和连接中断都会把它变成只在恢复时
  才暴露的坏文件。
- 用 `pg_restore -l` 读归档的 TOC，**逐张核对 14 张必须存在的表**。字节数说明不了
  任何事，TOC 可以。
- Redis 用 `BGSAVE` + **等 `rdb_bgsave_in_progress` 归零**再拷贝（立刻拷会拿到上一次
  的快照），并把 `LASTSAVE` 记进 MANIFEST。
- 写 `MANIFEST.txt`：sha256、字节数、表数量、每张表在备份时刻的行数、git rev。

`--verify` 模式是**唯一能证明备份可用**的方式：它把归档还原到一个临时库、
`ANALYZE` 之后逐表比对行数（`n_live_tup` 在刚还原时读 0，不 ANALYZE 会把
每一张表都报成"还原了 0 行"），然后删掉临时库。**它从不碰线上的 `support` 库。**

> 一个从没被恢复过的备份是假设，不是备份。截断的归档、被迁移删掉的表、
> 版本不兼容的 `pg_dump`——这些失效模式只有真正加载一次才看得见。

### 定时

```bash
sudo cp deployment/systemd/support-backup.*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now support-backup.timer support-backup-verify.timer
systemctl list-timers 'support-backup*'
```

| 单元 | 时间 | 做什么 |
|---|---|---|
| `support-backup.timer` | 每天 03:20 | 备份 PostgreSQL + Redis，保留最近 14 次 |
| `support-backup-verify.timer` | 每周日 04:30 | 把当天那份备份还原到临时库比对行数 |

两个 timer 都是 `Persistent=true`：机器在 03:20 是关机的，开机后会补跑一次，
而不是**整晚静默跳过**。

### 需要备份的卷

| 卷 | 内容 | 丢了会怎样 |
|---|---|---|
| `support_postgres_data` | 全部业务数据 | **不可恢复**——所以 `backup.sh` 会失败退出 |
| `support_redis_data` | 幂等键、限流计数 | 可恢复（本来就会过期），但备份里带着 |
| `support_caddy_data` | 自签名证书 | 自动重签，不用备 |

---

## 9. 评测与回归门禁

**pytest 能证明代码还做它当初被写来做的事，证明不了答案还是不是一样好。**
答案质量取决于知识库、意图阈值、模型和提示词——这些没有一个被单元测试钉住。

`evaluation/` 就是补这一层的：一组固定用例，每条带期望值，跑在真实链路上。

```bash
# 对真实部署跑（JWT 登录）
python3 evaluation/run_eval.py https://124.222.190.3/support

# 对本地 dev 模式跑
EVAL_DEV_USER=user001 python3 evaluation/run_eval.py http://localhost:8080

# 把当前结果记成基线（之后任何一条从通过变失败都会被报成「回归」）
python3 evaluation/run_eval.py <base> --update-baseline
```

### 断言的是**轨迹**，不只是答案文本

老版本只检查 `must_contain` / `must_not_contain`。**一段措辞完美的回答，
可能是走错路走出来的**：调错了工具、该转人工没转、或者悄悄降级成了模板回答——
这些在文本里全都看不见。所以每条用例可以断言：

| 字段 | 含义 |
|---|---|
| `expect.intent` | 意图判定（`refund` / `order_query` / `tech_support` / `human_handoff` / `faq` / `other`） |
| `expect.tool` | 应调用的工具；`null` = 不该调用任何工具 |
| `expect.tool_status` | `executed` / `failed` / `waiting_approval` |
| `expect.handoff_level` / `handoff_min` | 转人工级别；`handoff_min` 用于「必须升级但级别是实现细节」 |
| `expect.degraded` | 是否降级成模板回答 |
| `turns` | 多轮（挫败感转人工需要连续三轮，单轮测不到） |

**越权用例要断言 `tool_status: failed`**，不能只断言答案里没有敏感信息——
只断言文本的话，工具因为别的原因失败也会让用例通过，那是**因为错误的原因通过**。

### 两层跑同一份数据集

| 层 | 跑在哪 | 能抓什么 |
|---|---|---|
| `tests/test_eval_dataset.py` | CI，进程内，模型关闭 | 路由、越权、转人工、注入——每次提交都跑 |
| `evaluation/run_eval.py` | 部署后，真实链路 + 真实模型 | 答案质量漂移，以及相对基线的**回归** |

进程内那层有一类用例会**显式跳过**并写明原因：需要订单服务在链路上
（进程内没有它，工具调用一律 `failed`，断言会因错误的原因通过），
或需要模型做意图判定（关掉模型后主干对无法分类的问题一律升级转人工，
这与线上不同但同样正确）。数据集里有 `live_only` 字段标明依赖，
`tests/test_eval_dataset.py` 里有一条测试**守着「能在 CI 跑」的用例数下限**——
否则「逐条都合理」的跳过会把覆盖率悄悄降到零。

### 为什么评测不覆盖审批信任链

审批相关的用例会在演示订单系统里**动真状态**，第二次跑就会发现订单已经退过款了。
所以那部分由 `scripts/acceptance_probe.py` 负责，它跑之前会先重置演示数据。
分开的另一个好处是：这个门禁可以**反复跑，不消耗任何状态**。

## 10. 排障速查

| 症状 | 先看哪里 |
|---|---|
| 公网 502 / 连不上 | `docker logs novel-nginx`；`docker exec novel-nginx nginx -t` |
| 访问 `/support/` 出现**另一个项目**的页面 | 边缘 nginx 的 `location /support/` 没生效或没 reload |
| 页面能开但接口 404 | 前端用相对路径 `api/...`，必须落在 `/support/` 下（末尾斜杠不能少） |
| 回答到一半卡住 | 边缘 nginx 的 `proxy_buffering` 被打开了，SSE 需要 `off` |
| 登录 422 | 请求体缺 `tenant_id` —— 多租户登录必须带租户 |
| 工具全部 `denied` | 工具不在 `TOOL_REGISTRY` 白名单里（这是设计行为） |
| 工具返回 403 / 422 | 身份映射或参数契约不对：看 `gateway.app.models.User.business_customer_id` |
| 审批通过但业务没动 | `REVIEWER_API_KEY` 没配 —— 网关无法把结论回写订单系统 |
| `UndefinedColumn` | 模型加了列但没写迁移。`bash scripts/migrate.sh check` 会提前告诉你 |
| `alembic upgrade` 报 `relation already exists` | 库是 `create_all` 建的、还没 stamp。跑 `bash scripts/migrate.sh`（它会自动 stamp） |
| `alembic upgrade` 打印「完成」但 `alembic current` 没变 | **命令没挂工作副本**。容器读的是镜像里的旧 `migrations/`，`head` 解析成了旧值。见 §8「改 schema 的正确姿势」 |
| `alembic check` 说「无漂移」但你明明改了模型 | 同上：它比的是**镜像里那份** `models.py` |
| `make revision` 返回 0 但 `migrations/versions/` 里没有新文件 | 同上：文件写进了 `--rm` 容器的可写层。走 `scripts/new_migration.sh`（它跑完会断言文件落盘） |
| 生成的迁移文件名是 `xxx.py.py` | `alembic.ini` 的 `file_template` 不能带 `.py`，alembic 会自己加扩展名 |
| `alembic revision` 报 `Target database is not up to date` | autogenerate 是「模型 vs **当前**库」求差分。先 `bash scripts/migrate.sh upgrade` |
| 新增的环境变量在容器里看不到 | `docker-compose.yml` 的 `environment` 是**白名单**，没列出的永远进不了容器（`.env` 里设了也不报错） |
| UI 冒烟 `exit 2` / `Cannot find module 'jsdom'` | 找不到 jsdom。`bash scripts/ui_smoke.sh` 会把三个解法打出来；本机是 `JSDOM_HOME=... bash scripts/ui_smoke.sh <url>` |
| 发布门禁 UI 层失败但手跑同一条命令是绿的 | 说明这一层依赖了外部环境变量。现在统一走 `scripts/ui_smoke.sh`，不该再有这个差别 |
| Grafana 面板「Datasource not found」 | 数据源 uid 不是 `prometheus`，或卷是新建的而 provisioning 没重跑 |
| Grafana 看板还是旧版 | provisioning **只在启动时读**：`docker compose restart grafana` |
| 看板某个面板永远 "No data" | 它引用的指标没人上报。跑 `python observability/grafana/build_dashboard.py`，生成器会直接报出是哪条 |
| 告警响了但没人收到 | `ALERT_WEBHOOK_URL` 没配。看 `docker logs support-alertmanager-1`；同时看板「正在告警」面板仍是准的 |
| 备份脚本说成功但没数据 | 不应该再发生——现在的脚本会因为归档里缺表而**非零退出**。用 `bash scripts/verify_latest_backup.sh` 复核 |
| 新加的索引 `EXPLAIN` 里看不到 | 先看**窗口在数据里占多大比例**。窗口≈全历史时 planner 不选它是**对的**，不是索引建错了 |
| 退款被拒但看起来该受理 | 查 `aftersale_rule` 里有没有这个 `sku.category` 的行。**没有规则会返回 `eligible=null`（放行），不是拒绝** —— 所以真的被拒就是查到了规则且规则说不行。日志里 grep `aftersale_rule_blocked`，它带 `rule_id` 与 `reason` |
| 退货窗口算错 / 全部超期 | 看 `orders.signed_at` 是不是空。为空时答案是 `signing_time_unknown` → **放行**。如果它被写成了假时间戳（例如 epoch），窗口就会从假起点算，系统会开始拒绝本该受理的退货 |
| 规则服务挂了，退款会不会被误拒 | 不会。`check_aftersale_eligibility` 抛 `ToolDenied` 时日志记 `aftersale_rule_check_skipped` 并**放行**。读超时不是拒绝 |
| 商品库存/价格答错 | 只有 `sku` 表是权威。`gateway/app/seed.py` 里的知识库文档是散文，不参与校验 |

---

## 11. 已知限制（下一步）

已经做完的（原本列在这里）：

- ✅ **Alembic 迁移**（第 8 节）——`create_all()` 只建不改的问题已解决，
  并有 `alembic check` 与 `tests/test_migrations.py` 两道漂移门禁。
- ✅ **告警可送达**（第 6 节）——10 条规则接上了 Alertmanager，
  含抑制规则与「正在告警」面板。
- ✅ **备份可用**（第 8 节）——真备份 + 还原校验 + 定时。
- ✅ **CI 门禁是真的**——`scripts/release_gate.py` 不再在 `LIVE_ACCEPTANCE=0` 时
  静默跳过所有真实检查；它会校验配置、**重新生成看板并检查是否漂移**、
  校验迁移链，然后跑 pytest。真实链路由 `LIVE_ACCEPTANCE=1` 触发，
  跳过时会**明确打印**跳过了什么。
- ✅ **badcase 回归门禁接 CI**（第 9 节）——数据集扩到 18 条、加了轨迹断言与基线对比；
  `tests/test_eval_dataset.py` 让它在**每次提交**都在进程内跑一遍，
  真实链路那一遍由 `LIVE_ACCEPTANCE=1` 触发。
- ✅ **分析接口不再随流量线性吃内存**——`/api/admin/overview` 的会话计数下推到
  SQL、消息只取窄列（不再读 `content` / `sources`），并补了
  `(tenant_id, created_at)` 索引。`tests/test_admin_overview.py` 里两条测试
  断言的是**发出的 SQL**，防止有人改回整实体查询。

还没做的：

1. **两个服务共用一个数据库。** 刻意的简化，代价是表名必须全局唯一——
   `tests/test_schema_namespaces.py` 在守这条线。真要隔离，拆库或拆 schema。
2. **Plan Verification 只有白名单。** 目标是白名单 + **无环 DAG + 类型契约**校验。
3. **pgvector 只用了半条。** 表建好了、`vector` 扩展装了，但 embedding 仍存 JSON、
   `EMBEDDING_ENABLED=0`，检索实际只跑 BM25 通道。
4. **结果 + 轨迹双评未做。** 评测现在断言的是「意图/工具/转人工」这层轨迹，
   还没有对检索命中、参数正确性这类**中间步骤**逐项打分。
5. **流式并发被连接池封顶**（第 8 节）。`/api/chat/stream` 持有请求级会话
   直到流结束，所以并发流上限 = `DB_POOL_SIZE + DB_MAX_OVERFLOW`。
   要真正解开，得让流式端点不再依赖请求级会话（只在需要写库时开短会话）。
6. **自签名证书。** 浏览器首次访问会有警告（证书 SAN 只含 IP）。
   绑域名后把边缘 nginx 换成 Let's Encrypt 即可，本项目 Caddyfile 无需改动。
7. **单机单副本。** 网关无状态可水平扩，但 `AUTO_SEED` 与迁移需要显式编排，
   现在没做。

