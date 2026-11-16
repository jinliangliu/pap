# app/integrations/http_client.py
"""统一的外部 HTTP 适配器。

职责：固定 Base URL、超时、连接错误处理、安全重试、响应标准化、幂等键透传。
禁止在 Tool Handler 中直接散落 httpx 调用。

重试规则：
- 幂等只读 GET：仅对连接失败、读超时等暂态异常做有限次重试；
- 真实写入：默认不自动重试（网络异常可能已被外部系统受理）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.integrations.result import ExternalCallResult


class ExternalTransportError(Exception):
    """外部平台网络异常（只读场景）。"""


class PlatformHttpClient:
    def __init__(self) -> None:
        if not settings.station_platform_base_url:
            raise RuntimeError(
                "未配置 STATION_PLATFORM_BASE_URL，无法创建平台 HTTP 客户端。"
            )

        self._client = httpx.AsyncClient(
            base_url=settings.station_platform_base_url,
            timeout=httpx.Timeout(
                connect=3.0,
                read=float(settings.station_platform_timeout_seconds),
                write=float(settings.station_platform_timeout_seconds),
                pool=3.0,
            ),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
        stop=stop_after_attempt(settings.station_platform_read_retry_attempts + 1),
        wait=wait_exponential_jitter(initial=0.2, max=2),
        reraise=True,
    )
    async def get(
        self,
        *,
        path: str,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> ExternalCallResult:
        """只供幂等只读查询使用。不要复用到写操作。"""
        try:
            response = await self._client.get(path, params=params, headers=headers)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise ExternalTransportError("读取外部平台时网络异常。") from exc

        return self._normalize_response(response)

    async def post_write(
        self,
        *,
        path: str,
        json_body: dict[str, Any],
        idempotency_key: str,
        headers: dict[str, str] | None = None,
    ) -> ExternalCallResult:
        """
        写操作默认不自动重试。

        网络异常不代表“未写入”；调用方必须转入 UNKNOWN / RECOVERY_REQUIRED，
        并依据回读或幂等键查询恢复。
        """
        request_headers = {
            **(headers or {}),
            "Idempotency-Key": idempotency_key,
        }

        try:
            response = await self._client.post(
                path,
                json=json_body,
                headers=request_headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return ExternalCallResult(
                ok=False,
                business_status="unknown",
                retryable=False,
                reason="write_transport_uncertain:外部请求中断，无法确认是否已被接收。",
            )

        return self._normalize_response(response)

    @staticmethod
    def _normalize_response(response: httpx.Response) -> ExternalCallResult:
        try:
            body = response.json()
        except ValueError:
            body = None

        if 200 <= response.status_code < 300:
            return ExternalCallResult(
                ok=True,
                business_status=(
                    body.get("status", "success")
                    if isinstance(body, dict)
                    else "success"
                ),
                http_status=response.status_code,
                data=body,
                external_operation_id=(
                    body.get("operation_id") if isinstance(body, dict) else None
                ),
            )

        if response.status_code in {429, 502, 503, 504}:
            return ExternalCallResult(
                ok=False,
                business_status="temporary_failure",
                http_status=response.status_code,
                data=body,
                retryable=True,
                reason="外部平台暂时不可用。",
            )

        return ExternalCallResult(
            ok=False,
            business_status="failed",
            http_status=response.status_code,
            data=body,
            retryable=False,
            reason="外部平台返回业务失败。",
        )


@lru_cache(maxsize=1)
def get_platform_client() -> PlatformHttpClient:
    """复用连接池；应用关闭时由 shutdown 显式关闭。"""
    return PlatformHttpClient()


async def close_platform_client() -> None:
    if get_platform_client.cache_info().currsize:
        await get_platform_client().close()
        get_platform_client.cache_clear()
