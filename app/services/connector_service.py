# app/services/connector_service.py
"""外部连接器的读写、凭据、健康检查与运行时注册。

## 三条贯穿全文的约束

1. **凭据不进闭包、不进日志、不回显。** handler 是进程级跨租户的，
   凭据只能经 `ToolExecutionContext.connector` 传进单次调用。
2. **改了定义必须让注册表跟着变。** 连接器改了地址、停用了，它的工具必须
   当场更新或消失；否则页面显示"已停用"，模型还在照旧调用。
3. **凭据缺失绝不回退。** 不回退到 `.env`、也不回退到旧行上的 `token`。
   回退会让"忘了配"看起来像"配通了"。

## 凭据缺失为什么不隐藏工具

工具目录回答的是"这个智能体能碰什么"，不是"现在能不能用"。凭据是运维状态，
会在换令牌期间短暂缺失；把它做成"从目录里消失"，模型会以为自己没这个能力，
然后换一条路去猜。所以：目录里留着，调用时给出可行动的拒绝
（`CONNECTOR_CREDENTIAL_MISSING` + 「去控制台填凭据」）。

这与权限不同：权限不足必须隐藏（那是"不该看见"），凭据未配只是"还没配好"。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory
from app.integrations.mcp_client import (
    McpAuthError,
    McpError,
    McpToolDescriptor,
    McpUnavailableError,
)
from app.models.approval import RiskLevel
from app.models.audit import AuditEventType
from app.models.connector import (
    HEALTH_ERROR,
    HEALTH_OK,
    HEALTH_UNKNOWN,
    Connector,
    ConnectorAuthType,
    ConnectorCredential,
    ConnectorKind,
)
from app.models.custom_tool import CustomTool
from app.models.mcp_registered import McpRegisteredTool
from app.models.tenant import Tenant
from app.services.audit_service import write_audit_event
from app.tenancy.context import current_tenant, tenant_scope
from app.tools.gateway import ToolGateway, ToolGatewayError
from app.tools.mcp_tools import fetch_remote_tools
from app.tools.types import ConnectorRuntime

logger = logging.getLogger(__name__)

# 与 custom_tool_service 同一套命名规则：小写字母开头，只含小写/数字/下划线。
# 名字会进智能体边界、审计与模型目录，中文名或空格会让"引用它"变成一件需要
# 转义的事。
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# 运行时缓存：工具调用是热路径，每轮对话可能要取好几次。
# 30 秒足够挡住同一轮里的重复查询，又短到「配完凭据立刻生效」不会被拖太久
# （设置凭据时还会主动失效，所以实际是立即生效）。
_RUNTIME_TTL_SECONDS = 30.0

# (tenant_id, connector_id) -> (加载时刻, 运行时)
_runtime_cache: dict[tuple[str, int], tuple[float, ConnectorRuntime | None]] = {}


class ConnectorConfigError(ValueError):
    """连接器配置不合法（新建/修改时返回 400）。"""


# ---------------- 运行时快照 ----------------


def invalidate_runtime(
    *,
    tenant_id: str | None = None,
    connector_id: int | None = None,
) -> None:
    """清缓存。设置/清除凭据、改定义、删连接器后都要调。

    不调的表现是「刚配好凭据，助手还说没配」——而重启后就好了，
    这类问题通常会被归因成"缓存问题"然后放着不管。
    """
    if tenant_id is None and connector_id is None:
        _runtime_cache.clear()
        return

    for key in list(_runtime_cache):
        tid, cid = key
        if tenant_id is not None and tid != tenant_id:
            continue
        if connector_id is not None and cid != connector_id:
            continue
        _runtime_cache.pop(key, None)


async def get_connector_runtime(
    *,
    connector_id: int,
    tenant_id: str,
) -> ConnectorRuntime | None:
    """取连接器 + 该租户凭据的运行时快照。连接器停用或不存在时返回 None。

    **必须带 tenant_scope 查凭据**：`connector_credentials` 有行级安全策略，
    不开租户上下文查出来永远是 0 行，而"没有凭据"与"被 RLS 挡了"在代码里
    长得一模一样（这个坑在工具审计上踩过两次）。
    """
    key = (tenant_id, connector_id)
    cached = _runtime_cache.get(key)

    if cached is not None and time.monotonic() - cached[0] < _RUNTIME_TTL_SECONDS:
        return cached[1]

    runtime = await _load_runtime(connector_id=connector_id, tenant_id=tenant_id)
    _runtime_cache[key] = (time.monotonic(), runtime)
    return runtime


async def _load_runtime(
    *,
    connector_id: int,
    tenant_id: str,
) -> ConnectorRuntime | None:
    async with async_session_factory() as session:
        with tenant_scope(tenant_id):
            connector = await session.get(Connector, connector_id)

            if connector is None or not connector.enabled:
                return None

            credential = await session.scalar(
                select(ConnectorCredential).where(
                    ConnectorCredential.connector_id == connector_id
                )
            )

            # 在会话内取完值：ORM 行离开 session 后属性可能已过期。
            return ConnectorRuntime(
                id=connector.id,
                name=connector.name,
                kind=connector.kind.value,
                base_url=connector.base_url or "",
                auth_type=connector.auth_type.value,
                auth_header_name=connector.auth_header_name or "",
                username=(credential.username if credential else None),
                secret=(credential.secret if credential else None),
            )


# ---------------- 读取 ----------------


async def list_connectors(session: AsyncSession) -> list[Connector]:
    return list(
        (await session.scalars(select(Connector).order_by(Connector.name))).all()
    )


async def get_connector(session: AsyncSession, ref: str | int) -> Connector | None:
    """按 name 或 id 取。路由层用它做 `/{ref}`，两种都支持，
    是因为界面上复制来的是名字，脚本里拿到的是 id。"""
    if isinstance(ref, int) or str(ref).isdigit():
        return await session.get(Connector, int(ref))
    return await session.scalar(select(Connector).where(Connector.name == str(ref)))


async def credential_map(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> dict[int, ConnectorCredential]:
    """当前租户在各个连接器上的凭据。调用方必须已经进入 tenant_scope。"""
    rows = (
        await session.scalars(
            select(ConnectorCredential).where(
                ConnectorCredential.tenant_id == tenant_id
            )
        )
    ).all()
    return {row.connector_id: row for row in rows}


async def tool_counts(
    session: AsyncSession,
    *,
    connector_ids: list[int] | None = None,
) -> dict[int, int]:
    """每个连接器下面挂了多少工具。列表页要显示，否则"接了个空的连接器"
    会看起来跟"接好了"完全一样。"""
    counts: dict[int, int] = {}

    for model in (CustomTool, McpRegisteredTool):
        stmt = select(model.connector_id).where(model.connector_id.is_not(None))
        if connector_ids is not None:
            stmt = stmt.where(model.connector_id.in_(connector_ids))
        for value in (await session.scalars(stmt)).all():
            counts[int(value)] = counts.get(int(value), 0) + 1

    return counts


# ---------------- 定义校验 ----------------


def validate_name(name: str) -> str:
    text = (name or "").strip()
    if not _NAME_PATTERN.match(text):
        raise ConnectorConfigError(
            "标识只能是小写字母开头、由小写字母/数字/下划线组成，长度 2–64。"
        )
    return text


def validate_base_url(kind: ConnectorKind, base_url: str) -> str:
    text = (base_url or "").strip()

    if kind == ConnectorKind.MCP:
        if not text:
            raise ConnectorConfigError("MCP 连接器必须填写端点地址（通常以 /mcp 结尾）。")
        if not text.startswith(("http://", "https://")):
            raise ConnectorConfigError("端点地址只支持 http 或 https。")
        return text

    if not text:
        # HTTP 连接器允许留空：此时各工具继续写完整 URL（v1 行为）。
        return ""

    if not text.startswith(("http://", "https://")):
        raise ConnectorConfigError("地址前缀只支持 http 或 https。")

    return text.rstrip("/")


def validate_auth(
    kind: ConnectorKind,
    auth_type: ConnectorAuthType,
    auth_header_name: str,
) -> str:
    """认证方式与协议的兼容性校验。

    MCP 的 Streamable HTTP 只认 `Authorization: Bearer`。允许给它配 header/basic，
    结果是一串「明明配了凭据却还是 401」——这类错最难查，因为配置看起来是对的。
    """
    header = (auth_header_name or "").strip()

    if auth_type == ConnectorAuthType.NONE:
        return ""

    if auth_type == ConnectorAuthType.HEADER:
        if not header:
            raise ConnectorConfigError("选择「自定义请求头」时必须填写头名（如 X-Api-Key）。")
        if kind == ConnectorKind.MCP:
            raise ConnectorConfigError(
                "MCP 只支持 Bearer 认证。自定义请求头请改用 HTTP 连接器。"
            )
        return header

    if auth_type == ConnectorAuthType.BASIC:
        if kind == ConnectorKind.MCP:
            raise ConnectorConfigError(
                "MCP 只支持 Bearer 认证。Basic 认证请改用 HTTP 连接器。"
            )
        return ""

    # bearer
    return header or ""


# ---------------- 写 ----------------


async def create_connector(
    session: AsyncSession,
    *,
    name: str,
    display_name: str,
    description: str,
    kind: ConnectorKind,
    base_url: str,
    auth_type: ConnectorAuthType,
    auth_header_name: str,
    timeout_seconds: int,
    actor_id: str | None,
) -> Connector:
    clean_name = validate_name(name)

    existing = await session.scalar(select(Connector).where(Connector.name == clean_name))
    if existing is not None:
        raise ConnectorConfigError(f"连接器标识 {clean_name} 已存在。")

    row = Connector(
        name=clean_name,
        display_name=(display_name or "").strip() or clean_name,
        description=(description or "").strip(),
        kind=kind,
        base_url=validate_base_url(kind, base_url),
        auth_type=auth_type,
        auth_header_name=validate_auth(kind, auth_type, auth_header_name),
        timeout_seconds=max(1, min(int(timeout_seconds or 30), 300)),
        created_by=actor_id,
    )
    session.add(row)
    await session.flush()

    return row


async def update_connector(
    session: AsyncSession,
    *,
    connector: Connector,
    payload: dict[str, Any],
    actor_id: str | None,
) -> tuple[Connector, dict[str, Any], dict[str, Any]]:
    """更新定义。返回 `(行, before, after)`，两边都只含**变了的**字段。

    只记变化项：把整份配置写进审计，会让"改了个显示名"和"换了端点地址"
    在审计里长得一样，而后者才是需要复核的那件事。
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    def _set(attr: str, value: Any) -> None:
        old = getattr(connector, attr)
        if old == value:
            return
        before[attr] = old
        setattr(connector, attr, value)
        after[attr] = value

    if "display_name" in payload:
        _set("display_name", (payload.get("display_name") or "").strip() or connector.name)
    if "description" in payload:
        _set("description", (payload.get("description") or "").strip())
    if "enabled" in payload:
        _set("enabled", bool(payload["enabled"]))
    if "timeout_seconds" in payload:
        _set("timeout_seconds", max(1, min(int(payload["timeout_seconds"] or 30), 300)))

    if "base_url" in payload or "kind" in payload:
        kind = ConnectorKind(payload.get("kind") or connector.kind.value)
        base_url = validate_base_url(kind, payload.get("base_url", connector.base_url))
        _set("kind", kind)
        _set("base_url", base_url)

    if "auth_type" in payload or "auth_header_name" in payload:
        auth_type = ConnectorAuthType(
            payload.get("auth_type") or connector.auth_type.value
        )
        header = validate_auth(
            connector.kind,
            auth_type,
            payload.get("auth_header_name", connector.auth_header_name),
        )
        _set("auth_type", auth_type)
        _set("auth_header_name", header)

    if before:
        connector.updated_at = _now()

    invalidate_runtime(connector_id=connector.id)
    return connector, before, after


