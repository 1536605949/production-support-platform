# Changelog

## 2.6.0 — Verified Planning：在执行之前证明计划合法

2.5 解决的是「**能查到**哪些结构化事实」；2.6 解决的是「**在执行之前**能不能证明
这次要做的事是合法的」。

之前的治理层只能回答「这个工具允许被调用吗」。它回答不了「这一步的顺序对不对」、
「上一步真的产出这一步要的东西了吗」、「这个 `proposal_id` 是这一步自己造出来的，
还是从别处捡来的」。所以 `execute_refund` 单独出现是一个**合法**的 plan——
白名单会痛快放行，而它执行的是一份本轮从未创建过的申请。

### 1. Plan 是一个图，不是一串工具名

新增 `gateway/app/core/plan.py`：`Plan` / `PlanStep` / `InputRef` / `Requirement`。

**依赖边是推导出来的，不是声明的。** `PlanStep.dependencies` 由 `inputs` 和
`requires` 现算，不读任何 `depends_on` 字段。这不是洁癖：一旦允许声明，
「`depends_on` 写着 s1，参数却读了 s2」就成为一个**可表达**的状态，
而它无论如何都不该存在。推导让它无法被表达。

校验器跑 8 道闸，顺序是刻意的——结构 → 图 → 类型 → 时序：

| 闸 | 拒绝理由 |
|---|---|
| 1 | `duplicate_step_id` |
| 2 | `unknown_tool` / `unknown_argument` / `missing_argument` |
| 3 | `invalid_arguments`（字面量不满足契约） |
| 4 | `self_dependency` / `missing_dependency` |
| 5 | `cycle_detected`（Kahn 拓扑排序） |
| 6 | `invalid_reference`（前向引用：执行顺序**就是**声明顺序） |
| 7 | `type_mismatch` / `missing_output` |
| 8 | `invalid_sequence`（`requires_predecessor` 未满足） |

第 5 道排在第 6 道之前是有原因的：`A → B → A` 既是一个环，也是一次前向引用，
而报 `invalid_reference` 会把「你写了个环」这个真正的问题说成「顺序写错了」。
先跑拓扑排序，环就报环。

**空 plan 是合法的。** 「这一轮不需要调工具」是一个正常结论，不是校验失败。

### 2. 类型是名义的，不是结构的

`OrderId` / `ProposalId` / `SkuId` / `UserId` / `TicketId` / `RuleId` 是 `str` 的子类，
运行时零成本。**结构类型会接受把 `proposal_id` 传给 `order_id`**——两者都是
`str`，长得一样，Pydantic 也照收。名义类型不会。

`SEMANTIC_IDS` 把字段名映射到语义类型，因此语义挂在**名字**上，
不需要为每个工具的第二份参数类型表再维护一遍。输入和输出用同一张表。

规则是「窄化可以，加宽不行」：`OrderId → str` 合法，`str → OrderId` 不合法。
`bool` 是 `int` 的子类，但**显式不允许赋给 `int`**——`amount=True` 是 bug，
不是 `1`。

### 3. 校验在第一个工具之前跑，失败必须零副作用

执行链变成 `plan_tools → verify_plan → governance → execute`。校验器在**第一个
工具之前**跑完，所以一个非法 plan 产生的是**零次业务调用**，而不是「执行到一半
才发现不对」。

fail-closed 的行为是四条，缺一条都不算：

- 不调用业务服务（`tests/test_plan_verification.py` 断言 stub 调用次数 **= 0**）
- 不产生资金操作
- 写 trace（`plan_validation` 事件 + 完整 checks 台账）
- 用户拿到安全回复，不是 500、也不是原始异常

### 4. `execute_refund` 显式吃 `proposal_id`

网关侧 `ExecuteRefundArgs` 增加 `proposal_id`，值来自 plan 的 `InputRef`
（指向 `create_refund_proposal` 的 `proposal_id` 输出），**不是调用方给的**。

业务服务侧同步：`RefundExecute` 现在**要求** `proposal_id`，
`execute_approved_refund(db, user_id, order_id, proposal_id)` 按四元组精确取那一行。
之前它按 `(user_id, order_id, status='approved')` 取最新一条——猜对了和猜错了
在审计里长得一样。现在「人批准的那条 proposal」和「这次调用消费的那条」
可证明是同一行。

一次性消费的守卫没变，仍是 `proposal_id + status='approved'` 的条件 UPDATE。

### 5. 退款链路：三步 plan，审批是执行屏障

```
check_aftersale_eligibility ──requires(eligible is not False)──▶ create_refund_proposal
                                                                        │
                                                     inputs.proposal_id │
                                                                        ▼
                                                                 execute_refund  ← 停在这里等人工
```

`create_refund_proposal.requires_predecessor = ('check_aftersale_eligibility',)`
取代了原先的 `_eligibility_gate` 前置闸。「资格检查跑过了」从一句写在
`engine.py` 里的过程式断言，变成 plan 上**可被校验器看见**的属性。

`execute_refund.requires_predecessor = ('create_refund_proposal',)`。
单个 `execute_refund` 组成的 plan 现在无法通过校验——那正是**绝不该可执行**的 plan。

三值纪律沿用：`requires` 的字段**缺席**不算违反。规则服务挂了不是拒绝，
因为读超时就拒掉一笔本该受理的退货，是「看起来像严格」的那种 bug。

### 6. 版本号：四处不一致 → 一处

2.5 时版本号在四个地方各说各话（`VERSION` 文件 `1.7.1-rc1`、
`settings.version` `2.0.0`、两个 OpenAPI 文档 `1.7.1`、CHANGELOG `2.5.0`）。
一个错成四种样子的版本号不是版本号，是四个猜测。

现在六处统一到 `2.6.0`，并由 `tests/test_version_consistency.py` 守着——
任何一处漂掉，门禁红。业务服务那两处独立字面量也收成一个模块常量
（`SERVICE_VERSION`），且刻意**不是** `Settings` 字段：一个能被环境变量覆盖的
版本号，回答不了「这是哪个构建」。

### 7. 修掉：plan 写进了响应模型之外，于是被静默丢掉

