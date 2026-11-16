# tests/unit/test_config.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings


def test_defaults_are_safe():
    assert settings.app_name == "personal-agent-platform"
    assert settings.api_prefix == "/api/v1"
    assert settings.approval_expire_seconds == 300
    assert settings.tool_default_timeout_seconds > 0


def test_production_flag():
    assert (
        Settings(
            app_env="production",
            auth_mode="gateway",
            redis_url="redis://127.0.0.1:6379/0",
        ).is_production
        is True
    )
    assert Settings(app_env="development").is_production is False


def test_production_requires_gateway_auth():
    """生产环境必须由可信身份源提供身份，dev 模式不得对外服务。"""
    with pytest.raises(ValidationError, match="AUTH_MODE=dev"):
        Settings(
            app_env="production",
            auth_mode="dev",
            redis_url="redis://127.0.0.1:6379/0",
        )


def test_production_allows_jwt_auth_with_long_secret():
    """自验签同样属于可信身份源，但密钥必须足够长。"""
    assert (
        Settings(
            app_env="production",
            auth_mode="jwt",
            jwt_secret="x" * 40,
            redis_url="redis://127.0.0.1:6379/0",
        ).auth_mode
        == "jwt"
    )

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(
            app_env="production",
            auth_mode="jwt",
            jwt_secret="short",
            redis_url="redis://127.0.0.1:6379/0",
        )


def test_production_requires_shared_rate_limit():
    """生产环境不接受进程内限流：多进程下配额会被放大到 N 倍。"""
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings(app_env="production", auth_mode="gateway", redis_url="")


def test_rate_limit_backend_resolution():
    assert (
        Settings(
            rate_limit_backend="auto", redis_url="redis://h:6379/0"
        ).resolved_rate_limit_backend
        == "redis"
    )
    assert Settings(rate_limit_backend="auto", redis_url="").resolved_rate_limit_backend == "memory"
    assert (
        Settings(
            rate_limit_backend="memory", redis_url="redis://h:6379/0"
        ).resolved_rate_limit_backend
        == "memory"
    )


def test_redis_backend_requires_url():
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings(rate_limit_backend="redis", redis_url="")


def test_gateway_mode_flag():
    assert Settings(auth_mode="gateway").trusts_gateway_identity is True
    assert Settings(auth_mode="dev", app_env="development").trusts_gateway_identity is False


def test_sqlite_flag():
    assert Settings(database_url="sqlite+aiosqlite:///./data/app.db").is_sqlite is True
    assert Settings(database_url="postgresql+psycopg://u:p@h/db").is_sqlite is False


def test_approval_ttl_alias(monkeypatch):
    monkeypatch.setenv("APPROVAL_DEFAULT_TTL_SECONDS", "120")
    assert Settings().approval_expire_seconds == 120
