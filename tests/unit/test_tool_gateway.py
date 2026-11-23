# tests/unit/test_tool_gateway.py
from __future__ import annotations

import asyncio

import pytest

from app.models.approval import RiskLevel
from app.tools.gateway import (
    ToolApprovalRequiredError,
    ToolGateway,
    ToolGatewayError,
    ToolPermissionDeniedError,
    ToolPreflightRejectedError,
)
from app.tools.registry import ToolRegistry
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)


def context(
    *,
    scopes: tuple[str, ...] = ("tools:read",),
    roles: tuple[str, ...] = (),
    is_platform_admin: bool = False,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="t1",
        user_id="u1",
        thread_id="th1",
        run_id="r1",
        request_id="req1",
        scopes=scopes,
        roles=roles,
        is_platform_admin=is_platform_admin,
    )


def build_gateway(*tools: ToolDefinition, audit_sink=None) -> ToolGateway:
    registry = ToolRegistry()
    gateway = ToolGateway(registry, audit_sink=audit_sink)

    for tool in tools:
        gateway.register_tool(tool)

    return gateway


async def echo_handler(ctx, arguments):  # noqa: ANN001
    return ToolExecutionResult(success=True, result={"echo": arguments})


async def write_handler(ctx, arguments):  # noqa: ANN001
    return ToolExecutionResult(success=True, result={"written": True})


def read_tool(**overrides) -> ToolDefinition:
    defaults = {
        "name": "echo",
        "display_name": "回声",
        "description": "回显参数。",
        "kind": "python",
        "risk_level": RiskLevel.READ,
        "required_scopes": ("tools:read",),
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "handler": echo_handler,
    }
    defaults.update(overrides)
    return ToolDefinition(**defaults)


def write_tool(**overrides) -> ToolDefinition:
    defaults = {
        "name": "write",
        "display_name": "写操作",
        "description": "有副作用的操作。",
        "kind": "python",
        "risk_level": RiskLevel.WRITE,
        "requires_approval": True,
        "required_scopes": ("tools:write",),
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "handler": write_handler,
    }
    defaults.update(overrides)
    return ToolDefinition(**defaults)


async def test_unregistered_tool_is_rejected():
    gateway = build_gateway(read_tool())

    with pytest.raises(ToolGatewayError):
        await gateway.execute_read_only(
            context=context(),
            tool_name="run_shell_command",
            arguments={},
        )


async def test_schema_validation_rejects_missing_arguments():
    gateway = build_gateway(read_tool())

    with pytest.raises(ToolGatewayError):
        await gateway.execute_read_only(
            context=context(),
            tool_name="echo",
            arguments={},
        )


async def test_missing_scope_is_denied():
    gateway = build_gateway(read_tool())

    with pytest.raises(ToolPermissionDeniedError):
        await gateway.execute_read_only(
            context=context(scopes=()),
            tool_name="echo",
            arguments={"value": "x"},
        )


async def test_platform_admin_executes_read_tool_without_explicit_scope():
    """管理员豁免必须落在**执行拦截**上，不能只在目录过滤里生效。

    目录过滤（`_has_access`）与执行拦截（`_check_permissions`）是两处判断。
    线上踩过一次：只改前者，结果是「目录里看得见、一调用就被拒」，报的还是
    「缺少调用该工具所需的权限」——看着像权限配错，实际是两份判断走散了。
    """
    gateway = build_gateway(read_tool())

    result = await gateway.execute_read_only(
        context=context(scopes=(), is_platform_admin=True),
        tool_name="echo",
        arguments={"value": "x"},
    )

    assert result.success is True


async def test_platform_admin_executes_write_tool_without_explicit_scope():
    """写路径单独盯一遍：`execute_approved` 是实际出问题的那个入口。"""
    gateway = build_gateway(write_tool())

    result = await gateway.execute_approved(
        context=context(scopes=(), is_platform_admin=True),
        tool_name="write",
        arguments={"value": "x"},
    )

    assert result.success is True


async def test_platform_admin_does_not_bypass_agent_boundary():
    """管理员只跳过 scope 这一层。智能体边界比人更严，不能跟着一起放开。"""
    gateway = build_gateway(read_tool())

    with pytest.raises(ToolPermissionDeniedError):
        await gateway.execute_read_only(
            context=ToolExecutionContext(
                tenant_id="t1",
                user_id="u1",
                thread_id="th1",
                run_id="r1",
                request_id="req1",
                scopes=(),
                roles=(),
                is_platform_admin=True,
                agent_allowed_tools=(),
            ),
            tool_name="echo",
            arguments={"value": "x"},
        )


async def test_write_tool_cannot_be_executed_on_read_path():
    gateway = build_gateway(write_tool())

    with pytest.raises(ToolApprovalRequiredError):
        await gateway.execute_read_only(
            context=context(scopes=("tools:read", "tools:write")),
            tool_name="write",
            arguments={"value": "x"},
        )


async def test_write_tool_requires_approval_even_if_flag_missing():
    tool = write_tool(requires_approval=False)  # 故意漏配
    gateway = build_gateway(tool)

    assert gateway.requires_approval(tool) is True


