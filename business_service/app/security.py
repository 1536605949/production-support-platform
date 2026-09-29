import hmac
from fastapi import Header, HTTPException
from .config import get_settings


def _require_key(value: str | None, expected: str, detail: str):
    if not value or not expected or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail=detail)


def require_tool_key(x_tool_api_key: str | None = Header(default=None)):
    """Credential for Agent-facing tools; it cannot approve refunds."""
    _require_key(x_tool_api_key, get_settings().tool_api_key, 'invalid tool api key')


def require_reviewer_key(x_reviewer_api_key: str | None = Header(default=None)):
    """Human-review credential kept separate from the Agent tool credential."""
    _require_key(x_reviewer_api_key, get_settings().reviewer_api_key, 'invalid reviewer api key')
