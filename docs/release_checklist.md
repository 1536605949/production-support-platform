# Release Checklist

- [ ] Secrets are not committed; leaked keys have been rotated.
- [ ] Agent provider uses `X-Tool-API-Key`.
- [ ] Reviewer provider uses a different `X-Reviewer-API-Key`.
- [ ] Reviewer provider is not attached to autonomous Agent nodes.
- [ ] Dify Publish Check has no blocking errors.
- [ ] Current production DSL is exported and archived.
- [ ] `pytest -q` passes.
- [ ] Direct Dify smoke passes.
- [ ] Gateway smoke passes.
- [ ] Business tool health passes.
- [ ] Acceptance hard gates pass.
- [ ] Golden/Regression eval has zero critical failures.
- [ ] Refund cannot execute before reviewer approval.
- [ ] Approved refund cannot be replayed.
- [ ] Backup completed.
- [ ] Rollback image/DSL identified.
- [ ] Prometheus/Grafana reachable.
- [ ] Load test reviewed.