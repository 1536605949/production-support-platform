from pathlib import Path

import yaml


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding='utf-8'))


def test_agent_tools_do_not_contain_reviewer_capability():
    document = load('tools/business_api.openapi.yaml')
    operations = set()
    for methods in document['paths'].values():
        for method, spec in methods.items():
            if method.lower() in {'get', 'post', 'put', 'patch', 'delete'}:
                operations.add(spec['operationId'])
    assert {'get_order', 'get_account', 'get_sku', 'check_aftersale_eligibility',
            'create_ticket', 'create_refund_proposal', 'execute_refund'} <= operations
    assert 'review_refund' not in operations
    assert document['components']['securitySchemes']['ToolApiKey']['name'] == 'X-Tool-API-Key'
    execute_schema = document['paths']['/refunds/execute']['post']['requestBody']['content']['application/json']['schema']
    assert execute_schema['required'] == ['user_id', 'order_id', 'proposal_id']
    assert 'approval_action' not in execute_schema['properties']


def test_the_eligibility_contract_documents_a_three_valued_verdict():
    """``eligible`` is true / false / null, and ``null`` is not a refusal.

    A consumer that reads this contract as a boolean will treat "cannot decide"
    as "no" and refuse returns the shop owes, so the nullability is part of the
    published interface rather than an implementation detail.
    """
    document = load('tools/business_api.openapi.yaml')
    schema = (document['paths']['/aftersale/eligibility']['post']
              ['responses']['200']['content']['application/json']['schema'])
    assert schema['properties']['eligible']['nullable'] is True
    assert 'MUST NOT be treated as a refusal' in (
        document['paths']['/aftersale/eligibility']['post']['description']
    )


def test_reviewer_api_has_separate_credential_and_message_boundary():
    document = load('tools/reviewer_api.openapi.yaml')
    assert document['paths']['/refunds/review']['post']['operationId'] == 'review_refund'
    assert document['components']['securitySchemes']['ReviewerApiKey']['name'] == 'X-Reviewer-API-Key'
