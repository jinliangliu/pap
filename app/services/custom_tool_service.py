# app/services/custom_tool_service.py
"""自定义 HTTP Tool 的读写、导入导出与运行时注册。

三条约束贯穿本模块：

1. 配置改了必须让网关注册表跟着变，否则会出现「页面显示已保存、
   模型却还在调旧 URL」这种最难查的错；
2. 删除必须真的从注册表消失，否则模型仍能看见一个库里已不存在的工具；
3. 与内置 / MCP 工具同名时拒绝保存，绝不允许自定义工具覆盖内置实现。

进程重启后的注册由 `load_custom_tools` 在 lifespan 里完成，DB 是唯一真相。
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import RiskLevel
from app.models.connector import Connector
from app.models.custom_tool import CustomTool
from app.schemas.custom_tool import (
    CustomToolCreate,
    CustomToolImportItem,
    CustomToolImportRequest,
    CustomToolImportResponse,
    CustomToolResponse,
    ToolParamIn,
)
from app.tools.custom_http import (
    CustomToolConfigError,
    HttpParamSpec,
    HttpToolConfig,
    build_http_definition,
    build_input_schema,
    parse_params,
    validate_url,
)
from app.tools.gateway import ToolGateway, ToolGatewayError
from app.tools.types import ToolDefinition

logger = logging.getLogger(__name__)

_WRITE_RISKS = frozenset(
    {
        RiskLevel.WRITE,
        RiskLevel.EXECUTE,
        RiskLevel.DESTRUCTIVE,
        RiskLevel.EXTERNAL,
    }
)


def requires_approval(risk_level: RiskLevel) -> bool:
    """与 ToolGateway.requires_approval 保持同一套判定，前端读这里的结果。"""
    return risk_level in _WRITE_RISKS


# ---------------- 行 → 配置 ----------------


def join_base(base_url: str, url: str) -> str:
    """把相对路径拼到连接器的地址前缀上。

    绝对 URL（以 http 开头）原样返回：已经写全的配置不该因为归到某个连接器
    名下就失效，那会让"给旧工具补一个连接器"变成一次破坏性改动。
    """
    prefix = (base_url or "").strip().rstrip("/")
    target = (url or "").strip()

    if not prefix or not target:
        return target

    if target.startswith(("http://", "https://")):
        return target

    return f"{prefix}/{target.lstrip('/')}"


def row_to_config(
    row: CustomTool,
    *,
    connector: Connector | None = None,
) -> HttpToolConfig:
    url = join_base(connector.base_url if connector else "", row.url)

    # 拼接之后再校验一次：`validate_url` 在保存时只看到了片段，
    # 前缀可能把它拼成内网地址或带 userinfo 的形式（防 SSRF）。
    url = validate_url(url)

    return HttpToolConfig(
        name=row.name,
        display_name=row.display_name,
        description=row.description or "",
        method=(row.method or "GET").upper(),
        url=url,
        risk_level=row.risk_level,
        timeout_seconds=int(row.timeout_seconds or 20),
        params=parse_params(row.params),
        headers={str(k): str(v) for k, v in (row.headers or {}).items()},
        enabled=bool(row.enabled),
        connector_id=connector.id if connector else row.connector_id,
        connector_name=connector.name if connector else "",
        connector_ready=bool(connector.enabled) if connector else True,
    )


def build_definition_for_row(
    row: CustomTool,
    *,
    connector: Connector | None = None,
) -> ToolDefinition:
    """行 + 连接器 → 可注册定义。连接器相关路径的唯一入口。

    单独开这个函数而不是让调用方自己拼：`row_to_config` 的默认行为是
    "没有连接器"，忘了传就会出现"连接器改了地址、工具还在调旧地址"，
    而两处代码看起来都正常。
    """
    return build_http_definition(row_to_config(row, connector=connector))


def row_to_response(row: CustomTool, *, gateway: ToolGateway | None = None) -> CustomToolResponse:
    params = parse_params(row.params)
    risk = row.risk_level

    registered = False
    if gateway is not None and gateway.has_tool(row.name):
        registered = gateway.get_tool(row.name).source == "custom"

    return CustomToolResponse(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description or "",
        method=(row.method or "GET").upper(),
        url=row.url,
        risk_level=risk,
        timeout_seconds=int(row.timeout_seconds or 20),
        params=[
            ToolParamIn(
                name=spec.name,
                location=spec.location,  # type: ignore[arg-type]
                type=spec.type,  # type: ignore[arg-type]
                required=spec.required,
                description=spec.description,
                enum=list(spec.enum),
            )
            for spec in params
        ],
        headers={str(k): str(v) for k, v in (row.headers or {}).items()},
        enabled=bool(row.enabled),
        requires_approval=requires_approval(risk),
        registered=registered,
        input_schema=build_input_schema(params),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------- 写入 ----------------


def _params_payload(payload: CustomToolCreate) -> list[dict[str, Any]]:
    return [entry.model_dump() for entry in payload.params]


def validate_payload(payload: CustomToolCreate) -> None:
    """在落库前把 URL / 参数定义校验一遍，错误直接抛给调用方变 400。"""
    validate_url(payload.url)
    parse_params(_params_payload(payload))

    path_params = [item for item in payload.params if item.location == "path"]
    for item in path_params:
        if f"{{{item.name}}}" not in payload.url and f":{item.name}" not in payload.url:
            raise CustomToolConfigError(
                f"参数 {item.name} 声明为 path，但 URL 里没有对应的占位符。"
            )

    # 反向也要拦：URL 里有占位符却没有参数声明时，请求会带着字面
    # `{id}` 发出去，远端返回 404，排查起来像是「接口不存在」。
    declared = {item.name for item in path_params}
    for name in _placeholders(payload.url):
        if name not in declared:
            raise CustomToolConfigError(
                f"URL 里的占位符 {{{name}}} 没有对应的 path 参数。"
            )


def _placeholders(url: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", url))


def ensure_name_available(*, gateway: ToolGateway, name: str) -> None:
    """同名且不是自定义工具时拒绝。

    内置工具与 MCP 工具都在同一个注册表里，允许覆盖等于允许管理员
    用一条配置替换掉审批路径上的既有实现。DB 侧 name 唯一，
    因此「同名自定义」只可能是同一条记录，更新时覆盖是预期行为。
    """
    if not gateway.has_tool(name):
        return

    existing = gateway.get_tool(name)
    if existing.source != "custom":
        raise ToolGatewayError(f"工具名 {name} 已被{_source_label(existing.source)}占用。")


def _source_label(source: str) -> str:
    return {"builtin": "内置工具", "mcp": "MCP 工具"}.get(source, "其他工具")


# ---------------- 运行时注册 ----------------


def register_row(
    gateway: ToolGateway,
    row: CustomTool,
    *,
    connector: Connector | None = None,
) -> bool:
    """注册（或覆盖）单条自定义工具。失败返回 False，不抛出。"""
    try:
        definition = build_definition_for_row(row, connector=connector)
        gateway.register_tool(definition, replace=True)
        return True
    except (CustomToolConfigError, ToolGatewayError, ValueError):
        logger.exception(
            "注册自定义工具失败，该工具本次不可用。",
            extra={"event": "custom_tool_register_failed", "tool_name": row.name},
        )
        return False


def unregister_row(gateway: ToolGateway, name: str) -> None:
    try:
        gateway.unregister_tool(name)
    except ToolGatewayError:
        logger.warning(
            "注销自定义工具时发现它并非自定义来源，已跳过。",
            extra={"event": "custom_tool_unregister_skipped", "tool_name": name},
        )


async def load_custom_tools(session: AsyncSession, gateway: ToolGateway) -> int:
    """启动时把启用中的自定义工具注册进网关。

    单条配置坏掉只跳过那一条：一条写错的 URL 不该让整个平台起不来。
    带连接器的行要连连接器一起看：地址前缀与启停状态都来自它。
    """
    rows = (
        await session.scalars(
            select(CustomTool).where(CustomTool.enabled.is_(True)).order_by(CustomTool.id)
        )
    ).all()

    connectors = {
        row.id: row
        for row in (await session.scalars(select(Connector))).all()
    }

    registered = 0
    for row in rows:
        connector = connectors.get(int(row.connector_id)) if row.connector_id else None

        # 连接器被删了（理论上被 `delete_connector_guard` 挡住，但库可能被人
        # 手工改过）：跳过而不是退回"用行上的完整 URL"——那会让一个本该
        # 不可用的工具悄悄以另一种身份跑起来。
        if row.connector_id and connector is None:
            logger.warning(
                "自定义工具所属的连接器不存在，本次不注册。",
                extra={"event": "custom_tool_connector_missing", "tool_name": row.name},
            )
            continue

        if register_row(gateway, row, connector=connector):
            registered += 1

    logger.info(
        "自定义 HTTP 工具注册完成。",
        extra={
            "event": "custom_tools_loaded",
            "total": len(rows),
            "registered": registered,
        },
    )
    return registered


# ---------------- 导出 / 导入 ----------------


def export_tools(rows: list[CustomTool]) -> list[CustomToolCreate]:
    """导出成可迁移定义：只含「怎么调」，不含任何凭据。"""
    items: list[CustomToolCreate] = []

    for row in rows:
        params = parse_params(row.params)
        items.append(
            CustomToolCreate(
                name=row.name,
                display_name=row.display_name,
                description=row.description or "",
                method=(row.method or "GET").upper(),  # type: ignore[arg-type]
                url=row.url,
                risk_level=row.risk_level,
                timeout_seconds=int(row.timeout_seconds or 20),
                params=[
                    ToolParamIn(
                        name=spec.name,
                        location=spec.location,  # type: ignore[arg-type]
                        type=spec.type,  # type: ignore[arg-type]
                        required=spec.required,
                        description=spec.description,
                        enum=list(spec.enum),
                    )
                    for spec in params
                ],
                headers={str(k): str(v) for k, v in (row.headers or {}).items()},
                enabled=bool(row.enabled),
            )
        )

    return items


async def import_tools(
    session: AsyncSession,
    gateway: ToolGateway,
    *,
    payload: CustomToolImportRequest,
    actor: str | None,
) -> CustomToolImportResponse:
    """按名字合并 / 覆盖导入。逐条处理，一条失败不影响其余。"""
    result = CustomToolImportResponse()

    for entry in payload.tools:
        try:
            validate_payload(entry)
        except CustomToolConfigError as exc:
            result.skipped += 1
            result.items.append(
                CustomToolImportItem(name=entry.name, action="skipped", message=str(exc))
            )
            continue

        existing = await session.scalar(
            select(CustomTool).where(CustomTool.name == entry.name)
        )

        if existing is None:
            if gateway.has_tool(entry.name) and gateway.get_tool(entry.name).source != "custom":
                result.skipped += 1
                result.items.append(
                    CustomToolImportItem(
                        name=entry.name,
                        action="skipped",
                        message="与已有内置 / MCP 工具同名。",
                    )
                )
                continue

            row = CustomTool(
                name=entry.name,
                display_name=entry.display_name,
                description=entry.description,
                method=entry.method,
                url=entry.url,
                risk_level=entry.risk_level,
                timeout_seconds=entry.timeout_seconds,
                params=_params_payload(entry),
                headers=entry.headers,
                enabled=entry.enabled,
                created_by=actor,
            )
            session.add(row)
            await session.flush()
            register_row(gateway, row)
            result.created += 1
            result.items.append(
                CustomToolImportItem(name=entry.name, action="created", message="已新建。")
            )
            continue

        if payload.mode == "merge":
            result.skipped += 1
            result.items.append(
                CustomToolImportItem(
                    name=entry.name,
                    action="skipped",
                    message="同名工具已存在（merge 模式不覆盖）。",
                )
            )
            continue

        _apply_update(existing, entry)
        await session.flush()
        register_row(gateway, existing)
        result.updated += 1
        result.items.append(
            CustomToolImportItem(name=entry.name, action="updated", message="已覆盖。")
        )

    return result


def _apply_update(row: CustomTool, entry: CustomToolCreate) -> None:
    row.display_name = entry.display_name
    row.description = entry.description
    row.method = entry.method
    row.url = entry.url
    row.risk_level = entry.risk_level
    row.timeout_seconds = entry.timeout_seconds
    row.params = _params_payload(entry)
    row.headers = entry.headers
    row.enabled = entry.enabled
    row.updated_at = datetime.now(UTC)


# ---------------- 从 URL / curl 反解参数 ----------------


def parse_url_params(url: str) -> tuple[list[ToolParamIn], str]:
    """把 URL 里的 `{x}` / `:x` 占位符解析成 path 参数。

    返回 (参数列表, 归一化后的 URL)。`:` 形式会被统一改写成 `{x}`，
    因为同一份配置不能同时出现两种占位风格。
    """
    text = validate_url(url)
    params: list[ToolParamIn] = []
    seen: set[str] = set()

    # `http://host/items/:id` 形式：只认出现在路径段起始的冒号。
    normalized = re.sub(
        r"(?<=/):([A-Za-z_][A-Za-z0-9_]*)",
        lambda m: "{" + m.group(1) + "}",
        text,
    )

    for match in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", normalized):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            params.append(ToolParamIn(name=name, location="path"))

    return params, normalized


def parse_curl(curl: str) -> dict[str, Any]:
    """从 curl 命令里提取 method / url / headers / body 参数。

    只做「看得出意图」的解析，不执行任何东西；解析不出的部分留空，
    由管理员在表单里补。拆不动的命令直接报错，别猜。
    """
    text = (curl or "").strip()
    if not text:
        raise CustomToolConfigError("curl 命令为空。")

    if not text.lower().startswith("curl"):
        raise CustomToolConfigError("请粘贴以 curl 开头的命令。")

    try:
        tokens = shlex.split(text.replace("\\\n", " "))
    except ValueError as exc:
        raise CustomToolConfigError(f"curl 命令无法解析：{exc}") from exc

    method = ""
    url = ""
    headers: dict[str, str] = {}
    body: str | None = None

    index = 1
    while index < len(tokens):
        token = tokens[index]

        if token in {"-X", "--request"} and index + 1 < len(tokens):
            method = tokens[index + 1].upper()
            index += 2
            continue

        if token in {"-H", "--header"} and index + 1 < len(tokens):
            raw = tokens[index + 1]
            if ":" in raw:
                key, value = raw.split(":", 1)
                headers[key.strip()] = value.strip()
            index += 2
            continue

        if token in {"-d", "--data", "--data-raw", "--data-binary"} and index + 1 < len(tokens):
            body = tokens[index + 1]
            index += 2
            continue

        if token in {"-u", "--user", "--cookie", "-b"}:
            raise CustomToolConfigError(
                "curl 里包含凭据类参数（-u / --cookie）：凭据不应写进工具配置，"
                "请改用租户工具策略的 credential_ref。"
            )

        if token.startswith("-"):
            # 其他开关（-s / -k / --compressed 等）与工具配置无关，跳过。
            index += 1
            continue

        if not url and ("://" in token or token.startswith("/")):
            url = token

        index += 1

    if not url:
        raise CustomToolConfigError("curl 命令里没有识别到 URL。")

    params, normalized = parse_url_params(url)

    if not method:
        method = "POST" if body else "GET"

    fields: list[dict[str, Any]] = []

    if body:
        try:
            parsed_body = json.loads(body)
        except ValueError:
            parsed_body = None

        if isinstance(parsed_body, dict):
            for key, value in parsed_body.items():
                fields.append(
                    {
                        "name": key,
                        "location": "body",
                        "type": _guess_type(value),
                        "required": True,
                        "description": "",
                    }
                )

    dropped = sorted(
        key for key in headers if key.lower() in {"authorization", "cookie"}
    )

    return {
        "method": method,
        "url": normalized,
        "headers": {
            key: value
            for key, value in headers.items()
            if key.lower()
            not in {"authorization", "cookie", "host", "content-length", "content-type"}
        },
        "params": [*(item.model_dump() for item in params), *fields],
        # 让前端能提示「这几种头因为安全策略没带过来」。
        "dropped_headers": dropped,
        "has_body": bool(body),
    }


def _guess_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


__all__ = [
    "HttpParamSpec",
    "export_tools",
    "import_tools",
    "load_custom_tools",
    "parse_curl",
    "parse_url_params",
    "register_row",
    "requires_approval",
    "row_to_config",
    "row_to_response",
    "unregister_row",
    "validate_payload",
]
