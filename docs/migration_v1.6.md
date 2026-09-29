# Cloud -> Self-host Migration

Keep these contracts stable:

- Gateway request/response schema
- Agent and Reviewer Business Tool OpenAPI contracts
- Golden/Regression datasets

Change only environment/runtime bindings:

- `DIFY_BASE_URL`
- `DIFY_API_KEY`
- Model provider credential
- Knowledge Dataset ID
- Agent Tool Provider ID and `X-Tool-API-Key`
- Reviewer Tool Provider ID and `X-Reviewer-API-Key`

Export from Cloud, import to Self-host, re-bind resources and both tool providers, run Publish Check, then re-export the final DSL. Workspace IDs and provider IDs do not survive migration automatically.