决策检查器（运营台）和评测门禁都需要 plan 才能工作，而 plan 在 v2.6 的
**两条响应路径上都到不了客户端**。

原因不是漏了赋值，是漏了**声明**：`TurnResult.plan` 有了，
`_to_response()` 也老老实实写着 `plan=result.plan`，但 `ChatResponse` 上
没有 `plan` 字段。Pydantic 对未声明的关键字参数是**忽略**而不是报错——
于是阻塞接口和 SSE 的 `final` 事件都把 plan 丢了，没有异常、没有日志、
测试全绿。运营台的 `renderPlan()` 渲染不出任何东西，评测门禁里
「计划是什么」这件事根本无从断言。

抓到它的不是单元测试，是给 badcase 数据集加上 `plan_status` 之后跑
`test_eval_dataset.py` 得到的 11 条失败——**断言本身把 bug 找出来了**。

修法：`ChatResponse` 补上 `plan` 字段；并加 `tests/test_response_contract.py`
把这一整类问题封住——它按**字段集合**断言，要求 `TurnResult` 上每个公开字段
要么出现在响应模型上、要么出现在响应 `metadata` 里、要么被显式列进
「刻意不外露」集合（目前只有 `error`，因为内部错误文本属于日志不属于回复）。
所以下一个新增字段不会再悄悄消失。这个守卫做过负向验证：给 `TurnResult`
加一个字段，守卫立刻点名它。

### 8. 评测门禁开始断言 plan 的形状

`evaluation/run_eval.py` 新增四个期望键：`plan_status` / `plan_steps` /
`plan_dependencies` / `plan_rejection_reason`。已有的 18 条 badcase 里，
17 条现在同时钉住计划，其中「钉了工具名的用例必须同时钉住计划步骤」是一条
守卫不变量——**工具对了但图错了是看不见的**，而这正是退款链路最贵的错误：
step 3 不再消费 step 2 的 `proposal_id` 时，`execute_refund` 照样被调用、
回答照样正确，只是退回「按某个已被批准的 proposal 执行」。

两点刻意的取舍：

- `plan_status: null` 是合法值，不是缺失值——`handoff_level >= 2` 时主干
  根本不规划。没有数据集用例钉它（挫败感计数落在哪一级不够稳定），
  由 `tests/test_eval_plan_assertions.py` 用合成响应覆盖。
- **没有任何用例声称能触发「计划被拒」**，因为确定性 planner 做不到这件事；
  编一条会通过的假用例，比留下这个空缺更糟。拒绝路径由
  `tests/test_plan_verification.py` 用手工构造的非法 plan 覆盖。
  有一条测试专门守着「不许往数据集里塞这种用例」。

### 明确不做

这一版**不做**：pgvector（embedding 仍存 JSON）、多副本与主从、数据库分片、
Kubernetes、LLM Judge。计划路线是 v2.7 Hybrid Retrieval、v2.8 Evaluation 2.0、
v3.0 Concurrency & Scale。

**Plan 仍然由确定性代码生成，这一版没有把决策权交给模型。**
如果将来要试 LLM Planner，它只能产出**候选** plan，然后走同一个校验器——
校验器是权限边界，不是建议。

## 2.5.0 — 商品目录、结构化售后规则、C 端页面

这一版补的是两件**确实缺**的事，以及一个顺手抓出来的既有 bug。

### 1. 结构化业务事实：`sku` + `aftersale_rule`

在这之前，「这个商品还有货吗」和「我这一单能不能退」只能靠知识库召回一段散文
再让模型组织语言。散文能回答「规则是怎样的」，回答不了「**这一单**符不符合」。

- `sku`：名称、规格、类目、库存、价格。库存和价格**只此一处**是权威。
- `aftersale_rule`：按商品类目落表的售后政策，`window_days` + `eligible` + `reason`。
- `orders` 补 `sku_id` / `signed_at` 两列——退货窗口是从签收时刻算的，没有它算不了。

对应两个只读工具 `get_sku` / `check_aftersale_eligibility`，走的是既有的
`TOOL_REGISTRY` 五道闸，因此天然进审计、幂等与可观测。

### 2. 判定是三值的，`null` 不是拒绝

`eligible` 返回 `true` / `false` / **`null`**：

| 值 | 含义 | 处理 |
|---|---|---|
| `true` | 找到规则，且满足 | 放行 |
| `false` | 找到规则，且违反 | 拒绝 |
| `null` | 无法判定（订单无 SKU / 类目无规则 / 无签收时间） | **放行** |

把 `null` 折叠成 `false` 会让系统开始拒绝它本该受理的退货，而且看起来像「严格」
而不是 bug。所以闸门**只在有确凿不通过证据时才拦**；规则服务抛 `ToolDenied` 时
同理——**读超时不是拒绝**。

拒绝本身也**不交给模型措辞**：`status='blocked'` 直接走确定性回答，理由引用规则行原文，
否则用户就有了一个回合把它劝成别的答案。

### 3. C 端页面

`demo_client/customer.html`：只有对话，业务事实渲染成商品卡 / 订单卡，
**意图、置信度、召回来源、工具名、降级标记、耗时、trace_id 一律不显示**。
工作台（`index.html`）保持原样，两个前端调同一组 `/api/chat/stream`。

### 4. 修掉：确定性回答把订单金额印成「None」

`get_order` 返回的是 `amount`（元），`deterministic_answer` 读的是 `amount_cents`。
`int(None)` 抛的 `TypeError` 被格式化函数自己的 `except` 吞掉了，于是回答里
稳定输出「金额 **None**」。

它只在**模型不可用或刚失败**时才走到——也就是最不能出错的那条降级路径。
已加两条回归测试（正常值 + 缺失值应显示 `—` 而不是 `None`）。

### 5. 迁移 `0003_products_and_rules`

两张新表 + `orders` 上两列。这是「加列必须走迁移」的实例：新表交给
`create_all()` 也会建出来（所以只看表存不存在看不出问题），但**已存在表上的新列不会**——
`create_all()` 只建不改，既不生效也不报错，直到第一次查这一列才炸 `UndefinedColumn`。

