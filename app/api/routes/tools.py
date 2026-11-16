# app/api/routes/tools.py
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.schemas.tools import ToolInfoResponse
from app.tools.dependencies import get_tool_gateway
from app.tools.types import ToolExecutionContext

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolInfoResponse])
async def list_tools(
    identity: Identity = Depends(get_current_identity),
) -> list[ToolInfoResponse]:
    """
    只返回当前身份可见（已注册、已启用、Scope 满足）的工具。

    不返回 handler、内部 URL 或鉴权细节。
    """
    gateway = get_tool_gateway()

    # 目录过滤是同步路径，只能读租户策略缓存；缓存为空时它会**跳过**策略判定
    # （执行前还有一次强制校验兜底）。放到这个接口上就是：权限页写着「拒绝」，
    # 工具页照样列出来。所以读目录前先预热一次——与 `plan_request` 的做法一致。
    await gateway.refresh_tenant_policy(identity.tenant_id)

    context = ToolExecutionContext(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        thread_id="",
        run_id="",
        request_id="",
        roles=identity.roles,
        scopes=identity.scopes,
    )

    # 目录视图里已经带着生效的 `requires_approval`（由 Gateway 用同一份判定算好）。
    # 不要再在这里调一次 `requires_approval(tool)`：那是平台兜底，
    # 会看不到租户把它设成「需确认」，于是工具页与权限页对同一个工具说两套话。
    visible = {
        view.tool_name: view
        for view in gateway.list_available_for_context(context=context)
    }

    return [
        ToolInfoResponse(
            name=tool.name,
            display_name=tool.display_name,
            description=tool.description,
            kind=tool.kind,
            risk_level=tool.risk_level,
            source=tool.source,
            timeout_seconds=tool.timeout_seconds,
            enabled=tool.enabled,
            requires_approval=visible[tool.name].requires_approval,
            required_scopes=list(tool.required_scopes),
            allowed_roles=list(tool.allowed_roles),
            input_schema=tool.input_schema,
            verification_hint=tool.verification_hint,
        )
        for tool in gateway.list_tools()
        if tool.name in visible
    ]
