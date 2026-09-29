# API Testing

## 1. Static release gate

```bash
python scripts/release_gate.py
```

This validates YAML/OpenAPI/Compose contracts, runs pytest, and skips live checks unless `LIVE_ACCEPTANCE=1`.

## 2. Direct Dify

Use this first whenever Gateway returns an error:

```bash
export DIFY_BASE_URL=https://api.dify.ai/v1
export DIFY_API_KEY=app-NEW-KEY
python scripts/test_dify_direct.py --query "退款政策是多少？"
```

Expected: HTTP 200 with a non-empty `answer`.

## 3. Gateway

```bash
python scripts/test_gateway.py --query "退款政策是多少？"
```

Expected: HTTP 200 with `answer`, `request_id` and `trace_id`.

## 4. Business API

Run Business Service locally, then:

```bash
export TOOL_API_KEY=dev-tool-key
export REVIEWER_API_KEY=dev-reviewer-key
python scripts/test_business_api.py
```

It verifies owned-order access, cross-user protection and ticket creation.

## 5. Refund authorization boundary

A full live acceptance run requires a fresh local database because successful refund execution consumes `ORD-1001`:

```bash
export BUSINESS_BASE_URL=http://localhost:9000
export TOOL_API_KEY=dev-tool-key
export REVIEWER_API_KEY=dev-reviewer-key
python scripts/acceptance.py
```

Expected sequence:

1. Proposal is created.
2. Direct execute with Agent key returns `409 approval_required`.
3. Reviewer key approves the proposal.
4. Agent key executes it once.
5. Replay returns `409`.

## 6. Live HITL workflow

After publishing the bound v1.7 template:

```bash
python scripts/test_hitl_flow.py --action none
python scripts/test_hitl_flow.py --action approve
```

The first call must receive `human_input_required`. A real production reviewer UI should use the reviewer identity/RBAC path, not a shared long-lived API key.

## 7. Full online gate

```bash
LIVE_ACCEPTANCE=1 python scripts/release_gate.py
```