两列都 nullable 且无 server default：任何默认值都是撒谎，而 `signed_at` 一旦被写成
假时间戳，退货窗口就会从假起点计算，系统会开始拒绝本该受理的退货。

### 6. 验收补一层：C 端页面的 DOM 冒烟

在这之前，C 端页面只被验到「HTTP 200、标题在」。**这不是证据**——单文件页面里
一个未定义的全局就能让整段脚本静默死掉，而页面照样返回 200。本项目历史上
UI 那一层抓到过单元测试抓不到的缺陷，所以这里补上：

新增 `scripts/ui_smoke_customer.mjs`（60 项），在 jsdom 里跑**真实页面**：
登录、SSE 流式累积、商品卡 / 订单卡、快捷按钮、流中断提示、401 退回登录页，
以及最关键的**负向断言**——意图 / 置信度 / 召回来源 / `trace_id` / `degraded` /
工具名一律不出现在渲染后的 DOM 里。

两个细节值得记：

- **负向断言必须对着渲染结果，不能对着源码 grep。** 页面是单文件，`<script>`
  就在 `<body>` 里，`document.body.textContent` 会把脚本源码（连注释）一起返回。
  初版因此报了 4 个假泄漏（`intent` / `confidence` / `get_sku` / `get_order`）——
  页面自己的说明注释里就写着这几个词。现在用 `visibleText()` 跳过
  `SCRIPT`/`STYLE`，并且加了一条「守卫的守卫」：断言提取器确实没在读源码
  （`localStorage` 只出现在脚本里，不该出现在可见文本里）。
- **包装脚本的页面脚本改成可选参数**，而不是再写一个包装脚本：jsdom 的定位
  逻辑只能有一份，两份就是其中一份悄悄失效的开始。

### 7. 产品文档断言化

`docs/PRODUCT.md`（从设计到实际，含如实标注的缺口）+ `scripts/verify_product_doc.py`
（102 项断言），并接进发布门禁。文档里每一个计数都对着源码核对：数据表 19 张、
注册工具 7 个、主干节点 8 个、告警规则 10 条、指标族 22 个、两个前端页面的字节数……

理由是**手工维护的计数一定会漂**：这个仓库已经漂过一次（README 写 234，实际 235），
`docs/architecture.md` 也长期停在「九项能力 / 105 个 pytest / 18 个面板」。
一份对自己数字都说错的文档比没有更糟——读者分不清哪部分可信。

顺带堵了一个洞：原来只断言「文档里出现某个数字」，那只能证明数字**存在**，
证明不了它**对**。现在把文档写的「通过 + 跳过」对着 `pytest --collect-only`
的实际收集数核对（244 = 238 + 6），加一个测试就会红。

### 8. 修掉：日志把字符数标成「字节」

两个 UI 冒烟脚本打印页面大小时用的是 `html.length`，那是 UTF-16 码元数，不是字节数。
页面几乎全中文，两者差约每字 2 字节——C 端页是 **15247 字节 / 14829 字符**。
日志里写「14829 字节」会让人以为部署的页面比磁盘上的小，看着像没同步（实测时
差点按这个去查「镜像里的页面是不是旧的」）。现在两个数都打，字节数正好对上
文档里断言的那个值。`tests/test_release_gate.py` 加守卫：任何写着「字节」的行
必须同时算 `byteLength`。

### 验证

`pytest` **238 passed / 6 skipped**；新增 `tests/test_products_and_rules.py`（24 项）
覆盖库存查表、类目拒绝、超期拒绝、**两种 `null` 不拒绝**、以及
「新增商品关键词没有抢走退款/订单意图」。

`tests/test_governance.py` 里 5 条断言精确调用序列的用例同步更新：新增的只读预检
**显式列出**而不是从桩里过滤掉——过滤会让这些守卫悄悄失去意义，
多出一次意外调用就不再失败了。

`tests/test_release_gate.py` 补三条守卫：C 端那层不许被摘掉；
C 端冒烟不许退回 `document.body.textContent`（那会把内联脚本源码当成渲染结果，
负向断言就全空了）；日志不许把字符数标成「字节」。

---

## 2.4.0 — 迁移工具链一直在读镜像而不是工作副本

这一条不是在补功能，是在修一个**已经存在、而且已经生效过**的缺陷：
`migrate.sh` 和 `make revision` 都把 alembic 跑在 `gateway` 容器里，而
`gateway/Dockerfile` 是 **COPY** 源码进去、`gateway` 服务**没有 bind mount**。
镜像是源码的一份快照，于是：

| 命令 | 实际行为 |
|---|---|
| `make revision` | 迁移文件写进 `--rm` 容器的可写层，容器退出即删除。命令返回 0，文件没了 |
| `migrate.sh upgrade` | `head` 解析成镜像知道的那个 head，**一条迁移都不应用**，却打印「完成」 |
| `migrate.sh check` | 拿镜像里的 `models.py` 比数据库，**恰好在有漂移时报「无漂移」** |

三条都不是推测。实测过程：在部署上跑 `bash scripts/migrate.sh upgrade`，
输出「完成」，而 `alembic_version` 停在 `0001_baseline`，
`0002_analytics_indexes` 就躺在磁盘上没被应用；紧接着 `migrate.sh check`
报「没有未迁移的模型改动」；再在 `--rm` 容器里 `touch` 一个文件，
宿主机上什么也没有。`make revision` 更彻底——它连一个文件都不会留下。

修法是给两处 alembic 调用加 `-v "$ROOT:/app"`：让迁移描述的对象，等于下一次
`up --build` 会烤进镜像的那份源码。再加 `--user "$(id -u):$(id -g)"`，
免得在宿主机仓库里留下 root 属主的 `__pycache__`。

`make revision` 改为走 `scripts/new_migration.sh`，它负责三件事：

- **自动编号** `000N_slug`，拒绝随机 hex（`migrate.sh` 要按名字 stamp 基线）。
- **跑完断言迁移文件真的出现在宿主机上**——否则「配置应该能work」和「确实 work 了」
  没有区别，而这次事故就是前者。
