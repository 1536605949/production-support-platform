# demo-kit（面试素材归档）

> 这里放“面试官能快速看到”的素材：架构图、录屏、截图和演示顺序。完整讲法见 `docs/INTERVIEW_GUIDE.md`。

## 推荐三段录屏

| 文件 | 内容 | 重点 |
|---|---|---|
| `01_architecture.mp4` | 架构图 + 代码走查 | 为什么需要三个边界 |
| `02_offline_demo.mp4` | 离线演示退款授权闭环 | 409 -> reviewer -> executed -> replay blocked |
| `03_dify_cloud.mp4` | Dify Studio 录屏（有额度时） | RAG、Grounding、两个 Tool Provider、Publish Check |

## 演示前检查

- [ ] 使用干净数据库，`ORD-1001` 尚未退款。
- [ ] `pytest -q` 通过。
- [ ] `python scripts/release_gate.py` 通过。
- [ ] `docs/assets/architecture.svg` 能正常打开。
- [ ] 能解释 Agent Key 与 Reviewer Key 为什么必须分离。
- [ ] 能说清 v1.7.1 DSL 模板仍需要 Workspace 绑定和 Publish Check。

## 当前工程证据

- 本地测试基线：`31 passed`。
- CI：Python 3.11/3.12 matrix + compileall + release gate。
- 安全边界：Agent Tool Key 不能调用 reviewer 接口；审批后只能执行一次。
- Dify Provider：Agent 与 Reviewer 使用独立 OpenAPI 和独立 credential。

## 演示命令

```powershell
# 终端 1：业务服务
$env:DATABASE_URL='sqlite:///./data/business.db'
$env:TOOL_API_KEY='dev-tool-key'
$env:REVIEWER_API_KEY='dev-reviewer-key'
$env:REVIEWER_ID='demo-reviewer'
python -m uvicorn business_service.app.main:app --port 9000

# 终端 2：离线 Gateway
$env:REDIS_URL='redis://127.0.0.1:6390/0'
$env:DIFY_MOCK='1'
$env:AUTH_MODE='dev'
python -m uvicorn gateway.app.main:app --port 8000

# 终端 3：验收
python scripts/acceptance.py
```