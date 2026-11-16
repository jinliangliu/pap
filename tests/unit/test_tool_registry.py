# tests/unit/test_tool_registry.py
from __future__ import annotations

import pytest

from app.models.approval import RiskLevel
from app.tools.registry import (
    ToolAlreadyRegisteredError,
    ToolNotFoundError,
    ToolRegistry,
)
from app.tools.types import ToolDefinition, ToolExecutionResult


async def _handler(context, arguments):  # noqa: ANN001
    return ToolExecutionResult(success=True)


def _tool(name: str = "demo_tool", *, enabled: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        display_name="演示工具",
        description="仅用于测试。",
        kind="python",
        risk_level=RiskLevel.READ,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handler,
        enabled=enabled,
    )


def test_register_and_get():
    registry = ToolRegistry()
    registry.register(_tool())

    assert registry.get("demo_tool").name == "demo_tool"
    assert len(registry) == 1


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry()
    registry.register(_tool())

    with pytest.raises(ToolAlreadyRegisteredError):
        registry.register(_tool())


def test_unregistered_tool_raises():
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError):
        registry.get("run_shell_command")


def test_disabled_tools_hidden_by_default():
    registry = ToolRegistry()
    registry.register(_tool(enabled=False))

    assert registry.list_tools() == []
    assert len(registry.list_tools(include_disabled=True)) == 1
