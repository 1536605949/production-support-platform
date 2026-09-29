from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


#: The one place this service states its version.
#:
#: Deliberately a module constant rather than a ``Settings`` field: a setting can
#: be overridden by an environment variable, and a service whose reported version
#: depends on the environment cannot be used to answer "which build is this?".
#: Must equal the repo root ``VERSION`` file — ``tests/test_version_consistency.py``
#: fails on any drift. It was previously duplicated as two separate ``'1.7.1'``
#: literals in ``main.py``, which is how a service ends up reporting two versions.
SERVICE_VERSION = '2.6.0'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    environment: str = 'development'
    database_url: str = 'sqlite:///./data/business.db'
    tool_api_key: str = 'dev-tool-key'
    reviewer_api_key: str = 'dev-reviewer-key'
    reviewer_id: str = 'demo-reviewer'


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_placeholder_secret(value: str) -> bool:
    value = (value or '').strip()
    return value in {
        '',
        'dev-tool-key',
        'dev-reviewer-key',
        'change-this-tool-key',
        'change-this-reviewer-key',
    } or value.lower().startswith(('replace', 'change-me', 'change-this'))


def validate_settings(s: Settings) -> None:
    """Reject unsafe defaults when the service is explicitly production."""
    if s.environment.lower() != 'production':
        return
    problems = []
    if is_placeholder_secret(s.tool_api_key):
        problems.append('TOOL_API_KEY must be a strong non-placeholder secret')
    if is_placeholder_secret(s.reviewer_api_key):
        problems.append('REVIEWER_API_KEY must be a strong non-placeholder secret')
    if s.tool_api_key == s.reviewer_api_key:
        problems.append('TOOL_API_KEY and REVIEWER_API_KEY must be different')
    if not s.reviewer_id or s.reviewer_id == 'demo-reviewer':
        problems.append('REVIEWER_ID must identify the human reviewer service principal')
    if problems:
        raise RuntimeError('Unsafe production configuration: ' + '; '.join(problems))