- 把 alembic 的 `Target database is not up to date` 翻译成可执行的提示：
  autogenerate 是「模型 vs **当前**数据库」求差分，带着待应用的迁移跑会把那条
  迁移再生成一遍。

另外两处：

- `alembic.ini` 的 `file_template` 从 `%(rev)s_%(slug)s` 改为 `%(rev)s`。
  带描述性 rev-id 时原模板会自我重复（`0002_analytics_indexes_analytics_indexes.py`）。
  **注意不能写成 `%(rev)s.py`**：alembic 会自己加扩展名，结果是
  `0002_foo.py.py`——那不是 Python 模块，下一条 `upgrade` 找不到它。这个坑是
  拿真部署跑出来的。
- `tests/test_migration_tooling.py` 新增 12 条守卫。其中两条已用
  「把修复回退掉」验证过确实会失败。写第一版时踩到一件事：
  断言 `-v "$ROOT:/app"` 出现在 `migrate.sh` 里——**把挂载删掉后它依然通过**，
  因为解释这个挂载的注释里含有同样的字符串。现在断言的是**去掉注释后的
  函数体**。能被注释满足的守卫什么都没守。

### 发布门禁的 UI 层依赖外部环境变量

把这一轮的修复跑一遍门禁，UI 层报了 `命令失败（exit 2）`——**而几分钟前同一条
命令手跑是 17/17 全绿**。差别在于：我手跑时 shell 里 `export` 过 `NODE_PATH`，
而 `release_gate.py` 里那行是

```python
run(['node', 'scripts/ui_smoke_test.mjs', f'{PUBLIC_URL}/'],
    env={'NODE_TLS_REJECT_UNAUTHORIZED': '0'})
```

——**只传了 TLS 那一个变量**。`jsdom` 不在仓库里（没有 `package.json`），
`ui_smoke_test.mjs` 靠 CommonJS 解析以便认 `NODE_PATH`，于是这一层的结论
取决于**你 shell 里恰好 export 了什么**。干净 shell 里它永远失败，而且报的是一句
`exit 2`，读起来像 UI 回归，不像缺 npm 包。

> 一个结论取决于外部环境变量的门禁，不是门禁。

修法：新增 `scripts/ui_smoke.sh`，**只有它负责定位 jsdom**，`make ui` 和
`release_gate.py` 都走它。搜索顺序：已可解析（项目内 `node_modules` 或
`NODE_PATH`）→ `$JSDOM_HOME/node_modules`。都找不到时**把三个解法打出来**
（`npm i jsdom` / `JSDOM_HOME=` / `NODE_PATH=`）再退出，而不是丢一个 exit 2。

同时补了 `shutil.which('bash')` 检查——否则 `subprocess.run(['bash', ...])`
在没有 bash 的机器上抛 `FileNotFoundError`，门禁会以 traceback 崩掉，
又是一次"因为无关的原因失败"。

修完再跑，门禁又失败了一次，这次是 `exit 1`：

```
PROGRAM BLOCKED BY SECURITY POLICY ... wsl.exe
```

原因：Windows 的 `CreateProcess` 搜索顺序是
**当前目录 → System32 → Windows → PATH**，而 `shutil.which` 只按 PATH 顺序。
`C:\Windows\System32\bash.exe`（WSL 存根）存在，所以裸名 `bash` 命中它、
拉起 `wsl.exe`——**而同一条命令手打是绿的**。改成传 `shutil.which` 解析出的
绝对路径。规则：凡起子进程，不用裸名。

`tests/test_release_gate.py` 新增 7 条守卫。做了两次「把修复回退掉」的验证，
每次都有 3 条变红，合计覆盖 4 条不同的守卫（两次回退命中的子集不同）：

| 回退内容 | 结果 |
|---|---|
| 门禁改回直调 `node ui_smoke_test.mjs` + Makefile 改回直调 node | 3 failed / 3 passed |
| 门禁改回裸名 `'bash'` + Makefile 改回直调 node | 3 failed / 4 passed |

### `0002_analytics_indexes` 的效果（实测，不是估计）

索引建在 `messages(tenant_id, created_at)` / `conversations(tenant_id, created_at)`。
在部署上的 365 行数据里它**不会被用到**，这是对的：该租户的全部历史都在 7 天
窗口内，复合索引省不下任何一行，planner 选单列 `ix_messages_tenant_id` 更便宜。

所以效果在一个**临时表**里量（同形状、同谓词、30 万行跨 208 天，窗口占 3.4%，
不碰真实表）：

| | 迁移前 `ix(tenant_id)` | 迁移后 `ix(tenant_id, created_at)` |
|---|---|---|
| `created_at` 的角色 | 残余过滤 | **Index Cond** |
| Rows Removed by Filter | 289 920 | 0 |
| Buffers read | 2 206 | **127** |
| Execution Time | 40.4 ms | **2.26 ms** |

结论是「窗口查询变得可索引」，不是「窗口查询变快了」——后者在这个部署上量不出来。

## 2.3.0 — 评测门禁接 CI、分析接口不再随流量吃内存、连接池与空闲事务护栏

上一轮把运维底座补齐了（迁移 / 告警 / 备份）。这一轮处理三个「跑得起来但会
在真实负载下变坏」的地方，并且每一处都补上了**能失败的断言**。

### badcase 回归门禁真正接进 CI

`evaluation/run_eval.py` 有两个让它形同虚设的缺陷：

1. **它用 `X-Demo-User` 头鉴权**，那只有 `AUTH_MODE=dev` 才认。部署是
   `AUTH_MODE=jwt`，所以对着真实部署跑**每一条用例都 401 失败**——
   一个只能在"没人会部署的地方"跑通的回归门禁。现在真登录，
   dev 头只在显式设置 `EVAL_DEV_USER` 时才用。
2. **它只断言答案文本**（`must_contain` / `must_not_contain`）。一段措辞完美的
   回答可能是走错路走出来的：调错了工具、该转人工没转、或者悄悄降级成模板——
   这些在文本里全都看不见。现在每条用例可以断言**轨迹**：`intent` / `tool` /
  `tool_status` / `handoff_level` / `degraded`，并支持 `turns` 多轮
   （挫败感转人工需要连续三轮，单轮测不到）。

