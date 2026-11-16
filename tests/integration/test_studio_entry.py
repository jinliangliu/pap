# tests/integration/test_studio_entry.py
"""LangGraph Studio 入口的上下文注入。

Studio 只提供对话输入，身份必须由服务端补齐；客户端带进来的
user_id / tenant_id 不得生效——与 API 使用同一条规则。
"""

from __future__ import annotations

import pytest

from app.agents import studio as studio_module
from app.agents.studio import prepare_context
from app.core.config import settings


@pytest.fixture(autouse=True)
def _fresh_mcp_registration():
    """每个用例都从「尚未注册」开始，避免用例间互相吃掉一次性注册。"""
    studio_module.reset_mcp_registration()

    yield

    studio_module.reset_mcp_registration()


async def test_prepare_context_ignores_client_supplied_identity(seeded_identity):
    state = {
        "messages": [],
        "thread_id": "studio-thread-1",
        "user_id": "attacker",
        "tenant_id": "evil-tenant",
        "roles": ["admin"],
        "scopes": ["*"],
    }

    result = await prepare_context(state)

    assert result["user_id"] == seeded_identity["user_id"]
    assert result["tenant_id"] == settings.default_tenant_id
    assert result["roles"] != ["admin"]
    assert "station.read" in result["scopes"]
    assert result["thread_id"] == "studio-thread-1"
    assert result["run_id"]


async def test_prepare_context_generates_new_run_id_per_turn(seeded_identity):
    """同一线程连续两次调用必须换 run_id，否则审批幂等键会复用旧记录。"""
    first = await prepare_context({"messages": [], "thread_id": "studio-thread-2"})
    second = await prepare_context({"messages": [], "thread_id": "studio-thread-2"})

    assert first["run_id"] != second["run_id"]


async def test_prepare_context_rejects_unknown_subject(monkeypatch):
    monkeypatch.setattr(settings, "dev_subject", "ghost-user")

    with pytest.raises(RuntimeError, match="seed_dev_data"):
        await prepare_context({"messages": []})


async def test_prepare_context_registers_mcp_tools_once(seeded_identity, monkeypatch):
    """`langgraph dev` 不跑 lifespan，Studio 必须自己注册一次 MCP 工具。

    否则注册表里只有本地 station 工具，问「压力趋势」会被规划成
    get_station_snapshot，再用 MCP 站点码去过 PAP 的 station_grants，
    必然返回 tool_rejected。
    """
    calls: list[object] = []

    async def fake_register(gateway, *, extra_specs=()):
        calls.append((gateway, extra_specs))
        return 11

    monkeypatch.setattr(studio_module, "register_mcp_tools", fake_register)

    first = await prepare_context({"messages": [], "thread_id": "t-1"})
    second = await prepare_context({"messages": [], "thread_id": "t-1"})

    assert len(calls) == 1
    assert first["tenant_id"] == settings.default_tenant_id
    assert second["run_id"] != first["run_id"]


async def test_prepare_context_survives_mcp_registration_failure(seeded_identity, monkeypatch):
    """MCP 不可达时 Studio 仍要能开起来，只是少一组远端工具。"""

    async def boom(_gateway, *, extra_specs=()):
        raise RuntimeError("mcp down")

    monkeypatch.setattr(studio_module, "register_mcp_tools", boom)

    result = await prepare_context({"messages": [], "thread_id": "t-2"})

    assert result["user_id"] == seeded_identity["user_id"]