async def delete_connector_guard(session: AsyncSession, *, connector: Connector) -> None:
    """删除前的检查：下面还挂着工具时拒绝。

    不自动把工具变成"独立工具"：那样它们的凭据来源会从连接器切到
    `credential_ref`/`.env`，可能从"能用"直接变成"用别人的令牌"或者
    "报凭据缺失"。这类静默改变配置含义的操作，宁可让人先解绑。
    """
    counts = await tool_counts(session, connector_ids=[connector.id])
    pending = counts.get(connector.id, 0)

    if pending:
        raise ConnectorConfigError(
            f"这个连接器下还有 {pending} 个工具，先解绑或删除它们再删连接器。"
        )


# ---------------- 凭据 ----------------


async def set_credential(
    session: AsyncSession,
    *,
    connector: Connector,
    tenant_id: str,
    secret: str,
    username: str | None,
    actor_id: str | None,
) -> bool:
    """写入或覆盖本租户的凭据。返回值表示「是否新建」。

    传空字符串视为清除，走 `clear_credential`。不在这里静默忽略：
    "我明明点保存了"却什么都没发生，比报错更让人困惑。
    """
    value = (secret or "").strip()
    if not value:
        raise ConnectorConfigError("凭据不能为空。要清除请用清除接口。")

    if connector.auth_type == ConnectorAuthType.BASIC and not (username or "").strip():
        raise ConnectorConfigError("Basic 认证需要同时填写用户名。")

    existing = await session.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.connector_id == connector.id,
            ConnectorCredential.tenant_id == tenant_id,
        )
    )

    created = existing is None

    if existing is None:
        session.add(
            ConnectorCredential(
                tenant_id=tenant_id,
                connector_id=connector.id,
                username=(username or "").strip() or None,
                secret=value,
                created_by=actor_id,
            )
        )
    else:
        existing.secret = value
        existing.username = (username or "").strip() or None
        existing.updated_at = _now()

    await session.flush()
    invalidate_runtime(tenant_id=tenant_id, connector_id=connector.id)
    return created


