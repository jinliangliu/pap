# app/agents/planner.py
"""请求规划器。

模型输出只是“意图 + 参数建议”，不授予任何权限。
所有工具可见性、参数校验、站点授权、审批与核验都在服务端完成。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from langchain_core.runnables import Runnable

from app.agents.prompts import SYSTEM_PROMPT
from app.agents.schemas import ActionPlan
from app.observability.callbacks import UsageCollector, UsageTap
from app.observability.context import span
from app.services.model_service import build_chat_model, resolve_text_model

logger = logging.getLogger(__name__)


async def _invoke_planner(
    planner: Any, messages: list[Any], collector: UsageCollector | None
) -> ActionPlan:
    """调用规划器，顺便收集 token 用量。

    只有真正的 LangChain Runnable 才接受 `config`；测试里的假规划器只有一个参数。
    用 `isinstance` 判断而不是 try/except TypeError：后者会把调用方自己的
    类型错误也吃掉，然后悄悄再调一次模型。
    """
    if collector is not None and isinstance(planner, Runnable):
        return await planner.ainvoke(messages, config={"callbacks": [collector]})
    return await planner.ainvoke(messages)


def _flush_usage(sink: dict[str, Any] | None, collector: UsageCollector | None) -> None:
    if sink is not None and collector is not None and collector.usage:
        sink.update(collector.usage)

# 工具清单跟着 `llm.call` 一起记：详情页要回答「模型当时到底看到哪些工具」，
# 只记数量回答不了「我明明注册了它，为什么模型没选」。上限按字符算——
# 目录里的 description 不记，只有工具名，94 个约 1.7KB，与同一条 span 里的
# system prompt 相比可以忽略。
TOOL_NAMES_MAX_CHARS = 4000


def _messages_for_obs(messages: list[Any]) -> list[dict[str, Any]]:
    """把 (role, content) 元组摊成可读结构，供观测记录。"""
    out: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, tuple) and len(item) == 2:
            out.append({"role": str(item[0]), "content": item[1]})
        else:
            out.append({"role": "unknown", "content": str(item)})
    return out


def _plan_for_obs(plan: Any) -> Any:
    dump = getattr(plan, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            return str(plan)
    return plan


class ModelNotConfiguredError(RuntimeError):
    """未配置模型密钥。"""


class ModelInvocationError(RuntimeError):
    """模型调用失败，带可区分的错误类别。"""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class PlannerProtocol(Protocol):
    async def ainvoke(self, messages: list[Any]) -> ActionPlan: ...


def classify_model_error(exc: Exception) -> str:
    """区分认证、超时、限流、网络和格式错误，供日志与对外提示使用。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()

    if "authentication" in text or "api key" in text or "unauthorized" in text:
        return "model_auth_failed"
    if "ratelimit" in name or "rate limit" in text or "429" in text:
        return "model_rate_limited"
    if "timeout" in name or "timeout" in text:
        return "model_timeout"
    # 模型名不存在时供应商返回 400，若归到 invalid_request 会被误判成参数写错，
    # 实际是「控制台里选的模型名该换了」。单独给一个类别。
    if "supported api model names" in text or "model_not_found" in text or "does not exist" in text:
        return "model_not_available"
    if "invalidrequest" in name or "bad request" in text or "400" in text:
        return "model_invalid_request"
    if "connection" in name or "network" in text:
        return "model_network_error"
    if "validation" in name or "parse" in name or "json" in text:
        return "model_invalid_response"
    return "model_unavailable"


# 规划器客户端按配置指纹缓存：同一份配置不必每条消息都重建 HTTP 客户端。
_PLANNER_CACHE: dict[str, Any] = {"key": None, "planner": None}

# 有些供应商不认 reasoning_effort（多数非推理模型直接 400 unsupported
# parameter）。记在这里，之后同一份配置就不再传，免得每条消息都先失败一次。
_NO_REASONING_EFFORT: set[tuple[Any, ...]] = set()


def _is_thinking_tool_choice_conflict(exc: Exception) -> bool:
    """思考模式 + 强制 tool_choice 的组合被供应商拒绝。"""
    text = str(exc).lower()
    return "thinking mode" in text and "tool_choice" in text


def _is_reasoning_effort_rejected(exc: Exception) -> bool:
    """供应商不认 reasoning_effort 这个参数。"""
    text = str(exc).lower()
    if "reasoning_effort" not in text and "reasoning effort" not in text:
        return False
    return any(
        word in text
        for word in ("unsupported", "unrecognized", "unknown", "not supported", "invalid")
    )


