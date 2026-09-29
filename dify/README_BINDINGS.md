# Dify Workspace Bindings

`v1.7_production_custom_tool.template.dify.yml` is a production template and must not be presented as a specific Workspace's final DSL.

Required bindings:

1. `__SUPPORT_KNOWLEDGE_DATASET_ID__`
2. `__BUSINESS_TOOL_PROVIDER_ID__`
3. `__REVIEWER_TOOL_PROVIDER_ID__`
4. DeepSeek/model credentials
5. Knowledge embedding and retrieval settings
6. Agent provider credential: `X-Tool-API-Key`
7. Reviewer provider credential: `X-Reviewer-API-Key`

The Agent and Reviewer contracts are intentionally separate:

- `tools/business_api.openapi.yaml` contains order/account/ticket and refund proposal/execute operations.
- `tools/reviewer_api.openapi.yaml` contains only `review_refund`.
- Do not attach the Reviewer provider to an autonomous Agent node.
- The Agent credential cannot approve a refund.

Bind the template with:

```bash
python scripts/patch_dify_dsl.py \
  --input dify/workflows/v1.7_production_custom_tool.template.dify.yml \
  --output dify/workflows/v1.7_production.bound.dify.yml \
  --dataset-id YOUR_DATASET_ID \
  --tool-provider-id YOUR_AGENT_TOOL_PROVIDER_ID \
  --reviewer-tool-provider-id YOUR_REVIEWER_TOOL_PROVIDER_ID
```

After import, check:

- Tool nodes expose the expected `text` output.
- Human Input approve/reject edges lead through the reviewer tool.
- The execute node no longer receives `approval_action`.
- Final Merge variables are selectable.
- Dify Publish Check has zero blocking errors.

Because Custom Tool output schemas can differ across Dify versions, **Publish Check plus re-export is the final authority**.