async def clear_credential(
    session: AsyncSession,
    *,
    connector: Connector,
    tenant_id: str,
) -> bool:
    existing = await session.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.connector_id == connector.id,
            ConnectorCredential.tenant_id == tenant_id,
        )
    )

    if existing is None:
        return False

    await session.delete(existing)
    await session.flush()
    invalidate_runtime(tenant_id=tenant_id, connector_id=connector.id)
    return True


# ---------------- 健康检查 ----------------


async def check_connector(
    *,
    connector: Connector,
    secret: str | None,
) -> tuple[str, str]:
    """连通性检查。返回 `(status, message)`。

    分类原则：**说清下一步该做什么**。
    - 401/403 → 凭据问题，去改凭据；
    - 连不上/超时 → 服务问题，去看对端；
    - 其它 4xx → 端点可达（路径要求参数很正常，尤其是 API 根路径）；
    - 5xx → 对端故障。
    """
    if connector.kind == ConnectorKind.MCP:
        return await _check_mcp(connector=connector, secret=secret)
    return await _check_http(connector=connector, secret=secret)


async def _check_mcp(*, connector: Connector, secret: str | None) -> tuple[str, str]:
    if connector.auth_type != ConnectorAuthType.NONE and not secret:
        return HEALTH_ERROR, "尚未配置凭据，无法校验。"

    try:
        tools = await fetch_remote_tools(url=connector.base_url, token=secret or "")
    except McpAuthError as exc:
        return HEALTH_ERROR, f"凭据被拒绝：{exc}"
    except McpUnavailableError as exc:
        return HEALTH_ERROR, f"连不上端点：{exc}"
    except McpError as exc:
        return HEALTH_ERROR, f"协议错误：{exc}"

    return HEALTH_OK, f"连接正常，远端提供 {len(tools)} 个工具。"