async def get_planner() -> PlannerProtocol:
    """按当前生效的文字模型配置构造规划器。

    配置来自控制台「运行配置 → 文字模型服务」中所选中的供应商，
    没有可用供应商时回落到 .env。切换供应商不需要重启进程。

    这里**主动关闭思考模式**：结构化输出必须强制指定工具名
    （tool_choice=函数名），而思考模式模型明确拒绝这种组合——
    api.deepseek.com 返回 400「Thinking mode does not support this
    tool_choice」，deepseek-flash 与 deepseek-v4-pro 都一样。
    规划任务的输出只是一段很短的 JSON，本来也不需要长思考，
    关掉比换模型或改成不可靠的文本解析更省事。
    """
    config = await resolve_text_model()

    if not config.configured:
        raise ModelNotConfiguredError(
            "未配置模型服务：请在控制台「运行配置 → 文字模型服务」添加供应商，"
            "或在 .env 中设置 OPENAI_API_KEY。"
        )

    fingerprint = config.fingerprint()
    disable_thinking = fingerprint not in _NO_REASONING_EFFORT
    key = (fingerprint, disable_thinking)

    if _PLANNER_CACHE["planner"] is not None and _PLANNER_CACHE["key"] == key:
        return _PLANNER_CACHE["planner"]

    llm = build_chat_model(config, disable_thinking=disable_thinking)

    # 使用工具调用（function_calling）而非 json_schema：
    # DeepSeek 等 OpenAI 兼容服务目前只支持前者，json_schema 会返回 400。
    planner = llm.with_structured_output(ActionPlan, method="function_calling")

    # 观测用回调：结构化输出把 AIMessage 吞掉了，token 用量只能从回调拿。
    # 观测关闭时回调里的 current_span() 恒为 None，等于空转。
    planner = planner.with_config(callbacks=[UsageTap()])

    _PLANNER_CACHE["key"] = key
    _PLANNER_CACHE["planner"] = planner
    _PLANNER_CACHE["fingerprint"] = fingerprint
    return planner


def mark_reasoning_effort_unsupported() -> None:
    """记下「当前配置不认 reasoning_effort」，并丢掉缓存的规划器。

    下次 get_planner 就会用不含该参数的客户端，不必每条消息先失败一次。
    """
    fingerprint = _PLANNER_CACHE.get("fingerprint")
    if fingerprint:
        _NO_REASONING_EFFORT.add(fingerprint)
    _PLANNER_CACHE["key"] = None
    _PLANNER_CACHE["planner"] = None


_test_planner: PlannerProtocol | None = None


def set_planner_for_tests(planner: PlannerProtocol | None) -> None:
    """测试注入点：替换规划器实例。"""
    global _test_planner
    _test_planner = planner


def reset_planner() -> None:
    global _test_planner
    _test_planner = None
    _PLANNER_CACHE["key"] = None
    _PLANNER_CACHE["planner"] = None
    _PLANNER_CACHE.pop("fingerprint", None)
    _NO_REASONING_EFFORT.clear()


