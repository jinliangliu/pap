# app/integrations/sandbox_client.py
"""外部代码沙箱客户端。

边界划分（务必保持）：

- 平台**不执行**模型或用户给出的代码，只做协议适配；
- 权限、审批、限流由 ToolGateway 与 ApprovalService 负责，这里不做任何判断；
- 沙箱不可用时必须显式失败。不允许降级为“在 API 进程里跑一下”，
  那样沙箱的隔离边界就形同虚设。

协议约定（服务端需实现）：

    POST /v1/execute
        body: {language, code, stdin, timeout_seconds}
        header: Authorization: Bearer <key>, X-Sandbox-Tenant: <tenant>,
                Idempotency-Key: <key>
        200 → 终态结果；202 → {execution_id, status: queued|running}
    GET /v1/executions/{id}   → 同上，用于轮询
    GET /v1/health            → 200 表示可用

终态 status 取值：succeeded / failed / timeout / cancelled。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import settings

EXECUTE_PATH = "/v1/execute"
EXECUTION_PATH = "/v1/executions/{execution_id}"
HEALTH_PATH = "/v1/health"

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})

# 租户头：沙箱按它隔离执行记录与配额。
TENANT_HEADER = "X-Sandbox-Tenant"
# 归属信息：执行记录绑定到具体用户与会话，便于审计与按人排查。
USER_HEADER = "X-Sandbox-User"
CONVERSATION_HEADER = "X-Sandbox-Conversation"

# 错误文本上限：服务端可能返回整页 HTML，不能原样塞进日志或用户回答。
MAX_ERROR_CHARS = 240


class SandboxError(RuntimeError):
    """沙箱调用的基础异常。"""


class SandboxUnavailable(SandboxError):
    """网络不可达或沙箱自身异常。提交阶段出现时结果未知。"""


class SandboxRejected(SandboxError):
    """沙箱明确拒绝执行：参数非法、代码超限、配额不足。"""


class SandboxTimeout(SandboxError):
    """预算耗尽仍未拿到终态。"""


@dataclass(frozen=True, slots=True)
class SandboxResult:
    execution_id: str | None
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int | None = None
    truncated: bool = False
    # runsc / process：让审计与排障能看出这次执行用的是哪种隔离后端。
    backend: str = "unknown"

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded" and self.exit_code == 0


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class SandboxClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        max_output_chars: int | None = None,
    ) -> None:
        raw = (base_url if base_url is not None else settings.sandbox_base_url) or ""
        raw = raw.strip()

        if not raw:
            raise RuntimeError("未配置 SANDBOX_BASE_URL，无法创建沙箱客户端。")

        self._base_url = raw.rstrip("/")
        self._api_key = (api_key if api_key is not None else settings.sandbox_api_key) or ""
        self._timeout = float(timeout_seconds or settings.sandbox_timeout_seconds)
        self._poll_interval = float(poll_interval_seconds or settings.sandbox_poll_interval_seconds)
        self._max_output = int(
            max_output_chars if max_output_chars is not None else settings.sandbox_max_output_chars
        )

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout, connect=3.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(
        self,
        idempotency_key: str | None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json"}

        if tenant_id:
            # 租户决定执行记录归属；缺了它沙箱无法做跨租户隔离，
            # 因此平台侧每个请求都显式带上。
            headers[TENANT_HEADER] = tenant_id

        if user_id:
            headers[USER_HEADER] = user_id

        if conversation_id:
            headers[CONVERSATION_HEADER] = conversation_id

        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        if idempotency_key:
            # 幂等键用于让沙箱识别重复提交：确认按钮被连点两次时，
            # 不应该把同一段代码执行两遍。
            headers["Idempotency-Key"] = idempotency_key

        return headers

    async def execute(
        self,
        *,
        code: str,
        language: str,
        stdin: str = "",
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> SandboxResult:
        deadline = time.monotonic() + self._timeout

        payload: dict[str, Any] = {
            "language": language,
            "code": code,
            "stdin": stdin,
            "timeout_seconds": int(timeout_seconds or self._timeout),
        }

        data = await self._submit(
            payload,
            idempotency_key,
            tenant_id,
            user_id,
            conversation_id,
        )

        if data.get("status") not in TERMINAL_STATUSES:
            execution_id = str(data.get("execution_id") or "")

            if not execution_id:
                raise SandboxError("沙箱返回非终态结果但没有 execution_id，无法继续等待。")

            data = await self._poll(
                execution_id,
                deadline,
                tenant_id,
                user_id,
                conversation_id,
            )

        return self._to_result(data)

    async def _submit(
        self,
        payload: dict[str, Any],
        idempotency_key: str | None,
        tenant_id: str | None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(
                EXECUTE_PATH,
                json=payload,
                headers=self._headers(
                    idempotency_key,
                    tenant_id,
                    user_id,
                    conversation_id,
                ),
            )
        except httpx.TimeoutException as exc:
            # 提交超时属于“结果未知”：沙箱可能已经开始执行。
            raise SandboxTimeout("提交代码到沙箱时超时，无法确认是否已开始执行。") from exc
        except httpx.HTTPError as exc:
            raise SandboxUnavailable("无法连接代码沙箱服务。") from exc

        return self._decode(response)

    async def _poll(
        self,
        execution_id: str,
        deadline: float,
        tenant_id: str | None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        path = EXECUTION_PATH.format(execution_id=execution_id)

        while True:
            if time.monotonic() >= deadline:
                raise SandboxTimeout(f"沙箱执行超过 {self._timeout:.0f} 秒仍未结束。")

            await asyncio.sleep(self._poll_interval)

            try:
                response = await self._client.get(
                    path,
                    headers=self._headers(None, tenant_id, user_id, conversation_id),
                )
            except httpx.TimeoutException:
                # 单次轮询慢不代表任务失败，交给 deadline 判定。
                continue
            except httpx.HTTPError as exc:
                raise SandboxUnavailable("轮询沙箱执行结果时连接中断。") from exc

            data = self._decode(response)

            if data.get("status") in TERMINAL_STATUSES:
                return data

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise SandboxUnavailable(f"沙箱服务返回 {response.status_code}，本次执行未完成。")

        if response.status_code >= 400:
            raise SandboxRejected(self._error_message(response))

        try:
            data = response.json()
        except ValueError as exc:
            raise SandboxError("沙箱返回了非 JSON 响应。") from exc

        if not isinstance(data, dict):
            raise SandboxError("沙箱返回结构不符合约定。")

        return data

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        detail = ""

        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")

        if not detail:
            detail = response.text or ""

        detail = detail.strip()[:MAX_ERROR_CHARS]

        return f"沙箱拒绝了本次执行（HTTP {response.status_code}）：{detail}"

    def _clip(self, text: str) -> tuple[str, bool]:
        if self._max_output <= 0 or len(text) <= self._max_output:
            return text, False

        return text[: self._max_output] + "\n…（输出已截断）", True

    def _to_result(self, data: dict[str, Any]) -> SandboxResult:
        stdout, stdout_cut = self._clip(str(data.get("stdout") or ""))
        stderr, stderr_cut = self._clip(str(data.get("stderr") or ""))

        execution_id = data.get("execution_id")

        return SandboxResult(
            execution_id=str(execution_id) if execution_id else None,
            status=str(data.get("status") or "unknown"),
            exit_code=_as_int(data.get("exit_code")),
            stdout=stdout,
            stderr=stderr,
            duration_ms=_as_int(data.get("duration_ms")),
            truncated=stdout_cut or stderr_cut,
            backend=str(data.get("backend") or "unknown"),
        )

    async def get_execution(
        self,
        execution_id: str,
        *,
        tenant_id: str | None = None,
    ) -> SandboxResult:
        """
        回读一次执行的终态结果。

        审批服务要求“独立回读核验”才能把状态推进到 VERIFIED：
        执行时的返回值不能自证，必须再查一次沙箱侧的记录。
        """
        path = EXECUTION_PATH.format(execution_id=execution_id)

        try:
            response = await self._client.get(
                path,
                headers=self._headers(None, tenant_id),
            )
        except httpx.HTTPError as exc:
            raise SandboxUnavailable("回读沙箱执行记录时连接失败。") from exc

        return self._to_result(self._decode(response))

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                HEALTH_PATH,
                headers=self._headers(None),
            )
        except httpx.HTTPError:
            return False

        return response.status_code < 400


@lru_cache(maxsize=1)
def get_sandbox_client() -> SandboxClient:
    return SandboxClient()


async def close_sandbox_client() -> None:
    """测试与关停时释放连接池。"""
    if get_sandbox_client.cache_info().currsize == 0:
        return

    await get_sandbox_client().aclose()
    get_sandbox_client.cache_clear()
