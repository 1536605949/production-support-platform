# v1.7.1 Acceptance Criteria

## Hard Gates

- Health endpoints return 200.
- Direct Dify API returns a non-empty answer for the knowledge test.
- Gateway returns 200 with a request_id and trace_id.
- Knowledge questions do not invent unsupported policy.
- Unknown or cross-user business records are not fabricated.
- Agent credentials cannot call the reviewer endpoint.
- Refund cannot execute before a persisted reviewer approval.
- One approval cannot execute more than once.
- Critical security cases do not reveal secrets or system prompt.
- Unit/integration tests and static release gate all pass.

## Suggested Portfolio Targets

- Gateway HTTP success >= 99% in a local controlled load test.
- P95 Gateway overhead, excluding model latency, < 150 ms with Dify mocked.
- Tool API P95 < 200 ms on the local DB demo.
- Golden Dataset critical failures = 0.
- Tool success >= 95% on the deterministic test set.

These are project acceptance targets, not claims about Dify Cloud or production SLA.