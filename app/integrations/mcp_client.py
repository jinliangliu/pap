# app/integrations/mcp_client.py
"""MCP（Model Context Protocol）客户端。

边界（与 app/tools/mcp_tools.py 的分工）：
- 本模块只做协议适配：连上去、列出工具、调用工具、把异常分类；
- 能不能调、算不算写操作、要不要审批，全部由本地工具表决定，
  这里不做任何业务判断，也不接受调用方指定 URL 或传输方式。

三条硬约束（原占位实现里就写明的，保留）：
1. 模型不能传入 MCP Server 地址，地址只来自服务端配置；
2. 不对远端工具做「发现即暴露」，清单拉回来只用于与白名单比对；
3. 令牌只出现在请求头，不写日志、不进审计、不进模型上下文。

会话是短生命周期的：每次调用新建 Streamable HTTP 会话，用完即关。
多 worker 下不共享连接，也就不会出现连接绑定在别的 event loop 上的问题。
代价是每次多一次 initialize 往返（同机约几十毫秒），可以接受。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError as SdkMcpError

logger = logging.getLogger(__name__)

# 远端自定义的鉴权错误码（JSON-RPC 保留区段 -32000 ~ -32099）。
# 与 prv MCP Server 的 prv_auth.ERR_AUTH_MISSING / ERR_AUTH_INVALID 一致。
_REMOTE_CODE_AUTH_MISSING = -32001
_REMOTE_CODE_AUTH_INVALID = -32002
_AUTH_ERROR_CODES = frozenset({_REMOTE_CODE_AUTH_MISSING, _REMOTE_CODE_AUTH_INVALID})


class McpError(RuntimeError):
    """MCP 调用失败的基类。"""

    code = "MCP_ERROR"


class McpUnavailableError(McpError):
    """连接不上、服务未启动。这类错误重试可能有意义。"""

    code = "MCP_UNAVAILABLE"


class McpAuthError(McpError):
    """凭据缺失或不被接受。

    刻意不继承 McpUnavailableError：后者被标成可重试，而凭据问题重试一万次
    也是同样的结果，只会让上游以为「服务抖动」而反复重发。
    """

    code = "MCP_AUTH_REJECTED"


class McpProtocolError(McpError):
    """连上了但交互不符合协议，或远端返回了无法解析的内容。"""

    code = "MCP_PROTOCOL_ERROR"


class McpTimeoutError(McpError):
    """等待远端响应超时。"""

    code = "MCP_TIMEOUT"


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """远端工具的原始描述。不会直接暴露给模型。"""

    name: str
    description: str
    input_schema: dict[str, Any]


class _StatusRecorder(httpx2.AsyncBaseTransport):
    """传输层留一份「最后一次响应的状态码」。

    MCP SDK 在非 2xx 且响应体不是 JSON-RPC 错误时会丢掉状态码，
    只抛一句 `Server returned an error response`（见 mcp/client/streamable_http.py
    里 `if response.status_code == 404 ... else: ErrorData(code=INTERNAL_ERROR,
    message="Server returned an error response")`）。于是 401（凭据无效）和
    502（网关挡了）在平台侧长得一模一样，提示语只能二选一，猜错就把人送去
    查隧道——线上真发生过：连接器凭据被清空后点「发现工具」，报的是
    「网关拦下了请求」，而实际是远端 401。

    这里不做任何改写，只记状态码，分类时按它给结论。
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner
        self.last_status: int | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        self.last_status = response.status_code
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _flatten(exc: BaseException) -> BaseException:
    """
    anyio 的 TaskGroup 会把异常包成 ExceptionGroup。

    不解包的话，用户和日志里只会看到 "unhandled errors in a TaskGroup"，
    真正的「连接被拒绝」「401」全被埋掉，排障时毫无价值。

    取第一个子异常并不总是对：会话关闭时 TaskGroup 里常同时挂着一个
    业务异常和一个取消/关闭异常，谁排在前面取决于时序。所以先按「有诊断
    价值」挑一遍，都没有才退回第一个。
    """

    def walk(error: BaseException, depth: int = 0) -> list[BaseException]:
        if depth > 8:
            return [error]
        if isinstance(error, BaseExceptionGroup):
            found: list[BaseException] = []
            for child in error.exceptions:
                found.extend(walk(child, depth + 1))
            return found or [error]
        return [error]

    leaves = walk(exc)

    for leaf in leaves:
        if isinstance(
            leaf,
            (
                McpError,
                SdkMcpError,
                httpx2.HTTPStatusError,
                TimeoutError,
                httpx2.TimeoutException,
            ),
        ):
            return leaf

    return leaves[0]


