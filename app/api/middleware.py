# app/api/middleware.py
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import clear_log_context, set_log_context
from app.observability.metrics import inc, observe

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """写入请求 ID、耗时与基础指标；不记录请求体，避免敏感内容落日志。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex}"
        set_log_context(request_id=request_id)

        started = time.perf_counter()
        labels = f'{{method="{request.method}",path="{request.url.path}"}}'

        try:
            response = await call_next(request)
        except Exception:
            inc("http_errors_total", labels=labels)
            logger.exception(
                "请求处理异常。",
                extra={"event": "http_request_failed", "path": request.url.path},
            )
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            observe("http_request_duration_ms_avg", duration_ms, labels=labels)
            clear_log_context()

        inc("http_requests_total", labels=labels)

        if response.status_code >= 500:
            inc("http_errors_total", labels=labels)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