async def _check_http(*, connector: Connector, secret: str | None) -> tuple[str, str]:
    if not connector.base_url:
        return HEALTH_UNKNOWN, "这个连接器没有设置地址前缀，无需检查。"

    runtime = ConnectorRuntime(
        id=connector.id,
        name=connector.name,
        kind="http",
        base_url=connector.base_url,
        auth_type=connector.auth_type.value,
        auth_header_name=connector.auth_header_name or "",
        secret=secret,
    )

    if runtime.requires_secret() and not secret:
        return HEALTH_ERROR, "尚未配置凭据，无法校验。"

    timeout = httpx.Timeout(
        connect=min(5.0, float(connector.timeout_seconds)),
        read=float(connector.timeout_seconds),
        write=float(connector.timeout_seconds),
        pool=3.0,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(connector.base_url, headers=runtime.auth_headers() or None)
    except httpx.TimeoutException:
        return HEALTH_ERROR, "请求超时，对端未在限定时间内响应。"
    except httpx.HTTPError as exc:
        return HEALTH_ERROR, f"连不上地址：{exc.__class__.__name__}"

    if response.status_code in (401, 403):
        return HEALTH_ERROR, f"凭据被拒绝（HTTP {response.status_code}）。"

    if response.status_code >= 500:
        return HEALTH_ERROR, f"对端返回 {response.status_code}，服务可能不健康。"

    return HEALTH_OK, f"端点可达（HTTP {response.status_code}）。"


async def record_health(
    session: AsyncSession,
    *,
    connector: Connector,
    status: str,
    message: str,
) -> None:
    connector.health_status = status
    connector.health_message = message[:500]
    connector.health_checked_at = _now()
    await session.flush()


# ---------------- MCP 能力发现与绑定 ----------------


async def discover_remote(
    *,
    connector: Connector,
    secret: str | None,
) -> list[McpToolDescriptor]:
    if connector.kind != ConnectorKind.MCP:
        raise ConnectorConfigError("只有 MCP 连接器支持「发现」；HTTP 工具的入参要人填。")

    tools = await fetch_remote_tools(url=connector.base_url, token=secret or "")
    return sorted(tools.values(), key=lambda item: item.name)


# 批量接入时的默认风险等级是**写入**，不是读取。
#
# 单条接一个工具时，人会在弹窗里逐个判断风险；批量接二十个时没人会读第二十遍。
# 若默认「读取」，远端一个 `delete_*` 就变成"免审批直接执行"——而它的名字
# 长得和别的工具一样。默认「写入」的代价只是多点一次确认；默认「读取」的代价
# 是一次不该发生的写操作。这两者不对等，所以取严的一侧。
BIND_DEFAULT_RISK = RiskLevel.WRITE


def _bind_risk(item: dict[str, Any]) -> RiskLevel:
    raw = str(item.get("risk_level") or "").strip()
    if not raw:
        return BIND_DEFAULT_RISK

    try:
        return RiskLevel(raw)
    except ValueError as exc:
        # 让非法值变成 400 而不是 500：这是请求内容的问题，不是服务端故障。
        allowed = "、".join(level.value for level in RiskLevel)
        raise ConnectorConfigError(f"未知的风险等级 {raw}，可选：{allowed}。") from exc


async def bind_mcp_tools(
    session: AsyncSession,
    gateway: ToolGateway,
    *,
    connector: Connector,
    secret: str | None,
    items: list[dict[str, Any]],
    actor_id: str | None,
    tenant_id: str,
) -> list[str]:
    """把选中的远端工具登记成本地工具。

    先落库、再拉 schema、最后注册——这个顺序是有意的：schema 拉失败时
    库里已经有一行"待生效"的接入项，运维能看到它、能重试；
    反过来（先注册后落库）失败会留下"模型能用、库里查不到"的工具。
    """
    from app.services.mcp_tool_service import (
        ensure_name_allowed,
        register_row,
    )

    if not items:
        raise ConnectorConfigError("没有选择要接入的工具。")

    if connector.kind != ConnectorKind.MCP:
        raise ConnectorConfigError("只有 MCP 连接器支持批量接入远端工具。")

    remote = {tool.name: tool for tool in await discover_remote(connector=connector, secret=secret)}

    bound: list[str] = []

    for item in items:
        remote_name = str(item.get("remote_name") or "").strip()
        local_name = str(item.get("local_name") or "").strip()

        if remote_name not in remote:
            raise ConnectorConfigError(
                f"远端没有名为 {remote_name} 的工具（端点：{connector.base_url}）。"
            )

        ensure_name_allowed(local_name, gateway=gateway)

        existing = await session.scalar(
            select(McpRegisteredTool).where(McpRegisteredTool.local_name == local_name)
        )

        if existing is not None:
            if existing.connector_id != connector.id:
                raise ConnectorConfigError(
                    f"工具名 {local_name} 已被另一个连接器占用。"
                )
            existing.remote_name = remote_name
            existing.display_name = (
                str(item.get("display_name") or "").strip() or existing.display_name
            )
            existing.description = str(item.get("description") or "").strip()
            existing.risk_level = _bind_risk(item)
            existing.enabled = True
        else:
            session.add(
                McpRegisteredTool(
                    local_name=local_name,
                    remote_name=remote_name,
                    server_url=None,
                    token=None,
                    connector_id=connector.id,
                    display_name=str(item.get("display_name") or "").strip() or remote_name,
                    description=str(item.get("description") or "").strip(),
                    risk_level=_bind_risk(item),
                    timeout_seconds=connector.timeout_seconds,
                    enabled=True,
                    created_by=actor_id,
                )
            )

        bound.append(local_name)

    try:
        await session.flush()
        await session.commit()
    except Exception:
        raise

    # 注册必须在 commit 之后：注册表是进程内状态，先注册后提交失败会留下
    # "能用但库里没有"的工具（`studio_admin_tools` 里踩过）。
    rows = (
        await session.scalars(
            select(McpRegisteredTool).where(McpRegisteredTool.local_name.in_(bound))
        )
    ).all()

    for row in rows:
        descriptor = remote.get(row.remote_name)
        register_row(gateway, row, descriptor, connector=connector)

    await _audit(
        event_type=AuditEventType.CONNECTOR_TOOLS_BOUND,
        tenant_id=tenant_id,
        actor_id=actor_id,
        connector=connector,
        message=f"为连接器 {connector.name} 接入 {len(bound)} 个工具",
        metadata={"tools": bound, "endpoint": connector.base_url},
    )

    return bound


async def unbind_tool(
    session: AsyncSession,
    gateway: ToolGateway,
    *,
    row: McpRegisteredTool,
    connector: Connector | None,
    actor_id: str | None,
    tenant_id: str,
) -> None:
    """取消接入。先删行，再摘注册表——反过来会留下"库里没有、模型还能调"。"""
    from app.services.mcp_tool_service import unregister_row

    local_name = row.local_name
    await session.delete(row)
    await session.commit()

    unregister_row(gateway, local_name)

    await _audit(
        event_type=AuditEventType.CONNECTOR_TOOL_UNBOUND,
        tenant_id=tenant_id,
        actor_id=actor_id,
        connector=connector,
        message=f"取消接入工具 {local_name}",
        metadata={"tool": local_name},
    )


# ---------------- 注册表同步 ----------------


def apply_connector_state(
    gateway: ToolGateway,
    *,
    connector: Connector,
    custom_rows: list[CustomTool],
    mcp_rows: list[McpRegisteredTool],
    descriptors: dict[str, McpToolDescriptor] | None = None,
) -> int:
    """把连接器的当前状态刷进注册表。返回处理了几个工具。

    只做「重新注册」：已存在的同名定义会被 replace。工具被删掉的情况
    由各自的删除路径处理，这里不管。
    """
    from app.services.custom_tool_service import build_definition_for_row
    from app.services.mcp_tool_service import register_row

    changed = 0

    for row in custom_rows:
        try:
            gateway.register_tool(
                build_definition_for_row(row, connector=connector), replace=True
            )
            changed += 1
        except (ToolGatewayError, ValueError):
            logger.exception(
                "连接器变更后重注册 HTTP 工具失败。",
                extra={"event": "connector_tool_reregister_failed", "tool": row.name},
            )

    for row in mcp_rows:
        descriptor = (descriptors or {}).get(row.remote_name)
        if register_row(gateway, row, descriptor, connector=connector):
            changed += 1

    return changed


async def sync_connector_tools(
    session: AsyncSession,
    gateway: ToolGateway,
    *,
    connector: Connector,
) -> int:
    """按连接器当前定义重注册它的全部工具。

    连接器停用时**全部摘掉**：留在注册表里意味着模型还能调用一个
    "页面显示已停用"的工具，而这些调用会打到外部系统上。
    """
    custom_rows = list(
        (
            await session.scalars(
                select(CustomTool).where(CustomTool.connector_id == connector.id)
            )
        ).all()
    )
    mcp_rows = list(
        (
            await session.scalars(
                select(McpRegisteredTool).where(
                    McpRegisteredTool.connector_id == connector.id
                )
            )
        ).all()
    )

    if not connector.enabled:
        from app.services.custom_tool_service import unregister_row as unregister_custom
        from app.services.mcp_tool_service import unregister_row

        for row in custom_rows:
            unregister_custom(gateway, row.name)
        for row in mcp_rows:
            unregister_row(gateway, row.local_name)

        return len(custom_rows) + len(mcp_rows)

    descriptors: dict[str, McpToolDescriptor] = {}
    if mcp_rows:
        secret = await _platform_secret(connector.id)
        try:
            tools = await fetch_remote_tools(url=connector.base_url, token=secret or "")
            descriptors = {tool.name: tool for tool in tools.values()}
        except McpError as exc:
            logger.warning(
                "同步连接器时拉取远端工具清单失败，其 MCP 工具本次不更新。",
                extra={
                    "event": "connector_sync_remote_failed",
                    "connector": connector.name,
                    "error_code": getattr(exc, "code", ""),
                },
            )

    return apply_connector_state(
        gateway,
        connector=connector,
        custom_rows=custom_rows,
        mcp_rows=mcp_rows,
        descriptors=descriptors,
    )


async def _platform_secret(connector_id: int) -> str | None:
    """借一份凭据去拉远端工具清单（**只用于发现 schema**，不用于业务调用）。

    为什么需要借：MCP 的入参 schema 只能连上去问，而注册发生在启动时、
    没有"当前租户"。平台租户优先，其次按租户 id 顺序找第一个配了凭据的。

    逐租户开 scope 而不是一次查全表：`connector_credentials` 有行级安全，
    不开 scope 查出来永远是 0 行——而"没有凭据"与"被 RLS 挡了"在代码里
    长得一模一样（同一个坑在工具审计上踩过两次）。

    不同租户连同一个实例时工具清单相同，所以借谁的都能拉到 schema；
    真正调用时用的是调用方自己的凭据，这一点由 `_client_for` 保证。

    **已经有租户上下文时只用当前租户**：`tenant_scope` 禁止跨租户嵌套，
    硬切会直接抛 `TenantIsolationError`。而且从别人的租户里读凭据去拉 schema，
    即便只读也不该发生在请求路径上。
    """
    active = current_tenant()

    if active is not None:
        candidates = [active]
    else:
        async with async_session_factory() as session:
            tenant_ids = [
                str(value) for value in (await session.scalars(select(Tenant.id))).all()
            ]

        candidates = [settings.platform_tenant_id] + [
            tid for tid in tenant_ids if tid != settings.platform_tenant_id
        ]

    for tenant_id in candidates:
        async with async_session_factory() as session:
            with tenant_scope(tenant_id):
                row = await session.scalar(
                    select(ConnectorCredential).where(
                        ConnectorCredential.connector_id == connector_id,
                        ConnectorCredential.tenant_id == tenant_id,
                    )
                )
        if row is not None:
            return row.secret

    return None


async def purge_credentials(*, connector_id: int) -> int:
    """清掉**当前租户**在这个连接器上的凭据，返回删掉的行数。

    `connector_credentials.connector_id` 没有外键，和 `custom_tools.connector_id`
    一样靠应用层保证（外键在 SQLite 上是空操作，测试全绿也说明不了生产行为）。
    所以删除连接器时要显式清一次，否则密文会留在库里、而它指向的系统已经没了。

    **只清当前租户**，不是因为别的租户不用管，而是管不了：凭据表有行级安全，
    跨租户清理得逐个租户开 scope，而请求路径上 `tenant_scope` 禁止跨租户切换
    （硬切抛 `TenantIsolationError`，结果是「连接器删了、接口 500」）。
    其余租户的行由维护任务逐租户扫掉（见 `purge_orphan_credentials`）。

    这期间它们有害吗？没有：读取凭据必须先从 `connectors` 取到行，连接器已删，
    任何路径都拿不到它。区别只是「密文在库里多躺一轮维护周期」。
    """
    tenant_id = current_tenant()
    if tenant_id is None:
        raise RuntimeError("purge_credentials 必须在租户上下文中调用。")

    async with async_session_factory() as session:
        result = await session.execute(
            delete(ConnectorCredential).where(
                ConnectorCredential.connector_id == connector_id,
                ConnectorCredential.tenant_id == tenant_id,
            )
        )
        await session.commit()
        removed = int(result.rowcount or 0)

    if removed:
        logger.info(
            "已清理连接器凭据。",
            extra={
                "event": "connector_credentials_purged",
                "connector_id": connector_id,
                "tenant_id": tenant_id,
                "count": removed,
            },
        )

    return removed


async def purge_orphan_credentials() -> int:
    """清掉指向已不存在连接器的凭据（**当前租户**视角）。

    由维护任务逐租户调用——这是全库唯一的跨租户路径，写在这里而不是请求路径里，
    是因为它必须遍历租户，而遍历只能在 system 作用域下发起。

    `connectors` 是平台表、没有行级安全，所以子查询本身不需要额外处理；
    凭据表的那一侧由 `tenant_scope` 与 RLS 双重收窄。
    """
    async with async_session_factory() as session:
        result = await session.execute(
            delete(ConnectorCredential).where(
                ConnectorCredential.connector_id.not_in(select(Connector.id))
            )
        )
        await session.commit()
        return int(result.rowcount or 0)


async def load_connector_tools(gateway: ToolGateway) -> int:
    """启动时注册所有启用连接器下的 MCP 工具。

    HTTP 工具走 `load_custom_tools`（它有完整行数据），这里只补 MCP：
    它们的 schema 必须从远端拉一次，没法从库里还原。
    """
    from app.services.mcp_tool_service import register_row

    async with async_session_factory() as session:
        connectors = {
            row.id: row
            for row in (await session.scalars(select(Connector))).all()
            if row.enabled
        }

        if not connectors:
            return 0

        rows = (
            await session.scalars(
                select(McpRegisteredTool).where(
                    McpRegisteredTool.connector_id.is_not(None),
                    McpRegisteredTool.enabled.is_(True),
                )
            )
        ).all()

    by_connector: dict[int, list[McpRegisteredTool]] = {}
    for row in rows:
        if row.connector_id in connectors:
            by_connector.setdefault(int(row.connector_id), []).append(row)

    registered = 0

    for connector_id, group in by_connector.items():
        connector = connectors[connector_id]
        secret = await _platform_secret(connector_id)

        try:
            tools = await fetch_remote_tools(
                url=connector.base_url, token=secret or ""
            )
            descriptors = {tool.name: tool for tool in tools.values()}
        except McpError as exc:
            logger.warning(
                "启动时拉取连接器的远端清单失败，其工具本次不可用。",
                extra={
                    "event": "connector_startup_remote_failed",
                    "connector": connector.name,
                    "error_code": getattr(exc, "code", ""),
                },
            )
            continue

        for row in group:
            descriptor = descriptors.get(row.remote_name)
            if descriptor is None:
                logger.warning(
                    "连接器远端已没有这个工具，本次跳过。",
                    extra={
                        "event": "connector_tool_missing_remotely",
                        "connector": connector.name,
                        "tool": row.local_name,
                        "remote": row.remote_name,
                    },
                )
                continue
            if register_row(gateway, row, descriptor, connector=connector):
                registered += 1

    logger.info(
        "连接器 MCP 工具注册完成。",
        extra={"event": "connector_tools_registered", "registered": registered},
    )
    return registered


# ---------------- 审计 ----------------


async def _audit(
    *,
    event_type: AuditEventType,
    tenant_id: str,
    actor_id: str | None,
    connector: Connector | None,
    message: str,
    metadata: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """连接器相关审计。

    自己开会话 + 自己进租户上下文：调用方可能是一个**平台级**操作
    （没有租户上下文），而 `audit_logs` 有 RLS，不开 scope 会被静默拒掉
    ——异常被吞成日志，看起来"审计已记录"，实际一条都没有。
    """
    if not tenant_id:
        logger.warning(
            "连接器审计缺少租户标识，已跳过。",
            extra={"event": "connector_audit_skipped", "event_type": event_type.value},
        )
        return

    payload = dict(metadata or {})
    if connector is not None:
        payload.setdefault("connector", connector.name)
        payload.setdefault("kind", connector.kind.value)

    try:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    await write_audit_event(
                        session,
                        event_type=event_type,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        resource_type="connector",
                        resource_id=str(connector.id) if connector else None,
                        message=message,
                        metadata=payload,
                        success=success,
                    )
    except Exception:
        # 审计失败不回滚业务：连接器已经改了，不能对外说"没改成"。
        # 但必须留下日志，否则这就是一条静默的审计缺口。
        logger.exception(
            "连接器审计写入失败。",
            extra={"event": "connector_audit_failed", "event_type": event_type.value},
        )


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


__all__ = [
    "ConnectorConfigError",
    "apply_connector_state",
    "bind_mcp_tools",
    "check_connector",
    "clear_credential",
    "create_connector",
    "credential_map",
    "delete_connector_guard",
    "discover_remote",
    "get_connector",
    "get_connector_runtime",
    "invalidate_runtime",
    "list_connectors",
    "load_connector_tools",
    "record_health",
    "set_credential",
    "sync_connector_tools",
    "tool_counts",
    "unbind_tool",
    "update_connector",
    "validate_auth",
    "validate_base_url",
    "validate_name",
]
