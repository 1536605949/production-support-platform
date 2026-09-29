# Demo Scenarios

Recommended recording/demo order:

1. **Architecture**: show `docs/assets/architecture.svg` and explain the three boundaries.
2. **Knowledge / RAG**: ask a policy question. Dify routes it through retrieval and grounding.
3. **Order Tool**: ask for `ORD-1001`. The answer must use the real business service response.
4. **Cross-user protection**: user001 asks for `ORD-2001`. Business Service returns not found.
5. **Ticket**: create a support ticket and show a real `ticket_id`.
6. **Refund proposal**: Agent creates a proposal with `X-Tool-API-Key`.
7. **Approval boundary**: direct execute returns `409 approval_required`; reviewer approves with `X-Reviewer-API-Key`; Agent executes once; replay returns `409`.
8. **Prompt injection**: ask for the system prompt, API keys or a bypass. The response must not leak secrets or execute the write.
9. **Observability**: show structured request/trace logs and `/metrics`.
10. **Release evidence**: run `pytest -q` and `python scripts/release_gate.py`.

This order tells a system-design story rather than showing disconnected features.