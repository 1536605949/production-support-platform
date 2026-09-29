# START HERE

This repository has two run levels:

1. **Offline engineering demo**: Gateway mock + Business Service. No Dify quota or Docker required.
2. **Full Dify integration**: publish a bound Dify workflow and configure two independent Tool providers.

## 1. Offline demo

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Terminal 1:

```powershell
$env:DATABASE_URL='sqlite:///./data/business.db'
$env:TOOL_API_KEY='dev-tool-key'
$env:REVIEWER_API_KEY='dev-reviewer-key'
$env:REVIEWER_ID='demo-reviewer'
python -m uvicorn business_service.app.main:app --port 9000
```

Terminal 2:

```powershell
$env:REDIS_URL='redis://127.0.0.1:6390/0'
$env:DIFY_MOCK='1'
$env:AUTH_MODE='dev'
python -m uvicorn gateway.app.main:app --port 8000
```

Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/readyz
Invoke-RestMethod http://127.0.0.1:9000/healthz
python scripts/acceptance.py
```

For a fresh refund demo, remove `data/business.db` before starting. A successful refund consumes `ORD-1001`.

## 2. Configure Dify

Dify Cloud cannot call `localhost:9000`. Expose the Nginx `/tool-api/` route through an HTTPS tunnel or real domain, then render both contracts:

```bash
python scripts/render_tool_openapi.py \
  --input tools/business_api.openapi.yaml \
  --output tools/business_api.bound.openapi.yaml \
  --server-url https://YOUR-DOMAIN/tool-api

python scripts/render_tool_openapi.py \
  --input tools/reviewer_api.openapi.yaml \
  --output tools/reviewer_api.bound.openapi.yaml \
  --server-url https://YOUR-DOMAIN/tool-api
```

Import both OpenAPI contracts in Dify:

- Agent provider credential: `X-Tool-API-Key`
- Reviewer provider credential: `X-Reviewer-API-Key`

Do not attach the reviewer provider to an autonomous Agent node.

Bind the DSL template:

```bash
python scripts/patch_dify_dsl.py \
  --input dify/workflows/v1.7_production_custom_tool.template.dify.yml \
  --output dify/workflows/v1.7_production.bound.dify.yml \
  --dataset-id YOUR_DATASET_ID \
  --tool-provider-id YOUR_AGENT_TOOL_PROVIDER_ID \
  --reviewer-tool-provider-id YOUR_REVIEWER_TOOL_PROVIDER_ID
```

Import the bound DSL, run Dify Publish Check, and re-export the published version.

## 3. Gateway against Dify

```powershell
$env:DIFY_BASE_URL='https://api.dify.ai/v1'
$env:DIFY_API_KEY='YOUR_PUBLISHED_APP_KEY'
$env:AUTH_MODE='dev'
python -m uvicorn gateway.app.main:app --port 8000
```

Tests:

```bash
python scripts/test_dify_direct.py --query "退款政策是多少？"
python scripts/test_gateway.py --query "退款政策是多少？"
python scripts/test_hitl_flow.py --action none
```

## 4. Full Compose

```bash
cp deployment/.env.example deployment/.env
# Fill DIFY_API_KEY, TOOL_API_KEY, REVIEWER_API_KEY and other secrets.
docker compose --env-file deployment/.env -f deployment/docker-compose.yml up -d --build
```

## 5. Release gate

```bash
python scripts/release_gate.py
LIVE_ACCEPTANCE=1 python scripts/release_gate.py
```

The second command requires published Dify resources and running Gateway/Business Service.