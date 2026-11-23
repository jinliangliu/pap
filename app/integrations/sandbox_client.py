# app/integrations/sandbox_client.py
"""外部代码沙箱客户端。

边界划分（务必保持）：

- 平台**不执行**模型或用户给出的代码，只做协议适配；
- 权限、审批、限流由 ToolGateway 与 ApprovalService 负责，这里不做任何判断；
- 沙箱不可用时必须显式失败。不允许降级为“在 API 进程里跑一下”，
  那样沙箱的隔离边界就形同虚设。

协议约定（服务端需实现）：

    POST /v1/execute
        body: {language, code, stdin, timeout_seconds,
               policy: {revision, settings}}
        header: Authorization: Bearer <key>, X-Sandbox-Tenant: <tenant>,
                Idempotency-Key: <key>, X-Policy-Revision: <n>
        200 → 终态结果；202 → {execution_id, status: queued|running}
     GET /v1/executions/{id}   → 同上，用于轮询
     GET /v1/health            → 200 表示可用

`policy` 是**自包含**的一份完整策略，不是「你自己去查」的引用：沙箱在执行
中途不该再问平台一次，否则那次读取与这次提交之间管理员按一下开关，同一次
执行就跨越了两个策略。响应里的 `policy_revision` 必须原样回显——对不上时
平台会保留输出但不计入结论（见 `app/tools/sandbox_tools.py`）。老服务端不
回这个字段不算错：缺字段说明协议还没实现，不是它用了别的版本。

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
# 策略版本：这次执行是按平台的哪一版运行时配置发起的。
#
# 为什么同时放头和 body：头是给网关/代理做路由与日志用的（不必解包），
# body 里那份是**自包含**的（含完整的生效值），沙箱自身的行为不该依赖
# 回头再问平台一次——那会让一次执行的中途依赖两次读取，而两次之间
# 管理员可能已经按了开关。
POLICY_REVISION_HEADER = "X-Policy-Revision"

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
    # 沙箱回报它实际使用的策略版本。None = 没回（老版本服务端）。
    #
    # 用 None 而不是 0 表示「没回」：0 是一个合法的 revision（全按 .env），
    # 与「没告诉我们」混成一个值时，回显校验永远判不出缺失。
    policy_revision: int | None = None

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
        policy_revision: int | None = None,
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

        if policy_revision is not None:
            headers[POLICY_REVISION_HEADER] = str(policy_revision)

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
        policy_revision: int | None = None,
        policy: dict[str, Any] | None = None,
    ) -> SandboxResult:
        deadline = time.monotonic() + self._timeout

        payload: dict[str, Any] = {
            "language": language,
            "code": code,
            "stdin": stdin,
            "timeout_seconds": int(timeout_seconds or self._timeout),
        }

        if policy_revision is not None:
            # 自包含的一份：沙箱不必回头再问平台「现在是什么策略」。
            # `settings` 用点分键（与 `platform_settings.key` 一致），
            # 因为那是唯一稳定的标识，接口字段改名不该让历史快照失效。
            payload["policy"] = {
                "revision": int(policy_revision),
                "settings": dict(policy or {}),
            }

        data = await self._submit(
            payload,
            idempotency_key,
            tenant_id,
            user_id,
            conversation_id,
            policy_revision,
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
        policy_revision: int | None = None,
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
                    policy_revision,
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
            policy_revision=_as_int(data.get("policy_revision")),
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