数据集从 7 条扩到 **18 条**，覆盖知识问答、订单查询、越权、提示词注入、
边界政策、编造数字、格式错误的订单号、挫败感升级。

新增 `tests/test_eval_dataset.py`：**同一份数据集在 CI 里对进程内应用再跑一遍**
（模型关闭），这样路由 / 越权 / 转人工 / 注入的回归每次提交都能抓到。
另外加了基线对比——`run_eval.py` 记录每条用例的通过情况，
之后任何一条**从通过变失败**都会被报成「回归」，而不是只看一个孤立的通过率。

几个刻意的设计：

- **越权用例断言 `tool_status: failed`**，不只断言答案里没有敏感信息。
  只断言文本的话，工具因为别的原因失败也会让用例通过——那是**因为错误的原因通过**。
- **有一类用例显式跳过并写明原因**：需要订单服务在链路上（进程内没有它，
  工具调用一律 `failed`，断言会因错误的原因通过），或需要模型做意图判定。
  数据集里用 `live_only` 标依赖，并且有一条测试**守着「能在 CI 跑」的用例数下限**——
  否则「逐条都合理」的跳过会把覆盖率悄悄降到零。
- **评测不覆盖审批信任链**：那些用例会在演示订单系统里动真状态，第二次跑就发现
  订单已退过款。那部分归 `acceptance_probe.py`（它跑前会重置演示数据）。
  分开的另一个好处是这个门禁可以反复跑、不消耗任何状态。
- `run_eval.py` 取 BASE 从「第一个 `http(s)://` 参数」而不是 `sys.argv[1]`：
  它现在会被 pytest 导入，而 `argv[1]` 在 pytest 下是 `-q`，等于把
  `-q` 当成主机名。

### `/api/admin/overview` 不再随流量线性吃内存

原实现把整个时间窗口的 `Conversation` 和 `Message` **整实体**查出来，
再在 Python 里聚合。两个问题：

- 整实体查询会把 `content`（完整回答文本）和 `sources`（每一条检索命中的
  段落与分数，全表最宽的列）一起读出来，**而页面上没有任何一个数字用到它们**。
- 会话总数和转人工会话数是纯计数，却是把行load进内存再 `len()`。

改法：会话维度完全下推到 SQL（一条 `count` + `case` 聚合，SQLite 与 PostgreSQL
都支持）；消息维度只取需要的窄列。

**并且补了索引**：两张表都只有以 `tenant_id` 打头的索引，
而查询是 `WHERE tenant_id = ? AND created_at >= ?`——第二个条件只能作为
残余过滤，等于要读该租户的**全部历史**。新增
`(tenant_id, created_at)` 复合索引（迁移 `0002_analytics_indexes`），窗口变成范围扫描。

新增两条测试断言的是**发出的 SQL**：一条断言不出现
`messages.content` / `messages.sources`，一条断言会话计数下推到了 `count(`。
行为测试抓不到这类回退——改回整实体查询后响应**完全一样**，只是内存占用变了。
已验证这两条测试在回退时确实会失败（并打印出违规 SQL）。

### 连接池与 `idle_in_transaction` 护栏

上线后查 `pg_stat_activity` 发现网关侧有两条连接 **`idle in transaction`
持续 9 分钟以上**，最后一条语句是 `SELECT workflow_runs...`。当时**没有持有
任何锁**，也自行消失了——所以不是正在发生的事故，但它指出了失败模式：

一个没人提交的事务会一直持有一个快照，**autovacuum 就无法回收比它更新的死元组**。
表持续膨胀，而应用侧一切正常，没有任何指标会变红。

- 给每条连接加了服务端参数
  `-c idle_in_transaction_session_timeout=300000`：把无界的泄漏变成有界、
  且会记进日志的错误。**这是护栏，不是修复**。
- 连接池从"继承来的默认值"改成显式配置：`DB_POOL_SIZE` / `DB_MAX_OVERFLOW` /
  `DB_POOL_RECYCLE_SECONDS` / `DB_POOL_TIMEOUT_SECONDS`，SQLite 走单独分支
  （文件型 SQLite 不是网络资源，池参数对它无意义，反而会把并发写变成
  "database is locked"）。
- 顺带记下一个**容量等式**：`/api/chat/stream` 的 `db` 是请求依赖，
  而 FastAPI 的 `yield` 依赖在**响应发送完毕**后才拆除——对流式响应来说就是
  整个流期间。所以**单个 worker 的并发流上限 = `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`**。
  已写进 `docs/OPERATIONS.md` 与 `.env.example`。

### 文档

`docs/OPERATIONS.md` 新增第 9 节「评测与回归门禁」，
并在第 8 节补了「连接池与流式并发」（含上面那次实测事故的完整记录）；
已知限制重排，`badcase 门禁` 与 `分析接口内存` 移入已完成，
新列出「流式并发被连接池封顶」这条真正的遗留问题。

## 2.2.0 — 生产化收口：迁移、告警投递、备份可验证、观测面板生成

本轮补齐四项「上线真实数据前必须先有」的基础设施，并重写观测看板。
全部在真实部署上实测通过。

### Alembic 数据库迁移

- `create_all()` **只建不改**：给已有表加列不生效、不报错，直到首次查询才炸
  `UndefinedColumn`。上线真实数据后第一次改 schema 就是事故。
- `migrations/` 基线由 autogenerate 生成后手修（生成的 `astext_type=Text()` 缺
  `Text` 导入，会 `NameError`，11 处改为 `sa.Text()`）。
- `scripts/migrate.sh` 会**探测「有 schema 但没有 `alembic_version`」**的库
  （即当前生产库这种 `create_all()` 建的库），自动 `stamp 0001_baseline` 再升级，
  不需要人工判断该 stamp 到哪一版。
- 实测：stamp 后 `alembic check` 输出 **"No new upgrade operations detected."** ——
  证明基线精确复现了线上 schema，而不是「看起来差不多」。