async def create_action_plan(
    *,
    user_message: str,
    available_tools: list[dict[str, Any]],
    pending_tool_name: str | None = None,
    pending_slots: dict[str, Any] | None = None,
    known_user_facts: str = "",
    conversation_summary: str = "",
    recent_turns: list[dict[str, str]] | None = None,
    agent_block: str = "",
    skill_block: str = "",
    delegation_block: str = "",
    skill_meta: dict[str, Any] | None = None,
    usage_sink: dict[str, Any] | None = None,
) -> ActionPlan:
    """
    available_tools 必须由服务端 Tool Registry 生成。

    只向模型暴露工具名、展示名、用途、风险等级、必填字段；
    不暴露 Secret、内部 URL、未授权工具。

    agent_block 是当前智能体的职责（身份 + 边界说明），skill_block 是技能的
    作业指令。两者都拼在 system 末尾而不是塞进 payload：payload 里的内容是
    「数据」，模型会当事实引用；职责与作业规则属于「规则」，放 system 才有
    正确的优先级。

    顺序固定为「基础规则 → 角色职责 → 具体作业步骤」：越靠后越具体，
    也越应该在冲突时压过前面的泛化规则。

    `conversation_summary` 与 `recent_turns` 反过来**只能放 payload**：
    摘要是对更早对话的转述，一旦进了 system 就成了规则——用户上传的文档里
    一句「忽略之前的指令」被摘要提纯后抬进 system，比原文更有杀伤力。
    `user_message` 仍然排在最后，保证当前问题离模型最近。
    """
    payload: dict[str, Any] = {}
    if conversation_summary:
        payload["conversation_summary"] = conversation_summary
    if recent_turns:
        payload["recent_turns"] = recent_turns

    payload["user_message"] = user_message
    payload["available_tools"] = available_tools
    payload["pending_tool_name"] = pending_tool_name
    payload["pending_slots"] = pending_slots or {}
    # 记忆为空时**不放这个键**：多一个空字段模型会以为「系统给了但我没看到」，
    # 反过来去猜用户是谁。没有就是没有。
    if known_user_facts:
        payload["known_user_facts"] = known_user_facts

    planner = _test_planner
    if planner is None:
        planner = await get_planner()

    system_prompt = SYSTEM_PROMPT
    if agent_block:
        system_prompt = f"{system_prompt}\n\n{agent_block}"
    if delegation_block:
        # 在职责之后、作业步骤之前：它描述「这件事该交给谁」，
        # 与职责同属「我该做什么」的层面；技能步骤比它更具体，应排在其后。
        system_prompt = f"{system_prompt}\n\n{delegation_block}"
    if skill_block:
        system_prompt = f"{system_prompt}\n\n{skill_block}"

    messages = [
        ("system", system_prompt),
        ("human", json.dumps(payload, ensure_ascii=False)),
    ]

    span_attrs: dict[str, Any] = {"purpose": "plan"}
    if skill_meta:
        span_attrs["skill.name"] = skill_meta.get("name")
        span_attrs["skill.matched_by"] = skill_meta.get("matched_by")
        span_attrs["skill.tools_before"] = skill_meta.get("tools_before")
        span_attrs["skill.tools_after"] = skill_meta.get("tools_after")
        span_attrs["skill.truncated"] = skill_meta.get("truncated")
        span_attrs["skill.chars"] = skill_meta.get("chars")
        span_attrs["skill.limit"] = skill_meta.get("limit")

    # 目录清单：详情页「准备工具」那一节的数据源。名字过长时截断并留标记，
    # 别让一条 span 的 attrs 无声地膨胀。
    tool_names = [str(item.get("tool_name") or "") for item in available_tools]
    tool_names = [name for name in tool_names if name]
    joined = ",".join(tool_names)
    span_attrs["tools.count"] = len(tool_names)
    span_attrs["tools.names"] = joined[:TOOL_NAMES_MAX_CHARS]
    if len(joined) > TOOL_NAMES_MAX_CHARS:
        span_attrs["tools.names_cut"] = True

    # 用量收集器：只在调用方要的时候挂。它收集的是「这一轮模型实际看到了多少
    # token」，下一轮据此决定要不要压缩上下文。
    collector = UsageCollector() if usage_sink is not None else None

    try:
        async with span("llm.call", "llm", attrs=span_attrs) as llm_span:
            llm_span.content(input=_messages_for_obs(messages))
            plan = await _invoke_planner(planner, messages, collector)
            llm_span.content(output=_plan_for_obs(plan))
            _flush_usage(usage_sink, collector)
            return plan
    except (ModelNotConfiguredError, ModelInvocationError):
        raise
    except Exception as exc:
        # 供应商不认 reasoning_effort：去掉它重试一次。
        # 这样一来，非推理模型（本来就不需要关思考）也能正常规划，
        # 代价只是每条配置第一次多一次失败。
        if _test_planner is None and _is_reasoning_effort_rejected(exc):
            mark_reasoning_effort_unsupported()
            logger.info(
                "当前供应商不认 reasoning_effort，改为不传该参数重试。",
                extra={"event": "reasoning_effort_unsupported"},
            )
            try:
                retry_planner = await get_planner()
                async with span(
                    "llm.call", "llm", attrs={**span_attrs, "attempt": 2}
                ) as retry_span:
                    retry_span.content(input=_messages_for_obs(messages))
                    retry_plan = await _invoke_planner(retry_planner, messages, collector)
                    retry_span.content(output=_plan_for_obs(retry_plan))
                    _flush_usage(usage_sink, collector)
                    return retry_plan
            except (ModelNotConfiguredError, ModelInvocationError):
                raise
            except Exception as retry_exc:
                # 重试也失败：用这一次的错误做归类，别把裸异常漏给上层。
                exc = retry_exc

        category = classify_model_error(exc)
        logger.warning(
            "模型调用失败。",
            extra={
                "event": "model_call_failed",
                "error_category": category,
                # 只在日志里留原因原文，对外统一说「暂时不可用」。
                "detail": str(exc)[:400],
            },
        )
        if _is_thinking_tool_choice_conflict(exc):
            raise ModelInvocationError(
                "model_invalid_request",
                "当前模型处于思考模式且供应商不允许关闭，无法用于结构化规划。"
                "请在供应商侧关闭思考模式，或换用非思考模型。",
            ) from exc
        raise ModelInvocationError(category, "模型服务暂时不可用。") from exc
