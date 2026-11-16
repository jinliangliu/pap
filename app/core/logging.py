# app/core/logging.py
from __future__ import annotations

import json
import logging
import logging.config
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings

# 每个请求由中间件写入；异步任务或 Graph 节点也可显式设置。
request_id_context: ContextVar[str | None] = ContextVar(
    "request_id",
    default=None,
)
thread_id_context: ContextVar[str | None] = ContextVar(
    "thread_id",
    default=None,
)
user_id_context: ContextVar[str | None] = ContextVar(
    "user_id",
    default=None,
)
tenant_id_context: ContextVar[str | None] = ContextVar(
    "tenant_id",
    default=None,
)


SENSITIVE_FIELD_NAMES = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "set-cookie",
    "x-api-key",
    "client_secret",
    "private_key",
    "openai_api_key",
    "langsmith_api_key",
    "jwt_secret_key",
}

# 最后的兜底措施：屏蔽普通字符串中常见的 Bearer token。
BEARER_PATTERN = re.compile(
    r"(?i)(bearer\s+)([a-z0-9\-._~+/]+=*)"
)


def redact_sensitive_data(value: Any) -> Any:
    """
    递归脱敏日志数据。

    该函数只用于日志，不应被当作安全存储方案。
    敏感值本身仍不应写进数据库审计字段或业务状态。
    """
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("_", "-")
            if (
                normalized_key in SENSITIVE_FIELD_NAMES
                or "password" in normalized_key
                or "secret" in normalized_key
                or normalized_key.endswith("token")
            ):
                result[str(key)] = "***REDACTED***"
            else:
                result[str(key)] = redact_sensitive_data(item)
        return result

    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)

    if isinstance(value, str):
        return BEARER_PATTERN.sub(r"\1***REDACTED***", value)

    return value


class ContextFilter(logging.Filter):
    """
    将请求 / 会话 / 用户上下文附加到每条日志记录。

    用户 ID 仅用于审计关联；不得在日志中记录原始 Token。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get() or "-"
        record.thread_id = thread_id_context.get() or "-"
        record.user_id = user_id_context.get() or "-"
        record.tenant_id = tenant_id_context.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """
    面向容器日志平台的轻量 JSON Formatter。

    生产中可替换为 structlog、OpenTelemetry Logging 或
    平台规定的日志格式。
    """

    reserved_log_record_fields = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(
            record.created,
            tz=UTC,
        ).isoformat()

        event: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "thread_id": getattr(record, "thread_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "tenant_id": getattr(record, "tenant_id", "-"),
        }

        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)

        extra_fields = {
            key: value
            for key, value in record.__dict__.items()
            if key not in self.reserved_log_record_fields
            and key
            not in {
                "request_id",
                "thread_id",
                "user_id",
                "tenant_id",
            }
        }

        if extra_fields:
            event["extra"] = redact_sensitive_data(extra_fields)

        return json.dumps(
            redact_sensitive_data(event),
            ensure_ascii=False,
            default=str,
        )


class RedactingFormatter(logging.Formatter):
    """
    供开发环境使用的人类可读日志格式。

    注意：标准 logging 的 message 在格式化时生成；
    因此此处额外对 message 进行 Bearer Token 脱敏。
    """

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        original_args = record.args

        try:
            rendered_message = record.getMessage()
            record.msg = redact_sensitive_data(rendered_message)
            record.args = ()
            return super().format(record)
        finally:
            record.msg = original_msg
            record.args = original_args


def configure_logging(settings: Settings | None = None) -> None:
    """
    配置应用根日志。

    应在 FastAPI lifespan 启动阶段尽早调用一次。
    """
    settings = settings or get_settings()

    formatter_name = "json" if settings.log_json else "standard"

    logging_config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "context": {
                "()": ContextFilter,
            },
        },
        "formatters": {
            "standard": {
                "()": RedactingFormatter,
                "format": (
                    "%(asctime)s %(levelname)s %(name)s "
                    "[request_id=%(request_id)s "
                    "thread_id=%(thread_id)s "
                    "user_id=%(user_id)s "
                    "tenant_id=%(tenant_id)s] "
                    "%(message)s"
                ),
                "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            },
            "json": {
                "()": JsonFormatter,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": settings.log_level,
                "formatter": formatter_name,
                "filters": ["context"],
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "level": settings.log_level,
            "handlers": ["console"],
        },
        "loggers": {

# 将常见库日志控制在合理级别。
            "uvicorn.access": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "sqlalchemy.engine": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "httpx": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
        },
    }

    logging.config.dictConfig(logging_config)

# 避免某些运行环境里未捕获异常直接丢失。
    sys.excepthook = _unhandled_exception_hook


def _unhandled_exception_hook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: Any,
) -> None:
    logging.getLogger("app.unhandled").critical(
        "未捕获的顶层异常。",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def set_log_context(
    *,
    request_id: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """
    设置当前异步上下文的日志关联字段。

    FastAPI 中间件可在收到请求后调用；
    审批恢复接口和 Graph 执行前也可补充 thread_id。
    """
    if request_id is not None:
        request_id_context.set(request_id)

    if thread_id is not None:
        thread_id_context.set(thread_id)

    if user_id is not None:
        user_id_context.set(user_id)

    if tenant_id is not None:
        tenant_id_context.set(tenant_id)


def clear_log_context() -> None:
    """
    清除当前请求的关联信息。

    在 HTTP middleware 的 finally 中调用，避免连接复用或
    测试环境中上下文串扰。
    """
    request_id_context.set(None)
    thread_id_context.set(None)
    user_id_context.set(None)
    tenant_id_context.set(None)


def get_logger(name: str) -> logging.Logger:
    """
    统一获取 logger，便于后续替换为 structlog 等实现。

    示例：
        from app.core.logging import get_logger
        logger = get_logger(__name__)
        logger.info("审批预览已创建")
    """
    return logging.getLogger(name)