- `upgrade head --sql` 渲染 18 张表 / 10 个 JSONB / 26 个 TIMESTAMPTZ，可离线审阅。
- `alembic.ini` 的 `sqlalchemy.url` **刻意留空**，由 `env.py` 从 `get_settings()` 读，
  避免连接串（含口令）进仓库。加 `path_separator = os` 消掉弃用警告。

### 告警真正能送达（Alertmanager）

- 之前有 10 条规则、Prometheus UI 也会显示 firing，但 **Prometheus 不知道往哪发**：
  `prometheus.yml` 里没有 `alerting:` 块，`activeAlertmanagers` 为空。规则在响，
  没有人在被通知。
- 新增 `observability/alertmanager/alertmanager.yml`：按 severity 分级路由、
  `group_by` 防风暴、**抑制规则**（`GatewayDown` 时抑制 5xx/TTFT/完成率/成本的连带告警，
  只留根因；critical 抑制同 alertname 的 info）。
- **踩坑**：`--config.expand-env` 是 Loki/Promtail 的 flag，Alertmanager 没有，
  用了会 `unknown long flag` 并进入重启循环。改用 `webhook_configs.url_file` +
  entrypoint 写文件，密钥不进仓库。
- 启动前 `amtool check-config` 前置校验：**能启动但加载失败配置的监控组件比拒绝启动的更糟**。
- 新增 `scripts/observability_check.py`（Python 而非 shell，避免 heredoc 引号嵌套 bug），
  断言的是**链路**而非进程：4 个抓取目标 health、`activeAlertmanagers` 非空、
  规则数 ≥10、Alertmanager 配置含 receiver、Grafana 看板已挂载且无 provisioning 错误。
  实测 11/11 通过。

### 备份可验证（重写）

原 `backup.sh` 是 Dify 时代残留，三个缺陷：缺 `POSTGRES_CONTAINER` 就**静默跳过
`pg_dump` 却仍打印成功**、复制不存在的 `dify/workflows`、假设从项目根运行。

- 重写为：容器运行前置检查 → `pg_dump -Fc` → **校验归档里确实含 18 张表的数据**
  （`pg_restore -l` + grep）→ Redis `BGSAVE` 并等 `rdb_bgsave_in_progress` 归零 →
  记 `LASTSAVE` → sha256 MANIFEST → 保留 14 次（永不删当前）。
- 新增 `restore_backup.sh`（`--verify` 还原到临时库逐表比对后 DROP，从不碰线上库；
  `--yes` 破坏性还原）与 `verify_latest_backup.sh`。
- **两个自己引入又修掉的缺陷**：
  1. MANIFEST 把**正在写入的自己**也算进哈希 → 校验永远 FAILED。改用
     `find ! -name MANIFEST.txt`。
  2. 行数比对用 `pg_stat_user_tables.n_live_tup`（**估算值**，取决于 autovacuum 刷新时机），
     备份库与刚建的还原库必然对不上，产生与真实数据丢失**无法区分**的假告警。
     两侧统一改用精确 `count(*)`（单条 SQL 内用 `query_to_xml` 对每张表跑）。
- 实测：备份 155K / 18 张表；还原到临时库后 **18/18 张表行数精确一致**。
- `deployment/systemd/` 四个单元注册为 timer：每天 `03:20` 备份、每周日 `04:30` 校验，
  均 `Persistent=true`（关机错过会在开机后补跑），`TimeoutStartSec=900` 防挂死。
  已 `enable --now` 并手动触发一次实测通过。

### 观测看板改为生成

几十个面板的 PromQL 原始 JSON diff 无法 review（diff 里看不出是哪个面板变了），
必然腐坏。改为 `observability/grafana/build_dashboard.py` 生成，并在写出前**自证两条**：

1. **引用的指标真实存在** —— 从 `gateway/app/metrics.py` 正则提取所有
   Counter/Gauge/Histogram 名，逐面板扫 `expr`，引用不存在的 `gateway_*` 指标即非零退出
   （否则面板永远显示 `No data`，且没人知道为什么）。
2. **网格无重叠** —— 逐格占用检查。原布局引擎每个分组标题都压在上方图表上
   （游标从未推进 band 高度），自己写的检查抓到 44 处冲突后重写布局引擎。

- 输出路径修正为 Grafana 真正读取的 provisioning 树（原写到旁边一个没人读的文件）。
- `histogram_quantile` 的 `by (...)` 原来拼在 `_bucket` 之后 —— PromQL 能解析但
  **返回为空**；改为 `by (le, ...)` 附在 `sum(...)` 后。
- 新增 **⓪ 告警分组**（刻意放最前）：读 `ALERTS{alertstate="firing"}` ——
  这是 Prometheus 为每条规则自动生成的序列，**不依赖应用埋点**，
  是「告警响了没人知道」的兜底。另加通知投递失败数、Alertmanager up、依赖可用性扩到 4 个 job。
- 结果：32 个数据面板 + 8 个分组，66 个指标已校验、无网格重叠。

### 前端指标分析页

- 时间范围切换（24h / 7d / 30d）+ 手动刷新 + 数据时间戳。
- 每日消息量迷你柱图，**降级量作为柱内橙色子集**（降级不是另一根柱，是同一根柱的一部分）。
- 新增缓存命中率卡片与判定来源拆解（确定性 / LLM 兜底 / 模板）。
- `/api/admin/overview` 补 `cache_hit_tokens` / `cache_hit_rate` / `intent_source` /
  `compose_source` / `daily`，`daily` 从起始日到今日**逐日补齐**，空日填 0
  （否则前端柱图会把没数据的日子直接跳过，时间轴是错的）。
- `route_cache_hit` / `compose_cache_hit` 贯穿到持久化 `meta.cache_hit_tokens`，
  刷新后仍能解释「这次为什么便宜」。

### 门禁与 CI

- `scripts/release_gate.py` 重写：原版在 `LIVE_ACCEPTANCE=0` 时**只跑 pytest**。
  现在校验 7 个 YAML + `alembic.ini`（INI 解析）+ 看板 JSON，
  **重新生成看板并检查是否漂移**，跑 `alembic history`、pytest，
  `LIVE_ACCEPTANCE=1` 时跑真实链路（跳过时明确打印，支持 `--strict` 失败）。
