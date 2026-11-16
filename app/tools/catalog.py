# app/tools/catalog.py
from __future__ import annotations

from typing import Any

from app.tools.gateway import ToolGateway
from app.tools.types import ToolExecutionContext


def build_model_tool_catalog(
    *,
    gateway: ToolGateway,
    context: ToolExecutionContext,
) -> list[dict[str, Any]]:
    """
    返回可提供给 LLM 的工具目录。

    gateway 应负责：
    - 过滤未注册工具；
    - 过滤已禁用工具；
    - 过滤当前角色/Scope 不允许使用的工具；
    - 移除 Secret、内部 URL、服务端实现细节。
    """
    catalog: list[dict[str, Any]] = []

    for tool in gateway.list_available_for_context(context=context):
        catalog.append(
            {
                "tool_name": tool.tool_name,
                "display_name": tool.display_name,
                "description": tool.description,
                "risk_level": tool.risk_level.value,
                "input_schema": tool.public_input_schema,
                # 直接用 Gateway 算好的值，不在这里再判一次：
                # 模型看到的「是否需确认」必须与实际执行前的判定同源，
                # 两处各算一遍时，租户的 confirm 只会生效在其中一处。
                "requires_approval": tool.requires_approval,
            }
        )

    return catalog
