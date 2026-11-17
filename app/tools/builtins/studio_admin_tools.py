# app/tools/builtins/studio_admin_tools.py
"""构建助手的第二组工具：技能、自定义工具、知识库。

与 `studio_tools.py` 的分工：那边管「智能体本身」（建改删、启停、边界恢复），
这边管「智能体能用到的东西」（技能、工具、资料）。两处共用同一条治理链路：
注册表 → `ToolGateway` → 审批 → 审计，一个都不绕。

**凭据是本模块的第一条硬约束。** 平台的配置表里没有地方存 Token / 密码，
HTTP 工具的鉴权走租户凭据引用，MCP 端点凭据只存在接入项里且接口只回
`token_set`。助手如果接受一个 `token` 参数，那条值就会经过对话、上下文、
日志与审批卡——所以在参数进来之前就按字段名拒掉，而不是指望模型自觉。

第二条：**这些写操作都要求平台管理员身份。** 技能按租户隔离（受租户作用域
约束），但自定义工具与 MCP 接入项是平台级表（`ToolRegistry` 只有一份），
普通成员改一处会影响所有租户。判定口径与 REST 路由的 `require_platform_admin`
对齐，不另发明一套。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.knowledge import documents, store
from app.knowledge.index import get_knowledge_index
from app.knowledge.ingest import (
    SCOPE_REQUIREMENTS,
    SOURCE_UPLOAD,
    VALID_SCOPES,
    KnowledgeIngestError,
    build_document,
)
from app.models.agent import Agent
from app.models.approval import RiskLevel
from app.models.audit import AuditEventType
from app.models.conversation import Message, MessageRole
from app.models.custom_tool import CustomTool
from app.models.document import DocumentStatus
from app.models.mcp_registered import McpRegisteredTool
from app.models.workflow import WorkflowRunStatus
from app.schemas.custom_tool import CustomToolCreate
from app.services import attachment_store, skill_service
from app.services.custom_tool_service import (
    ensure_name_available,
    register_row,
    unregister_row,
    validate_payload,
)
from app.services.mcp_tool_service import (
    McpToolNameError,
    ensure_name_allowed,
)
from app.services.mcp_tool_service import (
    register_row as register_mcp_row,
)
from app.tenancy.context import tenant_scope
from app.tools.builtins.studio_tools import (
    WRITE_PERMISSION,
    _audit_mutation,
    _check_grantable,
    _fail,
)
from app.tools.custom_http import CustomToolConfigError
from app.tools.gateway import ToolGatewayError
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

SKILL_PERMISSION = "skill:write"
# 工具配置与建改智能体同一条权限：能改边界的人才能改它能调用的工具，
# 否则「把工具塞进目录」会变成绕过 boundary 的旁路。
TOOL_PERMISSION = WRITE_PERMISSION
KNOWLEDGE_PERMISSION = "knowledge:write"

# 连接器定义是平台级配置；凭据另算（见 connectors 路由的 credential 端点）。
CONNECTOR_PERMISSION = "connector:write"

# 编排流程与 `PATCH/DELETE /api/v1/workflows` 同一条权限：定义「谁在第几步做什么」
# 是治理动作。这里刻意**不**要求平台管理员——流程是租户级配置（挂 RLS），
# 与平台级的工具表、连接器不是一类东西；多一道更严的口径会让「控制台能改、
# 助手不能改」变成一条查不出原因的差异。
WORKFLOW_PERMISSION = WRITE_PERMISSION

# 凭据类字段名。命中即拒绝，不区分大小写，也不看值长什么样——
# 判断「这个字符串是不是密钥」永远会漏，判断「这个字段名是不是在要密钥」不会。
_CREDENTIAL_PATTERN = re.compile(
    r"(token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|authorization|cookie|bearer|credential)",
    re.IGNORECASE,
)

# `credential_ref` 是个**引用**（环境变量名），不是密钥本身，必须放行：
# 拦住它等于把「安全的用法」也一起禁掉，人只会转而去贴明文。
_CREDENTIAL_ALLOWED = frozenset({"credential_ref"})

CREDENTIAL_REJECTED = (
    "助手不接收凭据字段「{field}」。Token、密码、API Key 这类信息请到控制台"
    "对应的配置页用安全输入填写，或改用租户凭据引用（credential_ref）。"
    "已经被贴进对话的密钥，请当作已泄露并立即轮换。"
)


def _find_credential_field(value: Any, path: str = "") -> str | None:
    """递归找第一个「看起来在要密钥」的字段名。

    只看键名，不看值：值可能是任意文本，判不准；而键名是人写 schema 时
    自己起的，写 `api_key` 就是打算收一个密钥。
    """
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if name not in _CREDENTIAL_ALLOWED and _CREDENTIAL_PATTERN.search(name):
                return f"{path}{name}"
            found = _find_credential_field(item, f"{path}{name}.")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_credential_field(item, f"{path}[{index}].")
            if found:
                return found
    return None


def _guard_credentials(arguments: dict[str, Any]) -> str | None:
    field = _find_credential_field(arguments)
    return CREDENTIAL_REJECTED.format(field=field) if field else None


def _is_admin(context: ToolExecutionContext) -> bool:
    return bool(context.is_platform_admin)


# ---------------- 技能 ----------------


async def studio_skill_save(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """新建或修改一个技能。

    `mode` 与建智能体同一条理由：不做「存在就改」的自动判断。名字错一个字符时
    静默新建一个技能，审批卡上只写着「新建 cs2」，人要能一眼看出是新建还是修改。
    """
    blocked = _guard_credentials(arguments)
    if blocked:
        return _fail(blocked)

    mode = str(arguments.get("mode") or "").strip()
    if mode not in {"create", "update"}:
        return _fail("mode 必须是 create 或 update。")

    name = str(arguments.get("name") or "").strip()
    if not name:
        return _fail("需要指出技能的标识（name）。")

    sent_tools = "allowed_tools" in arguments
    tools = arguments.get("allowed_tools")
    if sent_tools:
        reason = _check_grantable(tools, context)
        if reason:
            return _fail(reason)

    keywords = arguments.get("trigger_keywords")

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            try:
                existing = await skill_service.get_by_name(session, name)
            except skill_service.SkillError as exc:
                return _fail(str(exc))

            if mode == "create":
                if existing is not None:
                    return _fail(f"技能标识「{name}」已存在，要修改它请用 mode=update。")
                try:
                    row = await skill_service.create_skill(
                        session,
                        name=name,
                        display_name=str(arguments.get("display_name") or name),
                        description=str(arguments.get("description") or ""),
                        instruction=str(arguments.get("instruction") or ""),
                        trigger_keywords=keywords,
                        allowed_tools=tools if sent_tools else None,
                        priority=int(arguments.get("priority") or 100),
                        enabled=bool(arguments.get("enabled", True)),
                        source="studio",
                        created_by=context.user_id,
                    )
                except (skill_service.SkillExistsError, skill_service.SkillError) as exc:
                    return _fail(str(exc))

                await session.commit()
                await session.refresh(row)

                skill_id = str(row.id)
                view = _skill_view(row)
                result = ToolExecutionResult(
                    success=True,
                    verified=True,
                    result={"skill": view},
                    message=(
                        f"已新建技能「{view['display_name']}」（{view['name']}）："
                        f"{'启用' if view['enabled'] else '停用'} · 工具：{_tools_label(view)}。"
                    ),
                    verification={"source": "skills_table", "ref": view["name"]},
                )

                await _audit_admin(
                    context,
                    tool_name="studio_skill_save",
                    event_type=AuditEventType.ADMIN_ACTION,
                    resource_type="skill",
                    resource_id=skill_id,
                    message=f"构建助手新建技能「{view['display_name']}」（{view['name']}）",
                    metadata={"operation": "skill_created", **view},
                )
                return result

            if existing is None:
                return _fail(f"没有找到名为「{name}」的技能，要新建请用 mode=create。")

            before = _skill_view(existing)
            try:
                row = await skill_service.update_skill(
                    session,
                    existing,
                    display_name=arguments.get("display_name"),
                    description=arguments.get("description"),
                    instruction=arguments.get("instruction"),
                    trigger_keywords=keywords,
                    allowed_tools=tools,
                    priority=arguments.get("priority"),
                    enabled=arguments.get("enabled"),
                    tools_provided=sent_tools,
                )
            except skill_service.SkillError as exc:
                return _fail(str(exc))

            await session.commit()
            await session.refresh(row)

            skill_id = str(row.id)
            view = _skill_view(row)
            changed = {key for key in view if before.get(key) != view[key]}
            result = ToolExecutionResult(
                success=True,
                verified=True,
                result={"skill": view},
                message=(
                    f"已更新技能「{view['display_name']}」（{view['name']}）："
                    f"{'启用' if view['enabled'] else '停用'} · 工具：{_tools_label(view)}。"
                ),
                verification={"source": "skills_table", "ref": view["name"]},
            )

            if changed:
                await _audit_admin(
                    context,
                    tool_name="studio_skill_save",
                    event_type=AuditEventType.ADMIN_ACTION,
                    resource_type="skill",
                    resource_id=skill_id,
                    message=f"构建助手修改技能「{view['display_name']}」（{view['name']}）",
                    metadata={
                        "operation": "skill_updated",
                        "changed": sorted(changed),
                        "before": {key: before.get(key) for key in changed},
                        "after": {key: view.get(key) for key in changed},
                    },
                )
            return result


async def studio_skill_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """删除技能。同样要求逐字核对名字——理由与删智能体一致：不能靠模型自觉。"""
    name = str(arguments.get("name") or "").strip()
    confirm = str(arguments.get("confirm_name") or "").strip()

    if not name:
        return _fail("需要指出要删除的技能标识（name）。")
    if confirm != name:
        return _fail(f"删除需要再次确认名称：请让用户回复完整的标识「{name}」后再试一次。")

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            row = await skill_service.get_by_name(session, name)
            if row is None:
                return _fail(f"没有找到名为「{name}」的技能。")

            skill_id = str(row.id)
            display = row.display_name
            # 技能的 allowed_skills 引用是按名字写的，删掉之后那些引用会变成
            # 一个永远命中不了的名字。不阻止删除，但必须说出来——否则表现为
            # 「明明配了技能，为什么它不用」。
            users = [
                agent.display_name or agent.name
                for agent in await session.scalars(select(Agent))
                if name in (agent.allowed_skills or [])
            ]

            await skill_service.delete_skill(session, row)
            await session.commit()

            gone = await skill_service.get_by_name(session, name)
            if gone is not None:
                return ToolExecutionResult(
                    success=False,
                    message="删除未生效，请稍后重试或到「技能」页处理。",
                    error_code="studio_delete_unverified",
                )

            message = f"已删除技能「{display}」。"
            if users:
                message += (
                    f"以下智能体的边界里还写着它，需要时请一并调整：{'、'.join(users)}。"
                )

            result = ToolExecutionResult(
                success=True,
                verified=True,
                result={"deleted": name, "display_name": display, "referenced_by": users},
                message=message,
                verification={"source": "skills_table", "absent": name},
            )

            await _audit_admin(
                context,
                tool_name="studio_skill_delete",
                event_type=AuditEventType.ADMIN_ACTION,
                resource_type="skill",
                resource_id=skill_id,
                message=f"构建助手删除技能「{display}」（{name}）",
                metadata={"operation": "skill_deleted", "name": name, "referenced_by": users},
            )
            return result


def _skill_view(row: Any) -> dict[str, Any]:
    return {
        "name": row.name,
        "display_name": row.display_name,
        "description": row.description,
        "enabled": row.enabled,
        "allowed_tools": row.allowed_tools,
        "trigger_keywords": row.trigger_keywords,
        "priority": row.priority,
    }


def _tools_label(view: dict[str, Any]) -> str:
    tools = view.get("allowed_tools")
    if tools is None:
        return "不限"
    return f"{len(tools)} 个" if tools else "无（一个都调不动）"


# ---------------- 自定义工具 / MCP 工具 ----------------


def _http_payload(
    arguments: dict[str, Any],
    existing: CustomTool | None = None,
) -> CustomToolCreate:
    """把工具参数拼成一份完整定义。

    更新走「读出现有 → 覆盖传入字段 → 整体校验」而不是逐字段赋值：
    URL 与 path 参数的对应关系只在整体校验时才看得见，逐字段改很容易留下
    「URL 里有个 {station_id}，但参数表里已经没有它」这种自相矛盾的配置。
    """

    def pick(key: str, default: Any = None) -> Any:
        if key in arguments:
            return arguments.get(key)
        if existing is not None:
            return getattr(existing, key)
        return default

    return CustomToolCreate(
        name=existing.name if existing is not None else str(arguments.get("name") or "").strip(),
        display_name=str(pick("display_name") or ""),
        description=str(pick("description") or ""),
        method=pick("method", "GET"),
        url=str(pick("url") or ""),
        risk_level=pick("risk_level", RiskLevel.READ),
        timeout_seconds=int(pick("timeout_seconds", 20) or 20),
        params=pick("params", []),
        headers=pick("headers", {}),
        enabled=bool(pick("enabled", True)),
    )


async def studio_tool_save(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """新建或修改一个工具。

    三种来源的区别不是分类癖好，而是「能不能改」完全不同：
    - `http`：控制台建的 HTTP 工具，可增可改；
    - `mcp`：把远端 MCP 工具接入白名单，只能新增（远端没有就没有，不能凭空写）；
    - `python`：代码注册的内置工具，只读——没有任何运行时新增路径。
    """
    blocked = _guard_credentials(arguments)
    if blocked:
        return _fail(blocked)

    if not _is_admin(context):
        return _fail("自定义工具与 MCP 接入是平台级配置，只有平台管理员能改。")

    kind = str(arguments.get("kind") or "").strip()
    if kind not in {"http", "mcp", "python"}:
        return _fail("kind 必须是 http、mcp 或 python。")

    if kind == "python":
        return _fail(
            "Python 工具是代码里注册的内置工具，没有运行时新增或修改的入口。"
            "要加或改，得走代码变更与部署；我可以帮你查现有的（resource=tool）。"
        )

    mode = str(arguments.get("mode") or "").strip()
    if mode not in {"create", "update"}:
        return _fail("mode 必须是 create 或 update。")

    if kind == "mcp":
        if mode != "create":
            return _fail("MCP 接入项不支持就地修改：请先取消接入，再按新端点重新接入。")
        return await _save_mcp_tool(context, arguments)

    return await _save_http_tool(context, arguments, mode)


async def _save_http_tool(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
    mode: str,
) -> ToolExecutionResult:
    from app.tools.dependencies import get_tool_gateway

    gateway = get_tool_gateway()
    name = str(arguments.get("name") or "").strip()
    if not name:
        return _fail("需要指出工具标识（name）。")

    async with async_session_factory() as session:
        existing = await session.scalar(select(CustomTool).where(CustomTool.name == name))

        if mode == "create":
            if existing is not None:
                return _fail(f"工具标识「{name}」已存在，要修改它请用 mode=update。")
            try:
                payload = _http_payload(arguments)
                validate_payload(payload)
                ensure_name_available(gateway=gateway, name=payload.name)
            except (CustomToolConfigError, ToolGatewayError, ValueError) as exc:
                return _fail(f"工具定义不合法：{exc}")

            row = CustomTool(
                name=payload.name,
                display_name=payload.display_name,
                description=payload.description,
                method=payload.method,
                url=payload.url,
                risk_level=payload.risk_level,
                timeout_seconds=payload.timeout_seconds,
                params=[entry.model_dump() for entry in payload.params],
                headers=payload.headers,
                enabled=payload.enabled,
                created_by=context.user_id,
            )
            session.add(row)
            await session.flush()
        else:
            if existing is None:
                return _fail(f"没有找到标识为「{name}」的自定义工具，要新建请用 mode=create。")
            try:
                payload = _http_payload(arguments, existing)
                validate_payload(payload)
            except (CustomToolConfigError, ToolGatewayError, ValueError) as exc:
                return _fail(f"工具定义不合法：{exc}")

            row = existing
            row.display_name = payload.display_name
            row.description = payload.description
            row.method = payload.method
            row.url = payload.url
            row.risk_level = payload.risk_level
            row.timeout_seconds = payload.timeout_seconds
            row.params = [entry.model_dump() for entry in payload.params]
            row.headers = payload.headers
            row.enabled = payload.enabled
            await session.flush()

        tool_id = str(row.id)
        view = {
            "name": row.name,
            "display_name": row.display_name,
            "method": row.method,
            "url": row.url,
            "risk_level": row.risk_level.value,
            "enabled": row.enabled,
            "params": [entry.get("name") for entry in (row.params or [])],
        }
        await session.commit()
        # commit 之后实例上的属性全部过期，同步函数里再读会触发隐式 IO，
        # 在 async 会话里直接 MissingGreenlet。所以先 refresh 再交给网关。
        await session.refresh(row)
        # 注册放在 commit 之后：注册表是进程内状态，先注册后提交的话，
        # 提交失败会留下一个「能用但库里没有」的工具。
        registered = register_row(gateway, row)

    if not registered:
        return ToolExecutionResult(
            success=False,
            message=(
                f"工具「{view['display_name']}」已保存，但注册到网关失败，"
                "这次调用拿不到它。请检查 URL 与参数后重试，或到「工具」页处理。"
            ),
            error_code="studio_tool_register_failed",
            result={"tool": view},
        )

    result = ToolExecutionResult(
        success=True,
        verified=True,
        result={"tool": view},
        message=(
            f"已{'新建' if mode == 'create' else '更新'}工具「{view['display_name']}」"
            f"（{view['name']}）：{view['method']} {view['url']} · "
            f"风险 {view['risk_level']} · {'启用' if view['enabled'] else '停用'}。"
        ),
        verification={"source": "tool_registry", "ref": view["name"]},
    )

    await _audit_admin(
        context,
        tool_name="studio_tool_save",
        event_type=AuditEventType.ADMIN_ACTION,
        resource_type="custom_tool",
        resource_id=tool_id,
        message=(
        f"构建助手{'新建' if mode == 'create' else '更新'}自定义工具"
        f"「{view['display_name']}」（{view['name']}）"
        ),
        metadata={"operation": f"custom_tool_{mode}d", **view},
    )
    return result


async def _save_mcp_tool(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    from app.tools.dependencies import get_tool_gateway
    from app.tools.mcp_tools import fetch_remote_tools

    gateway = get_tool_gateway()
    local_name = str(arguments.get("name") or "").strip()
    remote_name = str(arguments.get("remote_name") or "").strip()
    if not local_name or not remote_name:
        return _fail("接入 MCP 工具需要本地标识（name）与远端工具名（remote_name）。")

    try:
        ensure_name_allowed(local_name, gateway=gateway)
    except McpToolNameError as exc:
        return _fail(str(exc))

    endpoint = str(arguments.get("server_url") or "").strip()

    # 不传端点就落到 .env 的 MCP_SERVER_URL，令牌也由平台按租户取。
    # 外部端点只在「不需要鉴权」时能走这条路——需要令牌的端点请到控制台接入，
    # 令牌不从对话里过。
    try:
        descriptors = await fetch_remote_tools(url=endpoint, token="")
    except Exception as exc:
        return _fail(
            f"无法从 {endpoint or '默认 MCP 端点'} 取得工具清单：{exc}。"
            "如果需要令牌才能访问，请到控制台「工具 → MCP」页接入。"
        )

    descriptor = descriptors.get(remote_name)
    if descriptor is None:
        names = "、".join(sorted(descriptors)[:20]) or "无"
        return _fail(f"远端没有名为「{remote_name}」的工具。远端现有：{names}。")

    try:
        risk = RiskLevel(str(arguments.get("risk_level") or RiskLevel.READ.value))
    except ValueError:
        return _fail("risk_level 不合法。")

    async with async_session_factory() as session:
        exists = await session.scalar(
            select(McpRegisteredTool).where(McpRegisteredTool.local_name == local_name)
        )
        if exists is not None:
            return _fail(f"本地标识「{local_name}」已经接过一个 MCP 工具了。")

        row = McpRegisteredTool(
            local_name=local_name,
            remote_name=remote_name,
            display_name=str(arguments.get("display_name") or remote_name)[:128],
            description=str(arguments.get("description") or ""),
            risk_level=risk,
            timeout_seconds=int(arguments.get("timeout_seconds") or 60),
            # 与 REST 路由同一条口径：显式存下实际端点，之后改 .env 不会
            # 让这条接入项悄悄改指向。令牌这里恒为空——它不从对话里来。
            server_url=(endpoint or settings.mcp_server_url or "").strip() or None,
            token=None,
            enabled=True,
            created_by=context.user_id,
        )
        session.add(row)
        await session.flush()

        tool_id = str(row.id)
        view = {
            "local_name": row.local_name,
            "remote_name": row.remote_name,
            "display_name": row.display_name,
            "risk_level": row.risk_level.value,
            "server_url": row.server_url,
        }
        await session.commit()
        # 同上：commit 后属性过期，同步注册函数里读属性会炸。
        await session.refresh(row)
        registered = register_mcp_row(gateway, row, descriptor)

    if not registered:
        return ToolExecutionResult(
            success=False,
            message=(
                f"MCP 工具「{view['display_name']}」已记录，但注册到网关失败。"
                "这条记录会保留，重启时会重试一次；也可以到「工具」页重新接入。"
            ),
            error_code="studio_tool_register_failed",
            result={"tool": view},
        )

    result = ToolExecutionResult(
        success=True,
        verified=True,
        result={"tool": view},
        message=(
            f"已接入 MCP 工具「{view['display_name']}」：本地名 {view['local_name']}，"
            f"远端名 {view['remote_name']}，端点 {view['server_url'] or '默认端点'}，"
            f"风险 {view['risk_level']}。"
        ),
        verification={"source": "tool_registry", "ref": view["local_name"]},
    )

    await _audit_admin(
        context,
        tool_name="studio_tool_save",
        event_type=AuditEventType.ADMIN_ACTION,
        resource_type="mcp_registered_tool",
        resource_id=tool_id,
        message=(
        f"构建助手接入 MCP 工具「{view['display_name']}」"
        f"（{view['local_name']} ← {view['remote_name']}）"
        ),
        metadata={"operation": "mcp_tool_registered", **view, "token_set": False},
    )
    return result


async def studio_tool_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """移除工具。`kind` 必填：`http` 与 `mcp` 是两张表，只看名字会删错一边。"""
    if not _is_admin(context):
        return _fail("自定义工具与 MCP 接入是平台级配置，只有平台管理员能改。")

    kind = str(arguments.get("kind") or "").strip()
    name = str(arguments.get("name") or "").strip()
    confirm = str(arguments.get("confirm_name") or "").strip()

    if kind not in {"http", "mcp", "python"}:
        return _fail("kind 必须是 http、mcp 或 python。")
    if kind == "python":
        return _fail("Python 工具由代码注册，下线方式是改代码与部署，这里删不掉。")
    if not name:
        return _fail("需要指出要移除的工具标识（name）。")
    if confirm != name:
        return _fail(f"移除需要再次确认名称：请让用户回复完整的标识「{name}」后再试一次。")

    from app.tools.dependencies import get_tool_gateway
    from app.tools.mcp_tools import code_local_names

    gateway = get_tool_gateway()

    if kind == "mcp" and name in code_local_names():
        return _fail(f"{name} 由代码注册，不能在运行时取消接入。")

    async with async_session_factory() as session:
        if kind == "http":
            row = await session.scalar(select(CustomTool).where(CustomTool.name == name))
            if row is None:
                return _fail(f"没有找到标识为「{name}」的自定义工具。")
            tool_id, display = str(row.id), row.display_name
            await session.delete(row)
        else:
            row = await session.scalar(
                select(McpRegisteredTool).where(McpRegisteredTool.local_name == name)
            )
            if row is None:
                return _fail(f"没有找到本地标识为「{name}」的 MCP 接入项。")
            tool_id, display = str(row.id), row.display_name
            await session.delete(row)

        await session.commit()
        unregister_and_verify(gateway, name, kind)

    result = ToolExecutionResult(
        success=True,
        verified=True,
        result={"deleted": name, "kind": kind, "display_name": display},
        message=f"已移除{view_kind(kind)}「{display}」（{name}）。",
        verification={"source": "tool_registry", "absent": name},
    )

    await _audit_admin(
        context,
        tool_name="studio_tool_delete",
        event_type=AuditEventType.ADMIN_ACTION,
        resource_type="custom_tool" if kind == "http" else "mcp_registered_tool",
        resource_id=tool_id,
        message=f"构建助手移除{view_kind(kind)}「{display}」（{name}）",
        metadata={"operation": f"{kind}_tool_deleted", "name": name},
    )
    return result


def view_kind(kind: str) -> str:
    return "自定义工具" if kind == "http" else "MCP 接入项"


def unregister_and_verify(gateway: Any, name: str, kind: str) -> None:
    """注销并确认真的从目录里消失了。

    注册表是进程内状态，「库里删了、目录里还在」会让模型继续看到一个调不动的
    工具——这类残留比彻底失败更难查，所以删完要看一眼。
    """
    unregister_row(gateway, name)
    still = [tool.name for tool in gateway.list_tools(include_disabled=True)]
    if name in still:
        logger.error(
            "工具已从库中删除，但仍留在网关目录里。",
            extra={"event": "studio_tool_unregister_unverified", "tool_name": name, "kind": kind},
        )


# ---------------- 知识库 ----------------


async def studio_knowledge_add(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """把一段正文写进知识库。

    只收正文，不收文件：助手手里没有文件流，硬造一个上传路径会把「谁传的、
    原文在哪」搞乱。落盘仍然写一份 .md，于是「按原文重切」这条路照旧可用。
    """
    blocked = _guard_credentials(arguments)
    if blocked:
        return _fail(blocked)

    title = str(arguments.get("title") or "").strip()
    content = str(arguments.get("content") or "").strip()
    scope = str(arguments.get("access_scope") or "internal").strip().lower()

    if not title:
        return _fail("需要给出一份文档的标题（title）。")
    if not content:
        return _fail("正文是空的，没有可入库的内容。")
    if scope not in VALID_SCOPES:
        return _fail(f"未知的分级「{scope}」，只能是 {'、'.join(sorted(VALID_SCOPES))}。")

    required = SCOPE_REQUIREMENTS[scope]
    if not context.is_platform_admin and required not in context.scopes:
        return _fail(f"写入 {scope} 级文档需要 {required} 权限。")

    raw = content.encode("utf-8")
    if len(raw) > settings.knowledge_max_file_bytes:
        limit_mb = settings.knowledge_max_file_bytes // (1024 * 1024)
        return _fail(f"正文超过 {limit_mb} MB 上限，请拆成多份。")

    safe_title = re.sub(r"[\\/\x00-\x1f]", "-", title)[:120]
    filename = f"{safe_title}.md"
    body_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    async with async_session_factory() as session:
        # 同一份正文重复写要幂等：换一个 logical_id 就是两份文档，
        # 检索时同一条资料出现两次，还会各占一份切片。
        existing = await documents.find_upload_by_hash(session, context.tenant_id, body_hash)
        logical_id = existing.logical_id if existing else f"upload-{uuid.uuid4().hex[:12]}"

        try:
            parsed = build_document(
                content,
                logical_id=logical_id,
                title=title,
                source_uri=f"uploads/{filename}",
                source_type=SOURCE_UPLOAD,
                filename=filename,
                size_bytes=len(raw),
                meta={"access_scope": scope, "visibility": "tenant_shared"},
                acl_hint=filename,
            )
        except KnowledgeIngestError as exc:
            return _fail(str(exc))

        with tenant_scope(context.tenant_id):
            action = await documents.upsert_document(
                session,
                context.tenant_id,
                parsed,
                actor=context.user_id,
            )
            document_id = str(store.document_uuid(context.tenant_id, logical_id))

            if action != "unchanged":
                documents.store_upload_file(
                    tenant_id=context.tenant_id,
                    document_id=document_id,
                    filename=filename,
                    raw=raw,
                )

            await store.bump_version(session, context.tenant_id)
            await session.flush()

            row = await documents.get_document(session, context.tenant_id, document_id)
            await session.commit()

    # 索引指纹是轮询的（几秒一次），不主动清会出现「刚写进去、检索不到」，
    # 用户只会以为写失败了。
    get_knowledge_index().invalidate(context.tenant_id)

    if row is None:
        return ToolExecutionResult(
            success=False,
            message="写入后未能读回文档，请到「知识库」页确认。",
            error_code="studio_knowledge_unverified",
        )

    chunks = row.chunk_count
    if action == "unchanged":
        message = f"《{row.title}》内容没有变化，已存在（{chunks} 片），没有重复入库。"
    else:
        # 与 `studio_import` 说同一件事：正文写进来的这份也能报出
        # 切成几节几片、打了哪些标。两条入库路共用一个 `_pipeline_lines`，
        # 否则「上传文件」和「贴正文」得到的回复详略不一样，看着像两个功能。
        message = (
            f"已入库《{row.title}》：{chunks} 片 · 分级 {row.access_scope}。"
            + "".join(_pipeline_lines({"pipeline": parsed.pipeline, "tags": list(parsed.tags)}))
        )

    result = ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "document": {
                "id": row.id,
                "title": row.title,
                "access_scope": row.access_scope,
                "chunk_count": chunks,
                "status": row.status,
            },
            "action": action,
            "pipeline": parsed.pipeline,
            "tags": list(parsed.tags),
        },
        message=message,
        # 与 studio_import 同一条：审批后的回读核验按这个 id 另起会话再查一遍。
        external_operation_id=row.id,
        verification={"source": "documents_table", "ref": row.id},
    )

    await _audit_admin(
        context,
        tool_name="studio_knowledge_add",
        event_type=AuditEventType.KNOWLEDGE_INGESTED,
        resource_type="knowledge_document",
        resource_id=row.id,
        message=f"构建助手入库文档《{row.title}》",
        metadata={
            "operation": "studio_knowledge_add",
            "title": row.title,
            "filename": row.filename,
            "access_scope": row.access_scope,
            "chunk_count": chunks,
            "action": action,
        },
    )
    return result


async def studio_knowledge_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """下架一份文档（软删除）。同样要求核对标题。"""
    document_id = str(arguments.get("document_id") or "").strip()
    confirm = str(arguments.get("confirm_title") or "").strip()

    if not document_id:
        return _fail("需要给出文档 id（先用 studio_describe resource=knowledge 查）。")

    async with async_session_factory() as session:
        row = await documents.get_document(session, context.tenant_id, document_id)
        if row is None:
            return _fail(f"没有找到 id 为「{document_id}」的文档。")

        if confirm != row.title:
            return _fail(
                f"下架需要核对标题：请让用户回复完整的标题「{row.title}」后再试一次。"
            )

        required = SCOPE_REQUIREMENTS.get(row.access_scope, "")
        if not context.is_platform_admin and required and required not in context.scopes:
            return _fail(f"下架 {row.access_scope} 级文档需要 {required} 权限。")

        with tenant_scope(context.tenant_id):
            removed = await documents.soft_delete(session, context.tenant_id, document_id)
            await session.commit()

    if not removed:
        return _fail("下架未生效，请稍后重试或到「知识库」页处理。")

    get_knowledge_index().invalidate(context.tenant_id)

    result = ToolExecutionResult(
        success=True,
        verified=True,
        result={"deleted": document_id, "title": row.title, "chunk_count": row.chunk_count},
        message=(
            f"已下架《{row.title}》，{row.chunk_count} 个切片不再参与检索。"
            "原文仍保留在磁盘上，需要彻底删除请到「知识库」页操作。"
        ),
        # 回读核验按它再查一遍：状态是不是 deleted、活跃切片是不是归零。
        external_operation_id=document_id,
        verification={"source": "documents_table", "ref": document_id},
    )

    await _audit_admin(
        context,
        tool_name="studio_knowledge_delete",
        event_type=AuditEventType.KNOWLEDGE_DELETED,
        resource_type="knowledge_document",
        resource_id=document_id,
        message=f"构建助手下架文档《{row.title}》",
        metadata={
            "operation": "studio_knowledge_delete",
            "title": row.title,
            "access_scope": row.access_scope,
        },
    )
    return result


# ---------------- 统一导入（对话里完成） ----------------

# 与 `app/studio/parsers.py` 的目标枚举一致。这里不 import 它，
# 是为了让「参数取值」在 schema 里能读出来——真正的校验仍在 `run_import`。
IMPORT_TARGETS = ("auto", "knowledge", "skill", "tool")

_IMPORT_LABEL = {
    "auto": "自动识别",
    "knowledge": "知识库文档",
    "skill": "技能",
    "tool": "工具接口",
}


async def _attachment_filename(
    *,
    context: ToolExecutionContext,
    attachment_id: str,
) -> str | None:
    """从消息元数据里取回上传时的原始文件名。

    磁盘上只按句柄存（`txt_xxx.txt`），原始名字留在 `messages.metadata_json`
    里。判类**必须**用原始后缀：`.json` 与 `.md` 落盘后都可能是 `.txt`，
    拿落盘名去判会把 OpenAPI 文档认成知识库文档——而错误发生在「已经入库」
    之后，只能靠人发现库里多了一份不像资料的资料。

    找不到就返回 None，由调用方决定回退方式，不在这里猜。
    """
    if not context.conversation_id:
        return None

    try:
        conversation_id = uuid.UUID(str(context.conversation_id))
    except ValueError:
        # 构建助手的 thread_id 是业务串（`studio-20261117-…`），不是会话主键；
        # 取不到 UUID 就说明这轮没有可查的会话行，直接按「没有文件名」处理。
        return None

    async with async_session_factory() as session:
        with tenant_scope(context.tenant_id):
            rows = await session.scalars(
                select(Message.metadata_json)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == MessageRole.USER,
                )
                .order_by(Message.created_at.desc())
                .limit(50)
            )

            for metadata in rows.all():
                for item in (metadata or {}).get("attachments") or []:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("attachment_id") or "") != attachment_id:
                        continue
                    name = str(item.get("name") or "").strip()
                    if name:
                        return name

    return None


def _import_identity(context: ToolExecutionContext) -> Identity:
    """把工具上下文还原成 `run_import` 要的身份。

    `subject` 用 `user_id`：工具侧拿不到网关主体名，而它只进审计的
    `actor_subject` 字段；比起空串，用户 id 至少能定位到人。
    """
    return Identity(
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        subject=context.user_id,
        display_name="",
        roles=context.roles,
        scopes=context.scopes,
        is_platform_admin=context.is_platform_admin,
    )


async def studio_import(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """把会话里已上传的一份文件按用途建成知识库文档 / 技能 / 工具。

    为什么走「附件句柄」而不是让模型把文件内容传进来：内容在读取工具里
    已经被截断过（4 万字符），拿截断后的正文入库会得到一份**看着完整、
    实际少了一半**的资料——这比直接失败难查得多。句柄指向磁盘上的原文，
    切片按整份做，与页面上传完全同一条路径。

    判类与写入全部转手 `app.studio.importer.run_import`：那份实现同时被
    REST 接口使用，两边各写一套必然出现「对话里能导、页面上导不了」。
    """
    # 局部导入：importer → tools.dependencies → 本模块，模块级会成环。
    from app.studio import importer

    blocked = _guard_credentials(arguments)
    if blocked:
        return _fail(blocked)

    file_id = str(arguments.get("file_id") or "").strip()
    target = str(arguments.get("target") or "auto").strip().lower()

    if not file_id:
        return _fail("需要给出附件句柄（file_id），取自消息里 [附件] 清单的值。")
    if target not in IMPORT_TARGETS:
        return _fail(f"未知的导入目标「{target}」，只能是 {'、'.join(IMPORT_TARGETS)}。")
    if not context.conversation_id:
        return _fail("当前没有会话上下文，无法定位这份附件。")

    try:
        path, mime = attachment_store.resolve_attachment(
            tenant_id=context.tenant_id,
            conversation_id=context.conversation_id,
            attachment_id=file_id,
        )
    except attachment_store.AttachmentError as exc:
        return _fail(str(exc))

    # 图片没有「原文切片」这回事：送进知识库只能靠 OCR 出一段文本，
    # 与页面上传（直接解析 PDF/Word/Markdown）不是同一条路。
    if attachment_store.kind_of(mime) == "image":
        return _fail(
            "图片不能作为知识库文档或技能导入。"
            "要看图里的文字请用 ocr_document 读出来再处理；"
            "要入库请上传 PDF、Word 或文本文件。"
        )

    raw = path.read_bytes()
    if not raw:
        return _fail("这份附件是空的。")

    filename = str(arguments.get("filename") or "").strip() or await _attachment_filename(
        context=context, attachment_id=file_id
    )
    guessed = False
    if not filename:
        # 原始文件名丢了（客户端没给 name 的旧记录）。按 MIME 兜底并明说：
        # `.json`/`.yaml` 会退成 `.txt`，此时只有 target 显式指定才判得对。
        filename = f"attachment{attachment_store.suffix_for(mime)}"
        guessed = True

    options: dict[str, Any] = {}
    for key in ("title", "access_scope", "name", "base_url"):
        value = str(arguments.get(key) or "").strip()
        if value:
            options[key] = value
    mode = str(arguments.get("mode") or "").strip().lower()
    if mode in {"merge", "overwrite"}:
        options["mode"] = mode

    try:
        with tenant_scope(context.tenant_id):
            async with async_session_factory() as session:
                payload = await importer.run_import(
                    session=session,
                    identity=_import_identity(context),
                    filename=filename,
                    raw=raw,
                    target=target,
                    options=options,
                )
    except importer.ImportDenied as exc:
        return _fail(str(exc))
    except (importer.ImportRejected, importer.StudioParseError) as exc:
        return _fail(str(exc))
    except RuntimeError as exc:
        # 「写完之后读不回来」这类：不是换参数能修的，别让模型反复重试。
        return _fail(f"写入后未能确认结果：{exc}")

    resolved = str(payload.get("target") or target)
    detected = str(payload.get("detected_target") or resolved)
    result = payload.get("result") or {}
    notes = [str(note) for note in (payload.get("notes") or []) if note]

    message = _import_message(
        label=_IMPORT_LABEL.get(resolved, resolved),
        filename=filename,
        detected=detected,
        resolved=resolved,
        result=result,
        notes=notes,
        guessed=guessed,
    )

    await _audit_admin(
        context,
        tool_name="studio_import",
        event_type=AuditEventType.ADMIN_ACTION,
        resource_type={
            "knowledge": "knowledge_document",
            "skill": "skill",
            "tool": "custom_tool",
        }.get(resolved, "studio_import"),
        resource_id=(str(result.get("document_id") or "") or filename)[:200],
        message=f"构建助手从附件导入：{filename}",
        metadata={
            "operation": "studio_import",
            "file_id": file_id,
            "filename": filename,
            "target": resolved,
            "detected_target": detected,
            "result": result,
        },
    )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result=payload,
        message=message,
        # 回读核验要认的是「哪一份」，把 id 递出去，verifier 才有落点。
        # 技能与工具导入没有这一项：它们不落 documents 表，也就没有可回读的对象。
        external_operation_id=str(result.get("document_id") or "") or None,
        verification={"source": "studio_import", "target": resolved},
    )


async def _readback_document(
    context: ToolExecutionContext,
    document_id: str,
) -> documents.DocumentRow | None:
    """另起一个会话读一行文档，给回读核验用。

    必须是新会话、新连接：与写入同一个事务里的读回证明不了提交成功。
    """
    async with async_session_factory() as session:
        with tenant_scope(context.tenant_id):
            return await documents.get_document(session, context.tenant_id, document_id)


async def verify_ingested_document(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """入库回读：另起一个会话按 id 再查一遍。

    为什么不能拿工具自己的返回值作答：那是「我刚写完」的自述，而
    `ingest_upload` 里那次 `get_document` 与写入在同一个事务里，即使提交
    失败也照样读得到。换一个连接重新查，才有资格在审批卡上标 VERIFIED。

    能核到的东西是具体的：文档在、状态 ready、切片行数 > 0。三条任一不成立
    都算没核验——「接口返回成功」不是结论。

    只覆盖落进知识库的导入。技能与工具不在这张表里，没有可回读的落点，
    此时如实说未核验，不拿「接口返回成功」冒充核验。
    """
    document_id = str(context.external_operation_id or "").strip()

    if not document_id:
        return ToolExecutionResult(
            success=True,
            verified=False,
            message="这次导入没有落成知识库文档，没有可回读的落点。",
            verification={"verified": False, "reason": "no_document"},
        )

    row = await _readback_document(context, document_id)

    if row is None:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message="回读不到这次导入的文档，不能视为已入库。",
            error_code="VERIFY_DOCUMENT_MISSING",
            verification={"verified": False, "reason": "missing", "ref": document_id},
        )

    if row.deleted or row.status != DocumentStatus.READY.value:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message=f"文档已写入但状态是 {row.status}，不是可检索的 ready。",
            error_code="VERIFY_DOCUMENT_NOT_READY",
            verification={"verified": False, "reason": row.status, "ref": row.id},
        )

    if row.chunk_count <= 0:
        # 「文档建好了、一片没切」在列表上看着完全正常，检索时却什么都不出。
        return ToolExecutionResult(
            success=False,
            verified=False,
            message="文档在库里，但一片内容都没有，检索不到。",
            error_code="VERIFY_DOCUMENT_EMPTY",
            verification={"verified": False, "reason": "no_chunks", "ref": row.id},
        )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "document_id": row.id,
            "title": row.title,
            "status": row.status,
            "chunk_count": row.chunk_count,
            "access_scope": row.access_scope,
        },
        message=f"已回读确认《{row.title}》在库：{row.chunk_count} 片 · 分级 {row.access_scope}。",
        verification={
            "verified": True,
            "source": "documents_table",
            "ref": row.id,
            "chunks": row.chunk_count,
        },
    )


async def verify_document_retired(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """下架回读：另起一个会话确认这份文档真的不再参与检索。

    下架的承诺是「N 个切片不再参与检索」，所以核验要核两件事：状态是
    deleted，**并且活跃切片归零**。只看状态会漏掉一种失败——文档标了下架、
    切片却还是 active，列表上看着没了，检索照样命中，而这正是最难发现的那种。

    不拿工具返回值作答的理由与入库那条相同：写和读在同一个事务里。
    """
    document_id = str(context.external_operation_id or "").strip() or str(
        arguments.get("document_id") or ""
    ).strip()

    if not document_id:
        return ToolExecutionResult(
            success=True,
            verified=False,
            message="没有可回读的文档标识。",
            verification={"verified": False, "reason": "no_document"},
        )

    row = await _readback_document(context, document_id)

    if row is None:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message="回读不到这份文档，无法确认它已下架。",
            error_code="VERIFY_DOCUMENT_MISSING",
            verification={"verified": False, "reason": "missing", "ref": document_id},
        )

    if not row.deleted:
        return ToolExecutionResult(
            success=False,
            verified=False,
            message=f"文档状态仍是 {row.status}，没有下架成功。",
            error_code="VERIFY_DOCUMENT_NOT_RETIRED",
            verification={"verified": False, "reason": row.status, "ref": row.id},
        )

    if row.chunk_count > 0:
        # 标了下架、切片还活着：列表里没了、检索仍命中，比「没下架」更难发现。
        return ToolExecutionResult(
            success=False,
            verified=False,
            message=f"文档已标记下架，但仍有 {row.chunk_count} 个切片可被检索。",
            error_code="VERIFY_CHUNKS_STILL_ACTIVE",
            verification={
                "verified": False,
                "reason": "chunks_active",
                "ref": row.id,
                "chunks": row.chunk_count,
            },
        )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={"document_id": row.id, "title": row.title, "status": row.status},
        message=f"已回读确认《{row.title}》已下架，没有切片参与检索。",
        verification={
            "verified": True,
            "source": "documents_table",
            "ref": row.id,
            "chunks": 0,
        },
    )


def _import_message(
    *,
    label: str,
    filename: str,
    detected: str,
    resolved: str,
    result: dict[str, Any],
    notes: list[str],
    guessed: bool,
) -> str:
    """把三类导入的结果说成同一句话的形状。

    必须把「识别成什么」与「实际写成什么」分开写：人改了目标之后，
    仍然需要知道机器原本判的是什么——否则下次同样的文件还会判错，
    而没人知道那是判类问题。
    """
    action = str(result.get("action") or "")
    detail = str(result.get("message") or "")

    if resolved == "knowledge":
        head = (
            f"已导入知识库《{result.get('title') or filename}》："
            f"{result.get('chunk_count') or 0} 片 · 分级 {result.get('access_scope') or '-'}。"
            if action != "unchanged"
            else f"《{result.get('title') or filename}》内容没有变化，已在库里，没有重复入库。"
        )
    elif resolved == "skill":
        head = f"已按技能导入：{detail or action}。"
    else:
        head = f"已按工具接口导入：{detail or action}。"

    parts = [head]
    pipeline_lines = _pipeline_lines(result)
    if pipeline_lines:
        parts.extend(pipeline_lines)
    else:
        # 没有快照（老行、技能/工具、或入库前就失败了）时才落到这条老文案：
        # 它只说得出清洗这一环。有快照时由 `_pipeline_lines` 统一说，
        # 同一件事不说两遍。
        cleanup = result.get("cleanup") or {}
        if cleanup.get("chars_before"):
            # 「入库的字数」和「你传的文件字数」差一大截时必须说出来，否则
            # 人会以为后半本没进去（或者以为自己传错了）。差额全部是 HTML 标签，
            # 一个字的内容都没丢——这句就是用来打消那个误会的。
            parts.append(
                f"这份文件带 {cleanup.get('tables') or 0} 张 HTML 表格，"
                f"已转成 Markdown 表格入库：原文 {int(cleanup['chars_before']):,} 字，"
                f"清洗后 {int(cleanup.get('chars_after') or 0):,} 字。"
                "差额是标签与样式属性，正文没有删减。"
            )
    if detected != resolved:
        parts.append(
            f"自动识别原本判成「{_IMPORT_LABEL.get(detected, detected)}」，"
            f"按要求的「{label}」处理了。"
        )
    if guessed:
        parts.append(
            "这份附件没有留下原始文件名，判类按 MIME 兜底；"
            "如果结果不该是这个类型，请显式指定 target 再来一次。"
        )
    if notes:
        parts.append("解析提示：" + "；".join(notes[:5]))
    return "".join(parts)


# 标签一行最多列这么多，再多就说「还有几个」——库里最长的一份带了十几个
# 主题词，全铺出来会把结论挤到看不见的地方。
_PIPELINE_TAG_LIMIT = 8


def _pipeline_lines(result: dict[str, Any]) -> list[str]:
    """把入库快照说成一条「加工链路」，外加一行标签。

    为什么要说：入库是一个调用栈里跑完的五步，用户此前在对话里只拿得到
    「N 片 · 分级」——于是「我传了 56 万字，为什么只有 14 万字」「448 片是不是
    切碎了」这些问题只能靠人再跳一次控制台。快照本来已经写进库里（治理页读它），
    这里只是把同一份事实说回给提问的人，不另算一遍、也不做估算。

    没有快照就返回空列表，由调用方回落到老文案：宁可少说一句，
    也不能拿「0 片」「0 张表」这种数字冒充事实——那和「确实是 0」是两回事。
    """
    snapshot = result.get("pipeline") or {}
    if not snapshot:
        return []

    steps: list[str] = []

    parse = snapshot.get("parse") or {}
    if parse:
        chars = int(parse.get("chars") or 0)
        extractor = str(parse.get("extractor") or "").strip()
        # extractor 是后缀名（pdf / md / docx）；认不出来时只说「解析」。
        named = extractor if extractor and extractor != "unknown" else ""
        head = f"解析 {named}" if named else "解析"
        steps.append(f"{head} {chars:,} 字" if chars else head)

    clean = snapshot.get("clean") or {}
    if clean:
        before = int(clean.get("chars_before") or 0)
        after = int(clean.get("chars_after") or 0)
        tables = int(clean.get("tables") or 0)
        # 差额这句不能省：字数掉一大截时，人的第一反应是「后半本没进去」，
        # 「压掉的都是标签和样式，正文没删」才是真正要传达的结论。
        delta = (
            f"（{before:,} → {after:,} 字，差额是标签与样式，正文未删减）"
            if before or after
            else ""
        )
        if tables:
            steps.append(f"清洗 {tables} 张表{delta}")
        elif delta:
            steps.append(f"清洗{delta}")

    structure = snapshot.get("structure") or {}
    sections = int(structure.get("sections") or 0)
    refs = int(structure.get("section_refs") or 0)
    if sections:
        steps.append(f"结构化 {sections} 节")
    elif refs:
        # 没有 Markdown 标题层级的老扫描件走这条：按段落切，不是按章节切。
        steps.append(f"无标题层级（按 {refs} 段切分）")

    chunk = snapshot.get("chunk") or {}
    chunks = int(chunk.get("chunks") or 0)
    if chunks:
        average = int(chunk.get("chars") or 0) // max(chunks, 1)
        steps.append(f"切片 {chunks} 片（平均 {average} 字）")

    tags = [str(tag).strip() for tag in (result.get("tags") or []) if str(tag).strip()]
    steps.append(f"打标 {len(tags)} 项" if tags else "未识别出标签")
    steps.append("索引已刷新")

    lines = ["加工链路：" + " → ".join(steps) + "。"]
    if tags:
        shown = "、".join(tags[:_PIPELINE_TAG_LIMIT])
        # 截断要说清是截断：只补一个省略号，看着像抽词抽到一半断了。
        suffix = f"…（共 {len(tags)} 项）" if len(tags) > _PIPELINE_TAG_LIMIT else ""
        lines.append(f"标签：{shown}{suffix}。")
    return lines


# ---------------- 共用 ----------------


async def _audit_admin(
    context: ToolExecutionContext,
    *,
    tool_name: str,
    event_type: AuditEventType,
    resource_type: str,
    resource_id: str,
    message: str,
    metadata: dict[str, Any],
) -> None:
    """写一条资源级审计，自己开会话、自己带租户上下文。

     **为什么不让调用方传 session。** `audit_logs` 上有行级安全策略，
    插入要求事务里先设好 `app.tenant_id`。工具表（自定义工具 / MCP 接入项）
    是平台级表、本身不需要租户作用域，于是「顺手复用调用方的会话」
    在 SQLite 单测里完全正常，到 PostgreSQL 上就直接
    `new row violates row-level security policy`——而异常还被吞成一行日志，
    表现成「审计页上什么都没有，代码却以为记了」。线上冒烟就是这么踩到的。
    """
    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            await _audit_mutation(
                session,
                context,
                tool_name=tool_name,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                message=message,
                metadata=metadata,
            )


async def _preflight_credentials(context: ToolExecutionContext, arguments: dict[str, Any]) -> str | None:
    blocked = _guard_credentials(arguments)
    if blocked:
        return blocked
    return None


async def _preflight_import(
    context: ToolExecutionContext, arguments: dict[str, Any]
) -> str | None:
    """按目标判权限，别让人对着一张注定失败的审批卡点确认。

    `target=auto` 时判「至少有一类导入权限」：具体落到哪一类由 `run_import`
    决定，它还会按知识库分级再判一次（`internal` 之外要更高的 scope）。
    """
    blocked = _guard_credentials(arguments)
    if blocked:
        return blocked

    if context.is_platform_admin:
        return None

    target = str(arguments.get("target") or "auto").strip().lower()
    scopes = set(context.scopes)

    if target == "tool":
        return "导入工具接口是平台级操作，需要平台管理员权限。"
    if target == "skill":
        if SKILL_PERMISSION in scopes:
            return None
        return f"导入技能需要 {SKILL_PERMISSION} 权限。"
    if target == "knowledge":
        if KNOWLEDGE_PERMISSION in scopes:
            return None
        return f"导入知识库文档需要 {KNOWLEDGE_PERMISSION} 权限。"

    if scopes & {KNOWLEDGE_PERMISSION, SKILL_PERMISSION}:
        return None
    return (
        f"从文件导入需要 {KNOWLEDGE_PERMISSION} 或 {SKILL_PERMISSION} 权限"
        "（导入工具接口需要平台管理员）。"
    )


async def _preflight_tool(context: ToolExecutionContext, arguments: dict[str, Any]) -> str | None:
    blocked = _guard_credentials(arguments)
    if blocked:
        return blocked

    if not _is_admin(context):
        return "自定义工具与 MCP 接入是平台级配置，只有平台管理员能改。"
    return None


async def _preflight_tool_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> str | None:
    reason = await _preflight_tool(context, arguments)
    if reason:
        return reason

    kind = str(arguments.get("kind") or "").strip()
    name = str(arguments.get("name") or "").strip()
    if kind == "python":
        return "Python 工具由代码注册，下线方式是改代码与部署，这里删不掉。"
    if kind not in {"http", "mcp"} or not name:
        return None

    async with async_session_factory() as session:
        if kind == "http":
            row = await session.scalar(select(CustomTool).where(CustomTool.name == name))
        else:
            row = await session.scalar(
                select(McpRegisteredTool).where(McpRegisteredTool.local_name == name)
            )

    if row is None:
        return f"没有找到要移除的{view_kind(kind)}「{name}」，可能已经删过了。"
    return None


# ---------------- 连接器 ----------------


async def studio_connector_save(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """新建或修改连接器**定义**。

    **不接受凭据参数，也不替用户保存凭据。** 连接器凭据只能经控制台的
    安全输入框写入：那条路径不进对话、不进上下文、不进审批卡。
    如果有人在这里贴了一个 token，`_preflight_credentials` 会先拦住，
    而返回的文案会明确要求它去轮换。
    """
    if not context.is_platform_admin:
        return _fail("连接器是平台级配置，只有平台管理员能改。")

    from app.models.connector import ConnectorAuthType, ConnectorKind
    from app.services import connector_service

    mode = str(arguments.get("mode") or "create").strip()
    name = str(arguments.get("name") or "").strip()

    if mode not in ("create", "update"):
        return _fail("mode 只能是 create 或 update。")

    if not name:
        return _fail("需要给出连接器标识（小写字母、数字、下划线）。")

    kind_text = str(arguments.get("kind") or "mcp").strip()
    auth_text = str(arguments.get("auth_type") or "none").strip()

    try:
        kind = ConnectorKind(kind_text)
        auth_type = ConnectorAuthType(auth_text)
    except ValueError:
        return _fail("kind 只能是 http 或 mcp；auth_type 只能是 none/bearer/header/basic。")

    async with async_session_factory() as session:
        with tenant_scope(context.tenant_id):
            try:
                existing = await connector_service.get_connector(session, name)

                if mode == "create":
                    if existing is not None:
                        return _fail(f"连接器 {name} 已存在。要改它请用 mode=update。")

                    row = await connector_service.create_connector(
                        session,
                        name=name,
                        display_name=str(arguments.get("display_name") or ""),
                        description=str(arguments.get("description") or ""),
                        kind=kind,
                        base_url=str(arguments.get("base_url") or ""),
                        auth_type=auth_type,
                        auth_header_name=str(arguments.get("auth_header_name") or ""),
                        timeout_seconds=int(arguments.get("timeout_seconds") or 30),
                        actor_id=context.user_id,
                    )
                    before: dict[str, Any] = {}
                    after: dict[str, Any] = {"name": row.name, "kind": row.kind.value}
                else:
                    if existing is None:
                        return _fail(f"没有找到连接器 {name}。")

                    payload: dict[str, Any] = {}
                    if "display_name" in arguments:
                        payload["display_name"] = arguments["display_name"]
                    if "description" in arguments:
                        payload["description"] = arguments["description"]
                    if "kind" in arguments:
                        payload["kind"] = kind.value
                    if "base_url" in arguments:
                        payload["base_url"] = arguments["base_url"]
                    if "auth_type" in arguments:
                        payload["auth_type"] = auth_type.value
                    if "auth_header_name" in arguments:
                        payload["auth_header_name"] = arguments["auth_header_name"]
                    if "timeout_seconds" in arguments:
                        payload["timeout_seconds"] = arguments["timeout_seconds"]
                    if "enabled" in arguments:
                        payload["enabled"] = bool(arguments["enabled"])

                    row, before, after = await connector_service.update_connector(
                        session,
                        connector=existing,
                        payload=payload,
                        actor_id=context.user_id,
                    )
            except connector_service.ConnectorConfigError as exc:
                return _fail(str(exc))

            await session.commit()

            snapshot = {
                "name": row.name,
                "kind": row.kind.value,
                "base_url": row.base_url,
                "auth_type": row.auth_type.value,
                "enabled": bool(row.enabled),
            }

            # 改完要把下属工具重注册一次：否则会出现「页面显示新地址、
            # 模型还在调旧地址」，而那只有等到调用失败时才显形。
            if mode == "update" and before:
                from app.tools.dependencies import get_tool_gateway

                await connector_service.sync_connector_tools(
                    session, get_tool_gateway(), connector=row
                )

    await _audit_admin(
        context,
        tool_name="studio_connector_save",
        event_type=(
            AuditEventType.CONNECTOR_CREATED
            if mode == "create"
            else AuditEventType.CONNECTOR_UPDATED
        ),
        resource_type="connector",
        resource_id=str(row.id),
        message=f"{'新建' if mode == 'create' else '修改'}连接器 {row.name}",
        metadata={
            "via": "studio_assistant",
            "approval_id": context.approval_id,
            "before": before,
            "after": after,
        },
    )

    needs_credential = row.auth_type.value != "none"

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={"connector": snapshot, "changed": sorted(after) if mode == "update" else None},
        message=(
            f"连接器 {row.name} 已保存。"
            + (
                "它需要凭据，请让用户到控制台「连接器」页填写——"
                "凭据不要发在对话里，我也不会替你保存。"
                if needs_credential
                else "这个连接器不需要凭据，配好工具后即可调用。"
            )
        ),
    )


async def studio_connector_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """删除连接器。要求逐字核对标识，且下面不能还挂着工具。"""
    if not context.is_platform_admin:
        return _fail("连接器是平台级配置，只有平台管理员能删。")

    from app.services import connector_service

    name = str(arguments.get("name") or "").strip()
    confirm = str(arguments.get("confirm_name") or "").strip()

    if not name:
        return _fail("需要给出连接器标识。")

    if confirm != name:
        return _fail(f"删除需要核对标识：请让用户回复完整的「{name}」后再试一次。")

    async with async_session_factory() as session:
        with tenant_scope(context.tenant_id):
            row = await connector_service.get_connector(session, name)

            if row is None:
                return _fail(f"没有找到连接器 {name}。")

            try:
                await connector_service.delete_connector_guard(session, connector=row)
            except connector_service.ConnectorConfigError as exc:
                return _fail(
                    f"{exc}取消接入不会影响远端服务，只是让模型看不见这些工具。"
                )

            connector_id = row.id
            await session.delete(row)
            await session.commit()

    connector_service.invalidate_runtime(connector_id=connector_id)

    await _audit_admin(
        context,
        tool_name="studio_connector_delete",
        event_type=AuditEventType.CONNECTOR_DELETED,
        resource_type="connector",
        resource_id=str(connector_id),
        message=f"删除连接器 {name}",
        metadata={"via": "studio_assistant", "approval_id": context.approval_id},
    )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={"deleted": name},
        message=f"连接器 {name} 已删除。远端服务不受影响。",
    )


# ---------------- 编排流程 ----------------

# 图的 DSL 摘要写进工具的 description，**不**另开一个「怎么画图」的读工具：
# 模型对工具说明的服从度高于系统提示词，而它只在真要写图时才需要这段。


def _workflow_shape(row: Any) -> tuple[int, int]:
    """(节点数, 跳数)。取不到（坏图）时返回 (0, 0)。"""
    from app.services import workflow_service

    plan = workflow_service.plan_of(row)
    if plan is None:
        return 0, 0
    return plan.size, plan.depth_hops


async def studio_workflow_save(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """新建或修改一张编排流程。

    `definition` 原样交给 `workflow_service.save_workflow`：编译校验只有那一份
    （`compile_definition`）。在这里再写一遍「有没有兜底边」「条件算子认不认识」，
    迟早长成「控制台存得进去、助手存不进去」——而两边看到的都是「保存失败」。

    `mode` 不做「存在就改」的自动判断（与技能、智能体同一条理由）：名字错一个
    字符就静默改掉另一张图，审批卡上还写着「新建」。
    """
    mode = str(arguments.get("mode") or "").strip()
    if mode not in {"create", "update"}:
        return _fail("mode 必须是 create 或 update。")

    name = str(arguments.get("name") or "").strip()
    if not name:
        return _fail("需要指出流程标识（name）——它就是对话里「/workflow」后面那个词。")

    from app.services import workflow_service

    existing = await workflow_service.get_workflow(context.tenant_id, name)

    if mode == "create" and existing is not None:
        return _fail(f"流程「{name}」已经存在，要改它请用 mode=update。")
    if mode == "update" and existing is None:
        return _fail(f"没有找到标识为「{name}」的流程，要新建请用 mode=create。")

    definition = arguments.get("definition")
    if definition is None:
        if existing is None:
            return _fail(
                "新建流程必须给出 definition（整张图）。没有图就没有可跑的流程——"
                "先用 studio_describe resource=platform 看图的写法。"
            )
        # 不传图 = 只改名字、说明或启停。传空对象才是「把图清空」，那由编译拒绝。
        definition = existing.definition or {}
    elif not isinstance(definition, dict):
        return _fail('definition 必须是一个对象：{"entry": "起点id", "nodes": [...]}。')

    display_name = str(
        arguments.get("display_name") or (existing.display_name if existing else name)
    )
    description = arguments.get("description")
    if description is None and existing is not None:
        description = existing.description
    if "enabled" in arguments:
        enabled = bool(arguments["enabled"])
    else:
        enabled = bool(existing.enabled) if existing is not None else True

    try:
        row = await workflow_service.save_workflow(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            name=name,
            display_name=display_name,
            description=description,
            definition=definition,
            enabled=enabled,
        )
    except workflow_service.WorkflowError as exc:
        where = f"节点「{exc.node}」" if exc.node else "整张图"
        return _fail(
            f"{where}不成立（{exc.code}）：{exc.message}"
            "改好这一处再保存——存进库的必须是能跑的图。"
        )

    node_count, depth_hops = _workflow_shape(row)
    created = existing is None
    action = "新建" if created else "更新"

    result = ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "workflow": {
                "name": str(row.name),
                "display_name": str(row.display_name),
                "revision": int(row.revision or 0),
                "enabled": bool(row.enabled),
                "node_count": node_count,
                "depth_hops": depth_hops,
            }
        },
        message=(
            f"已{action}流程「{row.display_name}」（{row.name}）：{node_count} 个节点 / "
            f"{depth_hops} 跳 · {'启用' if row.enabled else '停用'} · v{row.revision}。"
            f"触发方式：对话里发「/workflow {row.name} 要做的事」，"
            "或在对话窗口的下拉里选这张流程。"
        ),
        verification={"source": "workflows_table", "ref": str(row.name)},
    )

    await _audit_admin(
        context,
        tool_name="studio_workflow_save",
        event_type=AuditEventType.ADMIN_ACTION,
        resource_type="workflow",
        resource_id=str(row.id),
        message=f"构建助手{action}流程「{row.display_name}」（{row.name}）v{row.revision}",
        metadata={
            "operation": "workflow_created" if created else "workflow_updated",
            "name": str(row.name),
            "revision": int(row.revision or 0),
            "node_count": node_count,
            "depth_hops": depth_hops,
            "enabled": bool(row.enabled),
        },
    )
    return result


async def studio_workflow_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """删除一张流程。要求逐字核对标识。

    **不拦「还有未完结的运行」。** 每条 run 都在行上冻结了一份图
    （`WorkflowRun.definition`），删掉配置不影响它按原图走完或继续等审批；
    拦下来只会让一次正常的清理永远做不完（一张没人管的暂停单能顶住整年）。
    但必须在结果里说清楚还有几条在跑——「删了之后它们还算数吗」是紧接着会问的。
    """
    name = str(arguments.get("name") or "").strip()
    confirm = str(arguments.get("confirm_name") or "").strip()

    if not name:
        return _fail("需要给出流程标识（name）。")
    if confirm != name:
        return _fail(f"删除需要核对标识：请让用户回复完整的「{name}」后再试一次。")

    from app.services import workflow_service

    row = await workflow_service.get_workflow(context.tenant_id, name)
    if row is None:
        return _fail(f"没有找到标识为「{name}」的流程。")

    workflow_id = str(row.id)
    display = str(row.display_name)

    runs = await workflow_service.recent_runs(
        tenant_id=context.tenant_id, workflow_name=name, limit=50
    )
    open_runs = [
        run
        for run in runs
        if run.status in (WorkflowRunStatus.RUNNING, WorkflowRunStatus.PAUSED)
    ]

    deleted = await workflow_service.delete_workflow(
        tenant_id=context.tenant_id, name=name
    )
    if not deleted:
        return _fail(f"删除没有生效：库里已经没有流程「{name}」了。")

    message = f"流程「{display}」（{name}）已删除，历史运行记录保留。"
    if open_runs:
        message += (
            f"另有 {len(open_runs)} 条未完结的运行（在跑或在等审批），"
            "它们按删除前冻结的图继续，不受影响。"
        )

    await _audit_admin(
        context,
        tool_name="studio_workflow_delete",
        event_type=AuditEventType.ADMIN_ACTION,
        resource_type="workflow",
        resource_id=workflow_id,
        message=f"构建助手删除流程「{display}」（{name}）",
        metadata={
            "operation": "workflow_deleted",
            "name": name,
            "open_runs": len(open_runs),
        },
    )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={"deleted": name, "open_runs": len(open_runs)},
        message=message,
    )


def build_studio_admin_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="studio_skill_save",
            display_name="新建或修改技能",
            description=(
                "新建或修改技能。mode=create 新建，mode=update 只改传入的字段。"
                "技能 = 一段可复用的作业指令 + 触发词 + 可选的工具白名单；"
                "它不授予权限，allowed_tools 只会在人已有权限里收窄。"
                "不要在这里放密钥，本工具会直接拒绝凭据类字段。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(SKILL_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["create", "update"]},
                    "name": {"type": "string", "minLength": 2, "maxLength": 64},
                    "display_name": {"type": "string", "maxLength": 64},
                    "description": {"type": "string", "maxLength": 256},
                    "instruction": {"type": "string", "maxLength": 8000},
                    "trigger_keywords": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 32},
                        "maxItems": 12,
                    },
                    "allowed_tools": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "maxItems": 64,
                    },
                    "priority": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "enabled": {"type": "boolean"},
                },
                "required": ["mode", "name"],
                "additionalProperties": False,
            },
            handler=studio_skill_save,
            preflight=_preflight_credentials,
            verification_hint="保存后回读技能，给出启用状态与工具数量。",
        ),
        ToolDefinition(
            name="studio_skill_delete",
            display_name="删除技能",
            description=(
                "删除技能。需要 confirm_name 与技能标识逐字一致；"
                "仍被智能体边界引用的技能不会阻止删除，但会在结果里点名是哪些。"
            ),
            kind="python",
            risk_level=RiskLevel.DESTRUCTIVE,
            required_scopes=(SKILL_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 2, "maxLength": 64},
                    "confirm_name": {"type": "string", "minLength": 2, "maxLength": 64},
                },
                "required": ["name", "confirm_name"],
                "additionalProperties": False,
            },
            handler=studio_skill_delete,
            verification_hint="删除后回读，确认已不存在。",
        ),
        ToolDefinition(
            name="studio_tool_save",
            display_name="新建或修改工具",
            description=(
                "管理工具。kind=http 新建/修改自定义 HTTP 工具；"
                "kind=mcp 把远端 MCP 工具接入白名单（只能新增，不能改）；"
                "kind=python 是代码注册的内置工具，只读，会被直接拒绝。"
                "**不要传任何凭据**：token / api_key / 密码一律拒绝，"
                "鉴权请到控制台配置，或使用 credential_ref 引用。"
                "需要平台管理员身份。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(TOOL_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["http", "mcp", "python"]},
                    "mode": {"type": "string", "enum": ["create", "update"]},
                    "name": {"type": "string", "minLength": 2, "maxLength": 64},
                    "display_name": {"type": "string", "maxLength": 128},
                    "description": {"type": "string", "maxLength": 4000},
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    },
                    "url": {"type": "string", "maxLength": 1024},
                    "risk_level": {
                        "type": "string",
                        "enum": ["read", "normal", "write", "execute", "destructive", "external"],
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                    "params": {"type": "array", "items": {"type": "object"}, "maxItems": 32},
                    "headers": {"type": "object"},
                    "enabled": {"type": "boolean"},
                    "remote_name": {"type": "string", "maxLength": 128},
                    "server_url": {"type": "string", "maxLength": 512},
                },
                "required": ["kind", "mode", "name"],
                "additionalProperties": False,
            },
            handler=studio_tool_save,
            preflight=_preflight_tool,
            verification_hint="保存后确认已注册进网关目录。",
        ),
        ToolDefinition(
            name="studio_tool_delete",
            display_name="移除工具",
            description=(
                "移除自定义 HTTP 工具（kind=http）或 MCP 接入项（kind=mcp）。"
                "需要 confirm_name 与工具标识逐字一致；代码注册的 Python 工具不能这样下线。"
            ),
            kind="python",
            risk_level=RiskLevel.DESTRUCTIVE,
            required_scopes=(TOOL_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["http", "mcp", "python"]},
                    "name": {"type": "string", "minLength": 2, "maxLength": 64},
                    "confirm_name": {"type": "string", "minLength": 2, "maxLength": 64},
                },
                "required": ["kind", "name", "confirm_name"],
                "additionalProperties": False,
            },
            handler=studio_tool_delete,
            preflight=_preflight_tool_delete,
            verification_hint="移除后确认网关目录里已经没有它。",
        ),
        ToolDefinition(
            name="studio_knowledge_add",
            display_name="往知识库写一份文档",
            description=(
                "把一段正文写成知识库文档（Markdown 切片）。"
                "同一份正文重复写是幂等的，不会产生两份。"
                "access_scope 取 public / internal / confidential，默认 internal，"
                "越高的分级需要对应的 knowledge 权限。"
                "正文里不要放密钥，凭据类字段会被拒绝。"
                "成功后的返回里带「加工链路」与标签，汇报时一并说出来。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(KNOWLEDGE_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "content": {"type": "string", "minLength": 1},
                    "access_scope": {
                        "type": "string",
                        "enum": ["public", "internal", "confidential"],
                    },
                },
                "required": ["title", "content"],
                "additionalProperties": False,
            },
            handler=studio_knowledge_add,
            preflight=_preflight_credentials,
            verifier=verify_ingested_document,
            verification_hint="另起一个会话按文档 id 回读：文档在、状态 ready、切片数大于 0。",
        ),
        ToolDefinition(
            name="studio_knowledge_delete",
            display_name="下架知识库文档",
            description=(
                "把一份文档从检索里下架（软删除）。需要 confirm_title 与标题逐字一致。"
                "文档 id 用 studio_describe resource=knowledge 查。"
            ),
            kind="python",
            risk_level=RiskLevel.DESTRUCTIVE,
            required_scopes=(KNOWLEDGE_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "minLength": 8, "maxLength": 64},
                    "confirm_title": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "required": ["document_id", "confirm_title"],
                "additionalProperties": False,
            },
            handler=studio_knowledge_delete,
            verifier=verify_document_retired,
            verification_hint="另起会话回读：状态是 deleted，且活跃切片归零。",
        ),
        ToolDefinition(
            name="studio_import",
            display_name="从附件导入（知识库 / 技能 / 工具）",
            description=(
                "把用户在本会话里上传的一份文件按用途建成知识库文档、技能或工具接口。"
                "file_id 取自消息 [附件] 清单；target 传 auto 时按内容判断："
                "带 YAML front matter 的 Markdown 是技能，OpenAPI / Swagger 或工具导出 JSON "
                "是工具，其余文档进知识库。"
                "用户在对话里说「把这份规程入库」「按这份 OpenAPI 加工具」「这份 md 做成技能」"
                "时用它；**不要**让用户去别处上传，也不要先把正文读出来再写——"
                "读取工具会截断长文，截断后的正文入库会少一半且看不出来。"
                "图片不能导入。判不准类型时先问，不要猜。"
                "成功后的返回里带一条「加工链路」（解析 → 清洗 → 结构化 → 切片 → 打标 → 索引）"
                "和识别出的标签：汇报时把它说全，别只报一个切片数——"
                "「我传了 56 万字，为什么库里只有 14 万字」正是靠清洗那一环解释的。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            # 不按目标写死 scope：知识库要 knowledge:write、技能要 skill:write、
            # 工具要平台管理员，三者由 run_import 按实际落点判。
            required_scopes=(),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "pattern": r"^(img|doc|txt)_[0-9a-f]{12}$",
                        "description": "消息 [附件] 清单里的附件句柄，原样传入。",
                    },
                    "target": {
                        "type": "string",
                        "enum": list(IMPORT_TARGETS),
                        "description": "auto 按内容判断；用户说了用途就直接指定。",
                    },
                    "filename": {
                        "type": "string",
                        "maxLength": 200,
                        "description": (
                            "可选。附件清单里的「文件名=」原值。判类依赖后缀，"
                            "只在清单里给了文件名时才传；不要凭猜测编一个。"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "可选。知识库文档标题，默认用文件名去后缀。",
                    },
                    "access_scope": {
                        "type": "string",
                        "description": "可选。知识库分级，拿不准用 internal。",
                    },
                    "name": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "可选。技能标识，默认取 front matter 里的 name。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["merge", "overwrite"],
                        "description": "可选。导入工具时的同名处理方式，默认 merge。",
                    },
                },
                "required": ["file_id"],
                "additionalProperties": False,
            },
            handler=studio_import,
            preflight=_preflight_import,
            verifier=verify_ingested_document,
            verification_hint=(
                "导入后另起会话回读知识库文档（在库、ready、有切片）；"
                "技能与工具没有可回读的落点，不标核验。"
            ),
        ),
        ToolDefinition(
            name="studio_connector_save",
            display_name="新建或修改连接器",
            description=(
                "接入一个外部系统：登记它的端点与认证方式。mode=create 新建，"
                "mode=update 只改传入的字段。"
                "修改端点或启停后，它下面的工具会立即重新注册。"
                "本工具**不接受凭据**：凭证请在控制台「连接器」页填写，"
                "不要索取、也不要让用户在对话里发送。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(CONNECTOR_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["create", "update"]},
                    "name": {"type": "string", "minLength": 2, "maxLength": 64},
                    "display_name": {"type": "string", "maxLength": 128},
                    "description": {"type": "string", "maxLength": 512},
                    "kind": {"type": "string", "enum": ["http", "mcp"]},
                    "base_url": {"type": "string", "maxLength": 512},
                    "auth_type": {
                        "type": "string",
                        "enum": ["none", "bearer", "header", "basic"],
                    },
                    "auth_header_name": {"type": "string", "maxLength": 64},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                    "enabled": {"type": "boolean"},
                },
                "required": ["mode", "name"],
                "additionalProperties": False,
            },
            handler=studio_connector_save,
            preflight=_preflight_credentials,
            verification_hint="保存后确认连接器出现在列表里，并提示用户去配凭据。",
        ),
        ToolDefinition(
            name="studio_connector_delete",
            display_name="删除连接器",
            description=(
                "删除连接器。需要 confirm_name 与标识逐字一致。"
                "下面还挂着工具时会被拒绝——先取消接入再删，"
                "否则那些工具会静默换成另一套凭据来源。"
            ),
            kind="python",
            risk_level=RiskLevel.DESTRUCTIVE,
            required_scopes=(CONNECTOR_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 2, "maxLength": 64},
                    "confirm_name": {"type": "string", "minLength": 2, "maxLength": 64},
                },
                "required": ["name", "confirm_name"],
                "additionalProperties": False,
            },
            handler=studio_connector_delete,
            verification_hint="删除后确认不再出现在连接器列表里。",
        ),
        ToolDefinition(
            name="studio_workflow_save",
            display_name="新建或修改编排流程",
            description=(
                "新建或修改一张编排流程（workflow）。mode=create 新建（必须给 definition），"
                "mode=update 只改传入的字段（不给 definition 就只改说明与启停）。"
                "definition 是一张有向无环图："
                '{"entry": "起点节点id", "nodes": [{"id", "kind"?, "agent"?, "task", '
                '"emit"?: {"field", "values"?/"pattern"?}, "next": [{"when": ...|null, "to": "节点id"}]}]}。'
                "kind 省略即 agent 节点（调 agent 指定的智能体）；kind=compose 只汇总不调人；"
                "kind=parallel 用 branches[{id,agent,task}] 并行再汇总。"
                "task 里可用的占位符只有 {{input.text}}（用户那句话）、{{nodes.<id>}}、"
                "{{nodes.<id>.<field>}}、{{inputs.<name>}}、{{upstream}}（已跑过的汇总）。"
                "next 的 when 为 null 是兜底边：**除终点（next: []）外每条分支都必须有一条**，"
                "否则条件全不命中时流程静默死掉。when 的写法 {key, op, value}，"
                "op 只有 eq/neq/in/gt/lt；key 必须是上游 emit.field 产出的。"
                "节点数上限 20，id 用小写字母开头。保存即编译，图不成立会被拒绝并指出哪个节点——"
                "照它改，不要为了过校验把边改成兜底边。"
                "流程是租户级配置，触发只有「/workflow <标识> <要做的事>」或对话窗口的下拉。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(WORKFLOW_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["create", "update"]},
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    },
                    "display_name": {"type": "string", "maxLength": 128},
                    "description": {"type": "string", "maxLength": 512},
                    "definition": {"type": "object"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["mode", "name"],
                "additionalProperties": False,
            },
            handler=studio_workflow_save,
            verification_hint="保存后回读这张流程，给出节点数、跳数与版本号。",
        ),
        ToolDefinition(
            name="studio_workflow_delete",
            display_name="删除编排流程",
            description=(
                "删除一张编排流程。需要 confirm_name 与流程标识逐字一致。"
                "历史运行记录与未完结的运行都会保留（它们按冻结的图走），"
                "删的只是配置。只是暂时不想被触发时用 studio_workflow_save 传 enabled=false。"
            ),
            kind="python",
            risk_level=RiskLevel.DESTRUCTIVE,
            required_scopes=(WORKFLOW_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "confirm_name": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "required": ["name", "confirm_name"],
                "additionalProperties": False,
            },
            handler=studio_workflow_delete,
            verification_hint="删除后回读，确认列表里已经没有它。",
        ),
    ]
