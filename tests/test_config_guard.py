# -*- coding: utf-8 -*-
import pytest
from gateway.app.config import Settings, validate_settings, is_placeholder_secret


def test_placeholder_detection():
    assert is_placeholder_secret('')
    assert is_placeholder_secret('REPLACE_WITH_NEW_APP_KEY')
    assert is_placeholder_secret('change-me')
    assert is_placeholder_secret('change-this-to-a-long-random-secret')
    assert not is_placeholder_secret('app-real-key-123')
    assert not is_placeholder_secret('a' * 40)


def test_production_guard_rejects_placeholder_keys():
    s = Settings(environment='production', engine_mode='dify', dify_api_key='REPLACE_WITH_NEW_APP_KEY',
                 jwt_secret='a' * 40, auth_mode='jwt')
    with pytest.raises(RuntimeError):
        validate_settings(s)


def test_production_guard_rejects_dev_auth():
    s = Settings(environment='production', engine_mode='dify', dify_api_key='app-real-key',
                 jwt_secret='a' * 40, auth_mode='dev')
    with pytest.raises(RuntimeError):
        validate_settings(s)


def test_production_guard_rejects_short_jwt_secret():
    s = Settings(environment='production', engine_mode='dify', dify_api_key='app-real-key',
                 jwt_secret='short', auth_mode='jwt')
    with pytest.raises(RuntimeError):
        validate_settings(s)


def test_production_guard_passes_hardened_dify_config():
    s = Settings(environment='production', engine_mode='dify', dify_api_key='app-real-key',
                 jwt_secret='a' * 40, auth_mode='jwt')
    validate_settings(s)  # must not raise


def test_production_guard_passes_hardened_builtin_config():
    s = Settings(
        environment='production', engine_mode='builtin', auth_mode='jwt',
        jwt_secret='a' * 40, tool_api_key='real-tool-key-value',
        database_url='postgresql+psycopg://support:pw@postgres:5432/support',
    )
    validate_settings(s)  # must not raise


def test_production_guard_rejects_sqlite_in_builtin_mode():
    s = Settings(
        environment='production', engine_mode='builtin', auth_mode='jwt',
        jwt_secret='a' * 40, tool_api_key='real-tool-key-value',
        database_url='sqlite:///./data/gateway.db',
    )
    with pytest.raises(RuntimeError):
        validate_settings(s)