# Production Runbook

## Dify unavailable

1. `python scripts/test_dify_direct.py`.
2. Verify DIFY_BASE_URL, API key, provider quota and Dify status.
3. Check Gateway logs by trace_id.
4. Return 502/504 to client; do not synthesize a fake successful answer.

## Business Tool unavailable

1. Check Business Service `/healthz`.
2. Check tool public/private network path from Dify.
3. Check `X-Tool-API-Key` credential.
4. Do not fall back to invented order/account data.

## Database unavailable

1. Business readiness should fail.
2. Stop write operations.
3. Restore DB or switch to validated standby/snapshot.

## Release

1. Backup.
2. Export current Dify DSL.
3. Run pytest/eval/acceptance in staging.
4. Deploy.
5. Smoke test.
6. Monitor error rate/latency.
7. Roll back application image + DSL version if critical gate fails.
