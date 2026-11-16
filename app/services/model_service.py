# app/services/model_service.py
"""文字模型服务的解析与实例化。

优先级：model_providers 中 is_active=True 的供应商 > 环境变量兜底。

为什么要有这一层：模型曾经只由 .env 决定，改模型必须重启进程，且控制台
无法回答「现在到底在用哪个模型」。这里把「配置来源」与「构造 ChatOpenAI」
收敛到一处，规划器与图片理解都从这里取，避免两处各读一份配置后逐渐分叉。

安全边界：供应商条目自带 base_url 时，**绝不回落到环境变量的 API Key**。
否则给一个指向第三方地址的供应商配空密钥，就等于把 DeepSeek 的密钥
连同请求一起发给第三方。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from app.core.config import settings as env
from app.db.session import async_session_factory
from app.models.model_provider import clean_context_window
from app.repositories.model_provider import ModelProviderRepository

logger = logging.getLogger(__name__)

# 未指定模型且供应商没填默认值时的兜底。
# 取值依据是实际端点的 /models 返回值，不是猜的：
# api.deepseek.com 只提供 deepseek-flash 与 deepseek-v4-pro。
DEFAULT_TEXT_MODEL = "deepseek-flash"

REASONING_EFFORT_MODES = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class TextModelConfig:
    source: str  # provider | env
    provider_name: str
    protocol: str
    model: str
    api_key: str | None
    base_url: str | None
    temperature: float
    reasoning_mode: str
    # 供应商声明的上下文窗口（token）；None = 未声明，由 CONTEXT_WINDOW_TOKENS 兜底。
    # 不进 fingerprint：窗口不参与构造客户端，改它不该让缓存的 client 失效。
    context_window: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def fingerprint(self) -> tuple[Any, ...]:
        """用于判断缓存的客户端是否还匹配当前配置。"""
        return (
            self.model,
            self.api_key,
            self.base_url,
            self.temperature,
            self.reasoning_mode,
            self.protocol,
        )


def env_config() -> TextModelConfig:
    return TextModelConfig(
        source="env",
        provider_name="环境变量（.env）",
        protocol="openai-compatible",
        model=(env.model_name or DEFAULT_TEXT_MODEL),
        api_key=env.openai_api_key,
        base_url=env.openai_base_url,
        temperature=float(env.model_temperature),
        reasoning_mode="auto",
    )


# 进程内短缓存：每条消息都查一次库没必要，但也不能缓存到必须重启。
_CACHE: dict[str, Any] = {"row": None}
_CACHE_UNTIL = 0.0
_CACHE_TTL = 30.0


async def _active_row():
    global _CACHE_UNTIL
    now = time.monotonic()
    if now < _CACHE_UNTIL:
        return _CACHE["row"]

    try:
        async with async_session_factory() as session:
            row = await ModelProviderRepository(session).get_active()
    except Exception as exc:
        # 读配置失败不能让对话整体不可用：退回环境变量，并留下可查的日志。
        logger.warning(
            "读取模型供应商配置失败，本次沿用环境变量。",
            extra={"event": "model_provider_read_failed", "error": str(exc)},
        )
        return None

    _CACHE["row"] = row
    _CACHE_UNTIL = now + _CACHE_TTL
    return row


def invalidate_model_cache() -> None:
    """控制台保存后立即失效，否则要等 TTL 才生效。"""
    global _CACHE_UNTIL
    _CACHE_UNTIL = 0.0
    _CACHE["row"] = None


def _window_for(models: list, model_id: str) -> int | None:
    """按模型 id 找供应商声明的窗口。

    历史写法里条目可能是纯字符串（只有 id、没有窗口），那种条目返回 None，
    交给 .env 兜底——不能因为「这条没填」就把所有人当成同一种模型。
    """
    for item in models or []:
        if isinstance(item, dict) and str(item.get("id") or "") == model_id:
            return clean_context_window(item.get("context_window"))

    return None


async def resolve_text_model(model_override: str | None = None) -> TextModelConfig:
    """当前生效的文字模型配置。

    model_override 用于「图片理解指定另一个模型」这类场景；
    没有可用供应商时，覆盖值同样作用于环境变量兜底配置。
    """
    row = await _active_row()

    if row is None:
        config = env_config()
        return replace(config, model=model_override) if model_override else config

    model_id = model_override or row.default_text_model or DEFAULT_TEXT_MODEL

    return TextModelConfig(
        source="provider",
        provider_name=row.name,
        protocol=row.protocol or "openai-compatible",
        model=model_id,
        # 注意：这里不做 env 回落，理由见模块文档。
        api_key=row.api_key or None,
        base_url=row.base_url or None,
        temperature=float(row.temperature if row.temperature is not None else 0.0),
        reasoning_mode=row.reasoning_mode or "auto",
        context_window=_window_for(row.models, model_id),
    )


def build_chat_model(
    config: TextModelConfig,
    *,
    temperature: float | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
    disable_thinking: bool = False,
):
    """按配置构造 ChatOpenAI。所有调用方共用，避免参数散落各处。

    disable_thinking：显式关闭思考模式（reasoning_effort="none"）。
    只有「必须强制 tool_choice」的调用方需要它，原因见 planning 模块的说明。
    """
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": config.model,
        "api_key": config.api_key,
        "base_url": config.base_url,
        "temperature": config.temperature if temperature is None else temperature,
        "timeout": env.model_timeout_seconds if timeout is None else timeout,
        "max_retries": env.model_max_retries if max_retries is None else max_retries,
    }

    # 实测 api.deepseek.com 对 reasoning_effort 返回 200 而非 400，
    # 但并非所有兼容服务都容忍未知字段，故仅在需要时下发。
    if disable_thinking:
        kwargs["reasoning_effort"] = "none"
    elif config.reasoning_mode in REASONING_EFFORT_MODES:
        kwargs["reasoning_effort"] = config.reasoning_mode

    return ChatOpenAI(**kwargs)


async def is_model_configured() -> bool:
    """就绪检查用：只要有一条可用来源就算已配置。"""
    try:
        config = await resolve_text_model()
    except Exception:
        return bool(env.openai_api_key)
    return config.configured
