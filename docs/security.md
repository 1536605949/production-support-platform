# Security Design

1. Browser does not receive the Dify API key or Business Tool keys.
2. Gateway derives the Dify `user` from an authenticated principal, not from arbitrary request content.
3. Gateway requires a `reviewer` role before proxying HITL actions.
4. Business Service re-checks resource ownership; LLM and tool parameters are untrusted input.
5. Agent-facing tools use `X-Tool-API-Key`.
6. Human review uses a separate `X-Reviewer-API-Key` and a separate Dify provider.
7. The Agent credential can create a refund proposal but cannot approve it.
8. `execute_refund` does not accept an `approval_action` flag; it reads persisted proposal state.
9. Refund execution uses a conditional state transition so one approval is consumed once.
10. RAG documents are untrusted evidence, never instructions.
11. Logs redact Authorization, API-key and token fields.
12. Production uses `AUTH_MODE=jwt`; dev auth and `X-Demo-*` headers are local-demo mechanisms only.
13. Rotate any Dify or reviewer key that has appeared in chat, terminal history or source control.

## Production replacement path

- Replace reviewer API keys with OIDC/SSO identities and RBAC.
- Store secrets in a managed secret service instead of `.env`.
- Add immutable audit storage and retention policy.
- Move rate limiting to highly available Redis and define fail-open/fail-closed behavior.
- Add Alembic migrations, database backups and restore drills.
- Keep the reviewer tool provider off autonomous Agent nodes.