async def test_plan_carries_the_operation_summary():
    """确认卡正文来自这里：参数是一整份结构时，摘要就是审批人唯一看得懂的那段。"""

    async def summarize(ctx, arguments):  # noqa: ANN001
        return f"将写入 {arguments['value']}"

    gateway = build_gateway(write_tool(summarize=summarize))

    plan = await gateway.prepare_call(
        context=context(scopes=("tools:write",)),
        tool_name="write",
        arguments={"value": "abc"},
        reason="",
    )

    assert plan.summary == "将写入 abc"


async def test_plan_carries_the_shape_when_the_tool_gives_one():
    """摘要可以带一张结构图：图只由工具给结构，不让模型现场编 SVG。

    卡片上那张图（`preview.graph`）就是从 `plan.graph` 落下去的。老钩子只返回
    字符串，所以这里同时盯住「字符串仍然能用」——不然加图就得改每个工具。
    """

    async def summarize(ctx, arguments):  # noqa: ANN001
        from app.tools.types import ToolBrief

        return ToolBrief(
            text="新建流程「巡检」",
            graph={"entry": "a", "nodes": [{"id": "a"}], "edges": []},
        )

    gateway = build_gateway(write_tool(summarize=summarize))

    plan = await gateway.prepare_call(
        context=context(scopes=("tools:write",)),
        tool_name="write",
        arguments={"value": "abc"},
        reason="",
    )

    assert plan.summary == "新建流程「巡检」"
    assert plan.graph["entry"] == "a"


async def test_a_broken_summary_does_not_block_the_approval():
    """摘要失败只让卡片少一段正文，不能把「本来能确认的操作」变成报错。"""

    async def summarize(ctx, arguments):  # noqa: ANN001
        raise RuntimeError("boom")

    gateway = build_gateway(write_tool(summarize=summarize))

    plan = await gateway.prepare_call(
        context=context(scopes=("tools:write",)),
        tool_name="write",
        arguments={"value": "abc"},
        reason="",
    )

    assert plan.summary == ""
    assert plan.requires_approval is True


async def test_tool_without_summarizer_has_an_empty_summary():
    gateway = build_gateway(write_tool())

    plan = await gateway.prepare_call(
        context=context(scopes=("tools:write",)),
        tool_name="write",
        arguments={"value": "abc"},
        reason="",
    )

    assert plan.summary == ""


async def test_timeout_returns_structured_failure():
    async def slow_handler(ctx, arguments):  # noqa: ANN001
        await asyncio.sleep(0.05)
        return ToolExecutionResult(success=True)

    tool = ToolDefinition(
        name="slow",
        display_name="慢工具",
        description="用于验证超时。",
        kind="python",
        risk_level=RiskLevel.READ,
        required_scopes=("tools:read",),
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=slow_handler,
        timeout_seconds=0,
    )
    gateway = build_gateway(tool)

    result = await gateway.execute_read_only(
        context=context(),
        tool_name="slow",
        arguments={"value": "x"},
    )

    assert result.success is False
    assert result.error_code == "TOOL_TIMEOUT"


async def test_catalog_hides_tools_without_scope():
    gateway = build_gateway(read_tool(), write_tool())

    visible = gateway.list_available_for_context(context=context(scopes=("tools:read",)))
    names = {view.tool_name for view in visible}

    assert names == {"echo"}


async def test_preflight_rejection_blocks_approval_creation():
    """预检必须真的被调到，否则写在工具上的理由永远不会生效。

    只测预检函数本身是不够的：接线漏了，函数照样单测通过，
    线上却还是会给注定失败的操作弹确认卡。
    """

    async def refuse(ctx, arguments):  # noqa: ANN001
        return "这个对象不能修改。"

    tool = write_tool(preflight=refuse)
    gateway = build_gateway(tool)

    with pytest.raises(ToolPreflightRejectedError) as exc_info:
        await gateway.check_resource_access(
            context=context(scopes=("tools:write",)),
            tool_name="write",
            arguments={"value": "x"},
        )

    assert "这个对象不能修改" in str(exc_info.value)


async def test_preflight_pass_lets_approval_through():
    async def allow(ctx, arguments):  # noqa: ANN001
        return None

    tool = write_tool(preflight=allow)
    gateway = build_gateway(tool)

    # 不抛出即通过：审批单可以正常生成。
    await gateway.check_resource_access(
        context=context(scopes=("tools:write",)),
        tool_name="write",
        arguments={"value": "x"},
    )


class _MemorySink:
    """把审计事件留在内存里，用来断言网关到底写了什么。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


async def test_tool_success_audit_records_which_agent_ran_it():
    """成功路径也要带 agent_name，与拒绝路径对称。

    少了这一列，审计里只能看到「某个工具跑过」；构建助手和普通智能体共用
    同一批工具时，就没法回答「这条是谁干的」。
    """
    sink = _MemorySink()
    gateway = build_gateway(read_tool(), audit_sink=sink)

    await gateway.execute_read_only(
        context=ToolExecutionContext(
            tenant_id="t1",
            user_id="u1",
            thread_id="th1",
            run_id="r1",
            request_id="req1",
            scopes=("tools:read",),
            agent_name="studio",
            agent_revision=3,
        ),
        tool_name="echo",
        arguments={"value": "x"},
    )

    event, payload = sink.events[-1]
    assert event == "tool_executed"
    assert payload["agent_name"] == "studio"
    assert payload["agent_revision"] == 3
