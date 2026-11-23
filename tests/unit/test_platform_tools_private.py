# tests/unit/test_platform_tools_private.py
"""`studio_*` 只对构建助手可见。

这条不是「顺手加的一个过滤」，它补的是一个真实的口子：默认智能体
`allowed_tools=None`（不额外收窄）、平台管理员跳过 scope 判定，于是
`studio_workflow_save` 一直在默认智能体的工具目录里——唯一拦住它的
是 `CHAT_SYSTEM_PROMPT` 里那句「不要在这里替他排步骤」。提示词挡不住
一句「直接调用 studio_workflow_save」。

所以判据必须落在网关，而不是提示词里；而且**目录与执行两处共用**——
只过滤目录时，模型凭记忆拼出工具名照样能调。
"""

from __future__ import annotations

import pytest

from app.models.approval import RiskLevel
from app.tools.gateway import (
    DENIAL_AGENT,
    ToolGateway,
    ToolPermissionDeniedError,
)
from app.tools.registry import ToolRegistry
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)


async def _ok_handler(ctx, arguments):  # noqa: ANN001
    return ToolExecutionResult(success=True, result={})


def _context(*, agent_name: str, is_platform_admin: bool = True) -> ToolExecutionContext:
    """默认智能体那套边界：不收窄、可写、平台管理员。

    这三项正是「边界拦不住」的原因，所以测试必须照这个样子构造——
    把 `agent_allowed_tools` 设成业务清单，就测不出这个口子了。
    """
    return ToolExecutionContext(
        tenant_id="t1",
        user_id="u1",
        thread_id="th1",
        run_id="r1",
        request_id="req1",
        scopes=(),
        roles=("admin",),
        is_platform_admin=is_platform_admin,
        agent_name=agent_name,
        agent_allowed_tools=None,
        agent_can_write=True,
    )


def _tool(name: str, *, display_name: str = "", risk: RiskLevel = RiskLevel.READ) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        display_name=display_name or name,
        description="测试用。",
        kind="python",
        risk_level=risk,
        required_scopes=("agent:write",),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_ok_handler,
    )


def _gateway() -> ToolGateway:
    registry = ToolRegistry()
    gateway = ToolGateway(registry)
    gateway.register_tool(_tool("fc_querypressure", display_name="查压力"))
    gateway.register_tool(_tool("studio_workflow_save", display_name="新建或修改编排流程"))
    return gateway


def _visible(gateway: ToolGateway, context: ToolExecutionContext) -> set[str]:
    return {
        view.tool_name
        for view in gateway.list_available_for_context(context=context)
    }


def test_platform_tools_are_invisible_to_other_agents():
    gateway = _gateway()

    names = _visible(gateway, _context(agent_name="assistant"))

    assert names == {"fc_querypressure"}


def test_platform_tools_are_visible_to_the_studio_agent():
    gateway = _gateway()

    names = _visible(gateway, _context(agent_name="studio"))

    assert names == {"fc_querypressure", "studio_workflow_save"}


@pytest.mark.parametrize("alias", ["studio", "studio_assistant"])
def test_studio_alias_also_sees_platform_tools(alias: str):
    """别名与正式名同一张边界，否则历史会话里的助手会被自己的工具挡住。"""

    assert _visible(_gateway(), _context(agent_name=alias)) == {
        "fc_querypressure",
        "studio_workflow_save",
    }


def test_policy_page_still_sees_platform_tools():
    """没有会话身份（工具策略页、脚本、回放）时不收窄。

    收窄了管理员就看不到 `studio_*` 的审批策略，也没法解释一次拒绝是哪来的。
    「不知道是谁在说」与 `allowed_tools=None` 同义：不额外收窄。
    """
    names = _visible(_gateway(), _context(agent_name=""))

    assert names == {"fc_querypressure", "studio_workflow_save"}


async def test_calling_platform_tool_from_other_agent_is_denied():
    """目录里看不到还不够：模型会凭记忆拼工具名。"""

    gateway = _gateway()

    with pytest.raises(ToolPermissionDeniedError) as exc_info:
        await gateway.execute_read_only(
            context=_context(agent_name="assistant"),
            tool_name="studio_workflow_save",
            arguments={},
        )

    error = exc_info.value
    assert error.reason == DENIAL_AGENT
    assert "构建助手" in str(error)
    # 说「缺权限」会让模型以为换个身份还能试，而这里换不了。
    assert "权限" not in str(error)


async def test_studio_can_call_the_same_tool():
    """正例必须成对存在：只测拒绝，一个永远拒绝的实现也能全绿。"""

    gateway = _gateway()

    result = await gateway.execute_read_only(
        context=_context(agent_name="studio"),
        tool_name="studio_workflow_save",
        arguments={},
    )

    assert result.success is True