def _http_status_of(exc: BaseException) -> int | None:
    """取 HTTP 状态码。

    httpx2 的 HTTPStatusError 上**没有** status_code 属性，只有 .response，
    而 .response.status_code 才是真值。只读 status_code 的话这个分支永远走不到，
    所有 401 都会被归成「协议错误」——这正是之前的实际表现。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _jsonrpc_error_of(exc: BaseException) -> SdkMcpError | None:
    """尽力从 HTTP 错误响应体里取出 JSON-RPC 错误。取不到就返回 None。"""
    response = getattr(exc, "response", None)
    if response is None:
        return None

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 流式响应未读、非 JSON 等，都算取不到
        return None

    if not isinstance(payload, dict):
        return None

    error = payload.get("error")
    if not isinstance(error, dict):
        return None

    code = error.get("code")
    if not isinstance(code, int):
        return None

    return SdkMcpError(
        code=code,
        message=str(error.get("message") or ""),
        data=error.get("data"),
    )


def _is_transport_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            httpx2.ConnectError,
            httpx2.ConnectTimeout,
            httpx2.ReadTimeout,
            httpx2.WriteTimeout,
            httpx2.PoolTimeout,
            ConnectionError,
            OSError,
        ),
    )


def _extract_payload(result: Any) -> dict[str, Any]:
    """
    从 CallToolResult 里取出业务数据。

    该 MCP 服务开启了 structured_output，业务失败不体现在协议层
    （is_error 恒为 False），而是 structured_content 里的 success/error 字段。
    因此这里只负责取数据，成败判断留在工具层。
    """
    structured = getattr(result, "structured_content", None)

    if isinstance(structured, dict):
        return structured

    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    return {}


class McpToolClient:
    """
    单个 MCP Server 的客户端。

    一个实例只对应一个端点与一个凭据；按租户分发凭据由调用方负责，
    避免把「换租户」这件事藏在客户端内部。
    """

    def __init__(
        self,
        *,
        url: str,
        token: str | None,
        connect_timeout_seconds: float = 15.0,
        call_timeout_seconds: float = 60.0,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise McpUnavailableError("MCP 地址必须是 http(s) URL。")

        # token 为 None = 这个端点不鉴权（内网服务常见），连 Authorization
        # 头都不发；空串仍按配置错误拦住——空串几乎总是「读到了一个空变量」，
        # 放过去会变成 httpx 的 LocalProtocolError("Illegal header value
        # b'Bearer '")，报出来是协议错误，与真正的原因差得远。
        if token is not None:
            token = token.strip()
            if not token:
                raise McpAuthError(
                    "未提供 MCP 访问令牌：请为该租户配置令牌，或检查令牌是否为空。"
                )

        self._url = url
        self._token = token
        self._connect_timeout = connect_timeout_seconds
        self._call_timeout = call_timeout_seconds
        # 最近一次 HTTP 响应的状态码，由 _StatusRecorder 在传输层写入。
        # 只用于错误分类，不参与任何业务判断。
        self._last_status: int | None = None

    # ---------------- 会话 ----------------

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        timeout = httpx2.Timeout(
            None,
            connect=self._connect_timeout,
            read=self._call_timeout,
            write=self._connect_timeout,
            pool=self._connect_timeout,
        )

        # trust_env=False 很关键：MCP 服务在内网（127.0.0.1），
        # 若进程环境里存在 HTTP_PROXY，默认行为会把内网调用也交给代理，
        # 表现为莫名其妙的 502/超时，而同一条 curl 加 --noproxy 又是通的。
        headers = {} if self._token is None else {"Authorization": f"Bearer {self._token}"}
        recorder = _StatusRecorder(httpx2.AsyncHTTPTransport())
        http = httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=recorder,
        )

        try:
            async with streamable_http_client(self._url, http_client=http) as streams:
                read_stream, write_stream = streams

                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
        except Exception as exc:  # noqa: BLE001 - 统一在下面分类
            # 先落状态码再分类：SDK 已经把非 2xx 的响应丢掉了，
            # 这是唯一还能拿到「到底是 401 还是 502」的地方。
            self._last_status = recorder.last_status
            raise self._classify(exc) from exc
        finally:
            # 关闭动作自己出错时不能盖掉上面刚分类好的异常——
            # 那会把「令牌无效」变成一句「aclose 失败」，等于换了个假线索。
            try:
                await http.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 MCP HTTP 客户端时出错。", exc_info=True)

    def _classify(self, exc: BaseException) -> McpError:
        if isinstance(exc, McpError):
            return exc

        root = _flatten(exc)

        if isinstance(root, McpError):
            return root

        # 服务端把业务/鉴权失败放在 JSON-RPC 错误里（HTTP 可能是 200 也可能是 401）。
        # 必须按远端错误码判断，而不是只看 HTTP 状态：新部署的严格鉴权前筛会把
        # 拒绝放在 401 上，但错误体的 code 才是唯一稳定可依的东西。
        if isinstance(root, SdkMcpError):
            return self._classify_remote_error(root)

        if isinstance(root, (TimeoutError, httpx2.TimeoutException)):
            return McpTimeoutError("MCP 服务响应超时。")

        if _is_transport_error(root):
            return McpUnavailableError(f"MCP 服务不可达：{type(root).__name__}。")

        status = _http_status_of(root)
        if status is not None:
            # 非 2xx 时服务端也可能把 JSON-RPC 错误放在 body 里（新部署的
            # 鉴权前筛就是 401 + JSON-RPC 体）。能读出 code 就按 code 分类，
            # 这样「缺令牌」与「令牌无效」不会都塌成一句「401」。
            remote = _jsonrpc_error_of(root)
            if remote is not None:
                return self._classify_remote_error(remote)
            if status in (401, 403):
                return McpAuthError("MCP 服务拒绝了当前凭据（HTTP 401/403）。")
            if status == 404:
                return McpUnavailableError("MCP 端点不存在，请检查 URL 路径。")
            return McpProtocolError(f"MCP 服务返回 HTTP {status}。")

        return McpProtocolError(f"MCP 交互失败：{type(root).__name__}: {root}")

    def _classify_remote_error(self, error: SdkMcpError) -> McpError:
        """把服务端的 JSON-RPC 错误翻成平台的四类语义。"""
        code = getattr(error, "code", None)
        message = str(getattr(error, "message", "") or "").strip() or str(error)

        if code == _REMOTE_CODE_AUTH_MISSING:
            return McpAuthError(
                f"MCP 服务要求携带令牌：{message}"
                if message
                else "MCP 服务要求携带令牌，但请求里没有。"
            )

        if code == _REMOTE_CODE_AUTH_INVALID:
            return McpAuthError(message or "MCP 令牌无效或已停用。")

        lowered = message.lower()

        # 服务端会先校验协议版本再鉴权（避免把 400 掩盖成 401）。
        # 走到这里说明是客户端 SDK 与服务端版本集合不匹配，改配置修不了。
        if "protocol-version" in lowered or "protocol version" in lowered:
            return McpProtocolError(
                "MCP 协议版本不兼容：服务端不接受本客户端协商出的版本。"
                f"（服务端原文：{message}）需要升级客户端 mcp SDK，"
                "或在服务端放宽支持的协议版本。"
            )

        # SDK 在非 2xx 且错误体不是 JSON-RPC 时自己编的兜底文案。它把状态码
        # 丢了，所以这句话得靠传输层记下的 `_last_status` 才能说对：401 是凭据
        # 问题，502 才是网关。分不清的代价是排障方向整个反了——线上有人照着
        # 「网关拦下了请求」去查隧道，而实际连接器根本没配凭据、远端回的是 401。
        if message == "Server returned an error response":
            status = self._last_status

            if status in (401, 403):
                return McpAuthError(
                    f"远端返回 HTTP {status}：凭据被拒绝或未携带。"
                    "请到「配置凭据」检查令牌，而不是改端点地址。"
                )

            if status == 404:
                return McpUnavailableError(
                    "MCP 端点不存在（HTTP 404）。请确认地址里的路径，"
                    "例如别漏了末尾的 /mcp。"
                )

            if status is not None and status >= 500:
                return McpUnavailableError(
                    f"远端返回 HTTP {status}：请求被中间的网关或反向代理挡下了，"
                    "对端 MCP 服务本身可能没收到请求。请检查隧道/反代是否在跑、"
                    "以及鉴权头是否被它改写。"
                )

            where = f"（HTTP {status}）" if status is not None else ""
            return McpProtocolError(
                f"MCP 服务返回了无法解析的错误响应{where}，"
                "通常是网关或反向代理拦下了请求。"
                "请检查地址是否正确、鉴权头是否被网关改写。"
            )

        if message == "Session terminated":
            return McpUnavailableError("MCP 会话已被服务端终止，重新建立会话即可。")

        if message.startswith("Unexpected content type"):
            return McpProtocolError(f"MCP 服务返回了非预期的内容类型：{message}")

        prefix = f"MCP 服务返回错误（{code}）：" if code is not None else "MCP 服务返回错误："

        return McpProtocolError(f"{prefix}{message}")

    # ---------------- 操作 ----------------

    async def list_tools(self) -> list[McpToolDescriptor]:
        """拉取远端工具清单。调用方必须按白名单过滤，不能直接暴露。"""
        async with self._session() as session:
            result = await session.list_tools()

        descriptors: list[McpToolDescriptor] = []

        for tool in getattr(result, "tools", None) or []:
            schema = getattr(tool, "input_schema", None)

            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}

            descriptors.append(
                McpToolDescriptor(
                    name=str(tool.name),
                    description=str(getattr(tool, "description", "") or ""),
                    input_schema=schema,
                )
            )

        return descriptors

    async def call_tool(
        self,
        *,
        remote_tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        调用远端工具，返回业务数据。

        只允许调用本地 registry 显式映射的 remote_tool_name——
        白名单校验在工具层完成，这里不重复，但也绝不用模型给的任意字符串。
        """
        async with self._session() as session:
            result = await session.call_tool(remote_tool_name, arguments)

        return _extract_payload(result)
