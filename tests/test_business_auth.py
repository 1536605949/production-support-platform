import pytest
from fastapi import HTTPException

from business_service.app import security


def test_tool_and_reviewer_credentials_are_separated(monkeypatch):
    settings = security.get_settings()
    monkeypatch.setattr(settings, 'tool_api_key', 'tool-secret')
    monkeypatch.setattr(settings, 'reviewer_api_key', 'reviewer-secret')

    security.require_tool_key('tool-secret')
    security.require_reviewer_key('reviewer-secret')

    with pytest.raises(HTTPException) as tool_error:
        security.require_tool_key('reviewer-secret')
    assert tool_error.value.status_code == 401

    with pytest.raises(HTTPException) as reviewer_error:
        security.require_reviewer_key('tool-secret')
    assert reviewer_error.value.status_code == 401