- CI 新增 "Migration renders for PostgreSQL"（`alembic upgrade head --sql` + grep），
  `compileall` 覆盖 `migrations`。

### 测试

- 新增 `tests/test_migrations.py`（8 项）与 `tests/test_admin_overview.py`（10 项）。
- pytest **154 → 172 passed**。

## 2.1.0 — DeepSeek 接入、错误契约与前后端一致性

### 接入 DeepSeek

- 模型在**两个只读节点**上：低置信意图兜底、有接地依据时的措辞组织。
  仍然不能选工具、不能决定审批、不能改状态。
- **绕开一个会静默失效的坑**：DeepSeek V4 默认开启 thinking，先把 `max_tokens`
  花在 `reasoning_content` 上。意图分类只有 80 token 预算，实测会被推理吃光、
  `content` 返回**空字符串**，随后 JSON 解析失败、**意图兜底静默失效**，
  而服务看起来一切正常。默认 `LLM_THINKING=disabled`，并把"只有 reasoning、
  没有 content"识别为独立错误 `thinking_consumed_budget`，不当成正常空回答。
- 模型名改用官方文档名 `deepseek-flash`（`deepseek-chat` 实测仍可用，但会路由到
  同一模型，属未文档化别名，随时可能下线）。`LLM_BASE_URL` 去掉 `/v1`，
  客户端自己拼 `/chat/completions`。
- 换到不认 `thinking` 字段的端点时报 400 时，**自动去掉该字段重试一次并记住**，
  不会每次调用都失败一次。
- 非流式与流式两条路径都**不转发 `reasoning_content`**：那是模型的草稿而非答案，
  流进面向客户的对话气泡等于泄漏思维链。

### 成本计量修正

- 缓存命中的输入比未命中**便宜 50 倍**，原来按 `prompt_tokens` 统一计价会严重高估。
  现在解析 DeepSeek 的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，
  并兼容 OpenAI 的 `prompt_tokens_details.cached_tokens`；两者都缺失时按未命中算
  （保守假设）。单价取峰时牌价，低谷是半价。
- 实测对账吻合：245 输入 + 47 输出 → `0.0001299`，与记录一致。

### 统一错误契约（修掉一个真实的前后端联调缺陷）

- **FastAPI 的 422 把 `detail` 返回成数组**，而前端 `api()` 直接渲染 `body.detail`，
  用户看到的是 `[object Object]`。这是线上真实可见的问题。
- 新增 `gateway/app/errors.py`，所有错误统一信封
  `{detail, error:{code,message,fields[]}, request_id, trace_id}`。
  `detail` **保持字符串**（现有测试与客户端依赖它），`error` 是新增的结构化形式。
- 422 的字段名去掉 `body.` 传输层前缀——读起来就是用户需要改的那个字段。
- 429 补 `Retry-After`（固定窗口，给到当前分钟结束的秒数），不再逼客户端盲重试。
- 500 不再回显异常详情（可能含连接串），只给 trace_id，详情进日志。

### 前端

- 修复 `[object Object]`；错误按 `error.code` 分类呈现。
- `alert()` 换成 toast：不阻塞、可样式化、可被测试断言、能带 trace_id 一键复制。
- **SSE 流中断检测**：流在收到 `final` 之前结束（Caddy 重载、断网、worker 被杀）
  以前会让气泡留着半截文本、看起来像完整回答；现在标记「回答未完成」。
- **降级可见**：气泡带「模板回退」标记。并补上 `MessageOut.degraded` /
  `intent_source`——这两个以前只在流式时可见，**刷新后就消失了**，同一条消息
  会因为进入方式不同而长得不一样。
- 判定来源显示中文（`deterministic` → 确定性规则）而非裸枚举。
- 置信度改条形图；检索依据显示 BM25 / 向量分项贡献，"为什么命中"在界面上有答案。
- 登录表单支持回车提交；窄屏下三列栅格会崩，改为堆叠。
- 指标面板的分布从 chips 改成比例条。

### 项目规范

- `@app.on_event('startup')` 是弃用 API 且每次启动刷警告，两个服务都改成
  `lifespan`。弃用警告从 2 条降到 1 条（剩下的是 starlette 内部）。
- `readyz` 暴露 `llm` 状态块（模型 / thinking / 熔断器），但**刻意不进 `checks`**：
  模型挂了不该让服务变 `not_ready` 被负载均衡摘掉，主干本来不依赖模型。
- `docker-compose.yml` 补上 `LLM_THINKING` / `LLM_PRICE_IN_CACHED_PER_1K` /
  `LLM_MAX_TOKENS`。**没列出的变量永远不会进容器，在 `.env` 里设了也白设**。

### 验证

| 层 | 结果 |
|---|---|
| `pytest` | **154 passed**（新增 47 项：错误契约 + LLM 客户端） |
| `scripts/acceptance_probe.py` | **55 / 0** |
| `scripts/llm_probe.py`（新增） | **30 / 0** |
| `scripts/ui_smoke_test.mjs` | **17 / 0** |

## 2.0.0 — 确定性主干重构与上线

把网关从「Dify 的薄代理」重写成目标架构里的**确定性主干**，并部署到腾讯云。
Dify 保留为可选引擎（`ENGINE_MODE=dify`），原有 DSL 与流程不受影响。

### 新增能力

- **多租户两级隔离**：`tenant_id` + `store_id`；`tenant_id=''` 为平台全局文档。
  行级权限在**打分之前**过滤候选集，避免越权文档高分泄漏。
- **鉴权**：自实现 HS256 JWT（先验 `alg` 再验签）+ PBKDF2 200k 口令哈希，
  零加密依赖。令牌里的租户是唯一租户来源，请求头只能比对不能拓宽。
- **对话持久化**：`conversations` / `messages`（唯一约束 `(conversation_id, seq)`），
  消息行记录 `ttft_ms` / `latency_ms` / `tokens_in` / `cost_usd` / `sources`。
