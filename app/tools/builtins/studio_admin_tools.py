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
from app.models.custom_tool import CustomTool
from app.models.mcp_registered import McpRegisteredTool
from app.schemas.custom_tool import CustomToolCreate
from app.services import skill_service
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
        message = f"已入库《{row.title}》：{chunks} 片 · 分级 {row.access_scope}。"

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
        },
        message=message,
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
            verification_hint="写入后读回文档，给出切片数与分级。",
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
            verification_hint="下架后确认该文档已不在检索结果里。",
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
    ]
