from pathlib import Path
import yaml
ROOT=Path(__file__).parents[1]

def test_baseline_dsl_validator_structured_output_enabled():
    d=yaml.safe_load((ROOT/'dify/workflows/v1.1_fixed_baseline.dify.yml').read_text(encoding='utf-8'))
    nodes={n['id']:n for n in d['workflow']['graph']['nodes']}
    assert nodes['validator']['data']['structured_output_enabled'] is True
    assert ['validator','structured_output','final_answer'] in nodes['merge']['data']['variables']

def test_v17_template_has_expected_placeholders_and_nodes():
    p=ROOT/'dify/workflows/v1.7_production_custom_tool.template.dify.yml'
    text=p.read_text(encoding='utf-8'); d=yaml.safe_load(text)
    assert '__SUPPORT_KNOWLEDGE_DATASET_ID__' in text
    assert '__BUSINESS_TOOL_PROVIDER_ID__' in text
    assert '__REVIEWER_TOOL_PROVIDER_ID__' in text
    types={n['data']['type'] for n in d['workflow']['graph']['nodes']}
    assert {'knowledge-retrieval','tool','human-input','question-classifier','variable-aggregator','answer'} <= types
    tool_names = {n['data'].get('tool_name') for n in d['workflow']['graph']['nodes'] if n['data']['type'] == 'tool'}
    assert {'create_refund_proposal', 'review_refund', 'execute_refund'} <= tool_names
