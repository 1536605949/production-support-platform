import pytest

from business_service.app.config import Settings, validate_settings


def test_production_rejects_shared_or_default_credentials():
    settings = Settings(
        environment='production',
        tool_api_key='dev-tool-key',
        reviewer_api_key='dev-reviewer-key',
        reviewer_id='demo-reviewer',
    )
    with pytest.raises(RuntimeError):
        validate_settings(settings)


def test_production_accepts_separated_hardened_credentials():
    settings = Settings(
        environment='production',
        tool_api_key='tool-' + 'a' * 40,
        reviewer_api_key='reviewer-' + 'b' * 40,
        reviewer_id='human-reviewer-01',
    )
    validate_settings(settings)
