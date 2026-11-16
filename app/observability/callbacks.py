# app/observability/callbacks.py
"""LangChain 回调：把 token 用量补到当前 span 上。

为什么用回调而不是在每个调用点读返回值：`with_structured_output()` 返回的是
`ActionPlan`（pydantic 模型），原始 `AIMessage.usage_metadata` 已经被吞掉。
回调是唯一还能拿到 usage 的位置，且不用为了观测改写业务调用。

回调只做一件事：往 `current_span()` 的 attrs 里塞 token 与模型名。
外层由 `span("llm.call", "llm")` 提供，所以这里不负责开/关 span。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler

from app.observability.context import current_span

logger = logging.getLogger(__name__)

# 按 run 暂存用量。为什么不靠 ContextVar：模型的回调可能跑在另一个 asyncio
# 任务里，任务里 set 的值不会传回调用方，而 `chat_service` 要在图返回之后
# 才读这个数。按 run_id 存一份就没有这个坑。
# 规模有界：每轮读完就 pop，正常情况下只会有几条在飞。
_USAGE_BY_RUN: dict[str, dict[str, Any]] = {}
_USAGE_BY_RUN_MAX = 256


def stash_usage_for_run(run_id: str | None, usage: dict[str, Any] | None) -> None:
    if not run_id or not usage:
        return
    if len(_USAGE_BY_RUN) >= _USAGE_BY_RUN_MAX:
        # 只可能是异常路径漏了 pop。丢最旧的，别让内存慢慢涨。
        _USAGE_BY_RUN.pop(next(iter(_USAGE_BY_RUN)), None)
    _USAGE_BY_RUN[run_id] = dict(usage)


def take_usage_for_run(run_id: str | None) -> dict[str, Any]:
    """取走并清掉这条 run 的用量。"""
    if not run_id:
        return {}
    return _USAGE_BY_RUN.pop(run_id, {})


def _usage_from(response: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}

    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(prompt, int):
                out["prompt_tokens"] = prompt
            if isinstance(completion, int):
                out["completion_tokens"] = completion
        model = llm_output.get("model_name") or llm_output.get("model")
        if model:
            out["model"] = str(model)[:64]

    # 新版 langchain 把 usage 挂在 message 上，优先信这个。
    generations = getattr(response, "generations", None)
    if generations and isinstance(generations, list):
        first = generations[0]
        if first and isinstance(first, list):
            message = getattr(first[0], "message", None)
            usage_meta = getattr(message, "usage_metadata", None)
            if isinstance(usage_meta, dict):
                if isinstance(usage_meta.get("input_tokens"), int):
                    out["prompt_tokens"] = usage_meta["input_tokens"]
                if isinstance(usage_meta.get("output_tokens"), int):
                    out["completion_tokens"] = usage_meta["output_tokens"]
            elif isinstance(usage_meta, list) and usage_meta:
                # 部分版本是 list[dict]
                item = usage_meta[0]
                if isinstance(item, dict):
                    if isinstance(item.get("input_tokens"), int):
                        out["prompt_tokens"] = item["input_tokens"]
                    if isinstance(item.get("output_tokens"), int):
                        out["completion_tokens"] = item["output_tokens"]

    return out


class UsageTap(AsyncCallbackHandler):
    """把一次模型调用的 token 用量记到当前 span 上。"""

    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ANN401
        try:
            usage = _usage_from(response)
            if not usage:
                return

            span = current_span()
            if span is not None:
                span.attrs.update(usage)
        except Exception:
            # 观测的失败永远只影响观测自己。
            logger.debug("观测回调处理失败。", exc_info=True)


class UsageCollector(AsyncCallbackHandler):
    """把用量收集到一个普通对象上，供调用方在 `ainvoke` 之后读取。

    与 `UsageTap` 的区别：那个往 span 上写（观测可以关），这个往内存里写，
    是业务侧判断「要不要压缩上下文」的依据——观测默认关闭，压缩依据不能随之消失。
    每次调用新建一个，不共享状态，并发两轮不会互相覆盖。
    """

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}

    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ANN401
        try:
            usage = _usage_from(response)
            if usage:
                self.usage.update(usage)
        except Exception:
            logger.debug("用量收集失败。", exc_info=True)
