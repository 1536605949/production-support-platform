# Interview Script

The current one-page talk track is in [INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md).

Recommended sequence:

1. State the problem: a Dify RAG demo is not a safe enterprise API boundary.
2. Explain the three boundaries: Gateway, Dify Runtime, Business Service.
3. Show the refund chain: proposal -> independent reviewer -> approved execution.
4. Show code: `gateway/app/security.py`, `business_service/app/service.py`.
5. Show evidence: `pytest -q` and `python scripts/release_gate.py`.
6. State boundaries: Dify template binding, reviewer identity, migrations and Redis HA remain production evolution work.

Do not lead with a feature list. Lead with the authorization problem and the system-design response.