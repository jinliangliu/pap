# tests/unit/test_checkpoint_config.py
"""检查点库的地址解析与后端判定。

要点：SQLAlchemy 方言后缀（+psycopg）必须剥掉才能交给 psycopg；
非 PostgreSQL 一律判定为不支持，不允许静默退回本地文件。
"""

from __future__ import annotations

from app.core.config import Settings


def test_checkpoint_reuses_database_url_by_default():
    s = Settings(
        checkpoint_database_url="",
        database_url="postgresql+psycopg://agent:pw@127.0.0.1:5432/personal_agent",
    )

    assert s.resolved_checkpoint_database_url == (
        "postgresql://agent:pw@127.0.0.1:5432/personal_agent"
    )
    assert s.checkpoint_uses_postgres is True


def test_checkpoint_url_overrides_database_url():
    s = Settings(
        checkpoint_database_url="postgresql://agent:pw@127.0.0.1:5432/agent_state",
        database_url="postgresql+psycopg://agent:pw@127.0.0.1:5432/personal_agent",
    )

    assert s.resolved_checkpoint_database_url == (
        "postgresql://agent:pw@127.0.0.1:5432/agent_state"
    )


def test_non_postgres_checkpoint_is_not_supported():
    s = Settings(
        checkpoint_database_url="",
        database_url="sqlite+aiosqlite:///./data/app.db",
    )

    assert s.checkpoint_uses_postgres is False
