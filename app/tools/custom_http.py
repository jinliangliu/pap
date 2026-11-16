# app/tools/custom_http.py
"""管理员自定义 HTTP Tool 的执行器。

与 station_tools 那批「写死代码」的工具不同，这里的 URL、方法、参数
全部来自 custom_tools 表。因此这一层的职责是把「配置」变成
Gateway 能认的 ToolDefinition，并且**只做配置允许的事**：

- 只允许 http/https，不跟随重定向（3xx 一律按失败处理）；
- 拒绝带 userinfo 的 URL 与云元数据地址（169.254.169.254 / link-local），
  防止一条配置就能让服务端去读实例凭据；
- 请求头只取配置里声明的静态头，不继承进程环境里的任何凭据；
- 响应体按上限截断后再回给模型，避免一次异常响应把上下文打爆。

工具定义在服务端进程里注册一次，配置变更后由 custom_tool_service
重新注册（replace=True）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.models.approval import RiskLevel
from app.models.custom_tool import SUPPORTED_METHODS
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 12000

_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"

_PARAM_TYPES = ("string", "integer", "number", "boolean")
_BLOCKED_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})


class CustomToolConfigError(ValueError):
    """配置本身不合法（新建/修改时返回 400）。"""


@dataclass(frozen=True, slots=True)
class HttpParamSpec:
    name: str
    location: str = "query"
    type: str = "string"
    required: bool = True
    description: str = ""
    enum: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HttpToolConfig:
    """从表行拍平出来的只读配置，handler 闭包只持有它。"""

    name: str
    display_name: str
    description: str
    method: str
    url: str
    risk_level: RiskLevel
    timeout_seconds: int
    params: tuple[HttpParamSpec, ...]
    headers: dict[str, str]
    enabled: bool = True

    # 来源连接器。凭据**不在这里**：handler 是进程级闭包，塞进第一个租户的
    # 令牌就等于让第二个租户拿别人的身份去请求。凭据只走
    # `ToolExecutionContext.connector`，那是单次调用的作用域。
    connector_id: int | None = None
    connector_name: str = ""
    connector_ready: bool = True


# ---------------- 配置校验 ----------------


def validate_url(url: str) -> str:
    """校验并归一化 URL。返回去掉首尾空白的值。"""
    text = (url or "").strip()
    if not text:
        raise CustomToolConfigError("URL 不能为空。")

    parsed = urlparse(text)

    if parsed.scheme not in {"http", "https"}:
        raise CustomToolConfigError("URL 只支持 http 或 https。")

    if not parsed.hostname:
        raise CustomToolConfigError("URL 缺少主机名。")

    if parsed.username or parsed.password:
        # 凭据写进 URL 会被日志、审计和错误信息带出去，一律拒绝。
        raise CustomToolConfigError("URL 不能内嵌用户名或密码，请改用请求头或租户凭据。")

    host = parsed.hostname.lower()

    if host in _BLOCKED_HOSTS:
        raise CustomToolConfigError("不允许指向云元数据地址。")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None and (address.is_link_local or address.is_unspecified):
        raise CustomToolConfigError("不允许指向链路本地或未指定地址。")

    return text


def parse_params(raw: Any) -> tuple[HttpParamSpec, ...]:
    """把表里的 JSON 参数定义解析成强类型列表。

    形状不对就报错，而不是静默忽略——少一个参数会让模型调用时
    收到一个看不懂的 400，排查成本远高于保存时直接拒绝。
    """
    if raw is None:
        return ()

    if not isinstance(raw, list):
        raise CustomToolConfigError("工具参数必须是数组。")

    specs: list[HttpParamSpec] = []
    seen: set[str] = set()

    for entry in raw:
        if not isinstance(entry, dict):
            raise CustomToolConfigError("每个工具参数必须是对象。")

        name = str(entry.get("name") or "").strip()
        if not name:
            raise CustomToolConfigError("工具参数缺少 name。")

        if name in seen:
            raise CustomToolConfigError(f"参数名重复：{name}。")
        seen.add(name)

        location = str(entry.get("location") or "query").strip().lower()
        if location not in {"path", "query", "body", "header"}:
            raise CustomToolConfigError(f"参数 {name} 的位置不合法：{location}。")

        value_type = str(entry.get("type") or "string").strip().lower()
        if value_type not in _PARAM_TYPES:
            raise CustomToolConfigError(f"参数 {name} 的类型不合法：{value_type}。")

        enum_raw = entry.get("enum") or []
        if not isinstance(enum_raw, list):
            raise CustomToolConfigError(f"参数 {name} 的 enum 必须是数组。")

        specs.append(
            HttpParamSpec(
                name=name,
                location=location,
                type=value_type,
                required=bool(entry.get("required", True)),
                description=str(entry.get("description") or ""),
                enum=tuple(str(item) for item in enum_raw),
            )
        )

    return tuple(specs)


def build_input_schema(params: tuple[HttpParamSpec, ...]) -> dict[str, Any]:
    """由参数定义生成给模型看的 JSON Schema。

    additionalProperties=False：模型多塞一个字段会被 Gateway 拦下，
    而不是被悄悄丢掉——后者会让「我明明给了参数」变成一场扯皮。
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for spec in params:
        item: dict[str, Any] = {
            "type": spec.type,
            "description": spec.description or f"{spec.location} 参数",
        }
        if spec.enum:
            item["enum"] = list(spec.enum)
        properties[spec.name] = item
        if spec.required:
            required.append(spec.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required

    return schema


def _render_path(url: str, arguments: dict[str, Any], path_params: list[HttpParamSpec]) -> str:
    rendered = url
    for spec in path_params:
        if spec.name not in arguments:
            raise CustomToolConfigError(f"缺少路径参数：{spec.name}。")

        value = str(arguments[spec.name])
        replaced = False
        for placeholder in (f"{{{spec.name}}}", f":{spec.name}"):
            if placeholder in rendered:
                rendered = rendered.replace(placeholder, value)
                replaced = True
        if not replaced:
            # 声明成 path 却没在 URL 里占位，属于配置错误。
            raise CustomToolConfigError(f"URL 中没有 {spec.name} 的占位符。")

    return rendered


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _make_handler(config: HttpToolConfig):
    async def handler(
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> ToolExecutionResult:
        path_params = [spec for spec in config.params if spec.location == "path"]
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        headers: dict[str, str] = dict(config.headers)

        for spec in config.params:
            if spec.name not in arguments:
                continue
            value = arguments[spec.name]
            if value is None:
                continue

            if spec.location == "query":
                query[spec.name] = _coerce_bool(value)
            elif spec.location == "body":
                body[spec.name] = value
            elif spec.location == "header":
                headers[spec.name] = str(value)

        try:
            target = _render_path(config.url, arguments, path_params)
        except CustomToolConfigError as exc:
            return ToolExecutionResult(
                success=False,
                message=str(exc),
                error_code="CUSTOM_TOOL_BAD_PATH_PARAM",
            )

        # 租户凭据存在时按 Bearer 透传：密钥来自租户策略的 credential_ref，
        # 不落在工具配置里，也不进日志。
        #
        # 连接器凭据优先，且**不与该路径混用**：连上了连接器就用连接器的认证方式，
        # 否则会出现「连接器要求 X-Api-Key，同时又被塞了一个 Authorization」
        # 这种谁也没配过的请求头组合。
        if context.connector is not None:
            headers.update(context.connector.auth_headers())
        elif context.credential:
            headers.setdefault("Authorization", f"Bearer {context.credential}")

        timeout = httpx.Timeout(
            connect=min(5.0, float(config.timeout_seconds)),
            read=float(config.timeout_seconds),
            write=float(config.timeout_seconds),
            pool=3.0,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(
                    config.method,
                    target,
                    params=query or None,
                    json=body if body else None,
                    headers=headers or None,
                )
        except httpx.TimeoutException:
            return ToolExecutionResult(
                success=False,
                message=f"外部接口超时（{config.timeout_seconds} 秒），结果未知。",
                error_code="CUSTOM_TOOL_TIMEOUT",
                retryable=config.method == "GET",
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "自定义 HTTP 工具请求失败。",
                extra={
                    "event": "custom_tool_transport_error",
                    "tool_name": config.name,
                    "host": urlparse(config.url).netloc,
                    "error": exc.__class__.__name__,
                },
            )
            return ToolExecutionResult(
                success=False,
                message="无法连接外部接口，结果未知。",
                error_code="CUSTOM_TOOL_TRANSPORT_ERROR",
                retryable=config.method == "GET",
            )

        payload, truncated = _read_body(response)

        if 200 <= response.status_code < 300:
            return ToolExecutionResult(
                success=True,
                result={
                    "status_code": response.status_code,
                    "data": payload,
                    "truncated": truncated,
                },
                message=f"外部接口返回 {response.status_code}。",
            )

        # 只有幂等只读请求才标可重试。写操作拿到 502 / 503 也不代表没写进去，
        # 自动重发可能造成重复下单，交给上层按幂等键回读判断。
        return ToolExecutionResult(
            success=False,
            result={
                "status_code": response.status_code,
                "data": payload,
                "truncated": truncated,
            },
            message=f"外部接口返回 {response.status_code}。",
            error_code=f"CUSTOM_TOOL_HTTP_{response.status_code}",
            retryable=(
                config.method == "GET"
                and response.status_code in {429, 502, 503, 504}
            ),
        )

    return handler


def _read_body(response: httpx.Response) -> tuple[Any, bool]:
    """优先按 JSON 解析，其次按文本；超长一律截断并标记。"""
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text

    if isinstance(body, str) and len(body) > MAX_RESULT_CHARS:
        return body[:MAX_RESULT_CHARS], True

    if not isinstance(body, str):
        dumped = json.dumps(body, ensure_ascii=False, default=str)
        if len(dumped) > MAX_RESULT_CHARS:
            return dumped[:MAX_RESULT_CHARS], True

    return body, False


def build_http_definition(config: HttpToolConfig) -> ToolDefinition:
    """把配置转成 Gateway 可注册的定义。

    审批由风险等级推导（Gateway 里 write 及以上强制走审批），
    这里不再单独存一个 requires_approval 字段，避免两处真相打架。
    """
    if config.method not in SUPPORTED_METHODS:
        raise CustomToolConfigError(f"不支持的 HTTP 方法：{config.method}。")

    return ToolDefinition(
        name=config.name,
        display_name=config.display_name,
        description=config.description or f"调用 {config.method} {config.url}",
        kind="http",
        source="custom",
        risk_level=config.risk_level,
        input_schema=build_input_schema(config.params),
        handler=_make_handler(config),
        enabled=config.enabled,
        requires_approval=False,
        timeout_seconds=max(1, min(int(config.timeout_seconds), 120)),
        verification_hint="调用成功仅代表外部接口返回 2xx，业务结果仍需按返回值判断。",
        connector_id=config.connector_id,
        connector_name=config.connector_name,
        connector_ready=config.connector_ready,
    )