- **意图路由**：加权关键词优先，置信度低于阈值才调 LLM，三级转人工。
  挫败感检测前置到置信度判断之前。
- **知识库**：BM25 + 向量 RRF 融合；中文友好分词（ASCII 词 + CJK 单字 + bigram），
  无分词库依赖。向量通道已实现但默认关闭。
- **治理五道闸**：白名单 → 参数 Pydantic → 角色门 → 风险门 → 结果契约。
  高危工具进入 `waiting_approval`，审批用条件 UPDATE 保证只消费一次。
- **持久化工作流外壳**：`workflow_runs` + `workflow_checkpoints`，
  接口对齐 Temporal 子集（`start`/`run`/`signal`/`query`/`get_result`/`cancel`）。
- **可观测**：`contextvars` 全链路 trace、结构化日志、18 面板 Grafana 仪表盘、
  10 条告警规则。指标不带 `tenant_id`（避免基数爆炸），租户维度走 SQL。
- **评测**：107 个 pytest + 55 项公网端到端探针 + 17 项 jsdom UI 冒烟。

### 本次部署中修复的缺陷

前三个是端到端探针抓出来的，pytest 全绿时它们依然存在：

- **身份映射缺失**：网关把自己的用户 id 直接当工具参数传给业务服务。
  dev 模式下恰好能跑（默认身份就叫 `user001`），换成真实 JWT 登录后
  每次订单查询都变成越权拒绝。现在显式存在 `users.business_customer_id`。
- **审批结论未回写业务系统**：网关自己的 `approvals` 表不是订单系统的审批状态机。
  只在本侧标记 `approved`，订单系统仍回 `approval_required`，退款永远不执行，
  而网关侧看起来一切正常。新增 `ToolSpec.review_path`，执行前用审阅凭据回写结论。
- **`/metrics` 307 逃出路径前缀**：Starlette 把 `/metrics` 跳到 `/metrics/`，
  这个绝对路径在 `/support/` 前缀下会落到本机另一个站点，返回别人的登录页且状态码 200。
  Caddy 改为内部 rewrite。
- **GET 工具参数被丢弃**：非路径参数没有作为查询参数发出，业务服务收到
  缺 `user_id` 的请求直接 422，所有权检查根本没跑。
- **`audit_events` 表名冲突**：网关与业务服务共用数据库，两边都声明了
  `audit_events` 且结构不同。`create_all` 按名字幂等，第二个声明静默失效，
  模型从此不可用。移除网关侧未使用的定义，并加测试断言两服务表名不相交。
- **Grafana 无法启动**：`GF_SERVER_HTTP_ADDR=0.0.0.0:3000` 被当成主机名解析，
  容器重启循环。地址与端口拆成两个变量。
- **Grafana 挂载点不存在**：向镜像里不存在的目录挂载在 overlayfs 上
  `read-only file system` 失败。改用 Grafana 标准 provisioning 布局。
- **Caddyfile 改动不生效**：bind mount 不改变容器配置哈希，Compose 不重建，
  Caddy 也不自动重载。加 `--watch`。

第二轮验收又抓出两个**验证工具自身**的缺陷——产品代码没问题，但它们会让门禁说谎：

- **探针的持久化阈值写死了**：`消息已落库（>=8 条）` 这个数字是按"审批段跑完"定的。
  审批段在演示订单已被退过款时会**合法跳过**，会话里就只剩 4 条消息，于是
  一次正常的跳过被报成失败。阈值改为从本轮实际发出的轮次推导（`2 × turns_sent`），
  两条路径都成立：完整路径 55/55，跳过路径 44/0/1。丢写仍然会被抓到，
  因为下限跟着本轮真实发送量走。
- **重置脚本会静默空转**：`reset_demo_data.sh` 要求先 `cd deployment`，从项目根跑时
  `docker compose` 以 `no configuration file provided: not found` 退出——**一个字段都没改**，
  却因为管道掩盖了退出码而看起来像成功。改为按脚本自身位置定位
  `deployment/docker-compose.yml`（从任何目录可用），并在 SQL 里**自校验**
  `ORD-1001` / `ORD-2001` 均已恢复可退款，否则 `RAISE EXCEPTION` 非 0 退出。

### 部署

- 8 服务编排（postgres/redis/business-service/gateway/caddy/prometheus/grafana），
  两个网络：私有 `internal` + 复用宿主已有的 `novel-agents_web` 边缘网络。
- 只发布 Caddy 到边缘网络，Postgres/Redis/两个 Python 服务无宿主端口。
  Grafana 与 Prometheus 仅监听 `127.0.0.1`，走 SSH 隧道访问。
- `scripts/inject_edge_nginx.py` 幂等注入边缘 nginx location，
  注入前 `nginx -t`、失败自动回滚。
- `scripts/acceptance_probe.py` 公网端到端验收，`scripts/reset_demo_data.sh` 重置演示数据。

## 1.7.1-rc1

- Replaced client-supplied refund approval with a persisted reviewer decision.
- Added separate `X-Reviewer-API-Key` authorization and a reviewer-only OpenAPI provider.
- Added conditional refund execution to consume an approved proposal exactly once.
- Added reviewer role enforcement to the Gateway HITL proxy.
- Added CI-compatible Python 3.11/3.12 workflow and fixed the previous invalid GitHub Actions YAML.
- Added portfolio README, architecture diagram, interview guide, portfolio copy and design decisions.
- Expanded tests to cover credential separation, refund review/replay, Gateway reviewer role, OpenAPI isolation and DSL nodes.

## 1.7.0-rc1

- Fixed v1.1 Grounding Validator structured output / Merge Result contract.
- Added production Custom Tool + HITL Dify DSL template.
- Added FastAPI Gateway with auth boundary, request/trace IDs, rate limiting, SSE and HITL submit proxy.
- Added deterministic Business Service with ownership checks, tickets, refund proposal/execute state machine and audit.
- Added OpenAPI Custom Tool contract and public `/tool-api/` Nginx route for Cloud Dify demos.
- Added Prometheus/Grafana, Docker Compose, backup/restore scripts.
- Added unit tests, API tests, live test clients, HITL test, evaluation, acceptance and release gate.