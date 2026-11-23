# app/services/memory_extract.py
"""记忆自动抽取：一轮对话结束后，判断里面有没有值得长期记住的事实。

为什么放在「回答落地之后」而不是对话中间：

- 抽取要读完整的一问一答，中途看到的只是半截；
- 抽取失败不能影响回答，放在主链路上等于给对话加了一个可失败点；
- 用户不关心这一轮有没有产生记忆，它必须是后台任务。

五条边界，先写在这里：

1. 只记稳定信息。本轮参数、一次性查询结果、工具返回内容、助手自己的建议，一律不记。
2. **置信度就是「能不能未经确认直接注入」的依据**：>=0.8 是用户明确说的，
   0.6-0.8 是可合理推断的，低于 0.6 直接丢弃——猜出来的东西连列表都不该进。
3. 不覆盖用户自己写过或确认过的记忆。模型推断出的话不能悄悄替换用户的原话，
   要改由人在页面上改。
4. 任何异常都吞掉，只留一条 warning。记忆是加分项，不能变成对话的故障点。
5. 关掉开关就完全不产生模型调用（连进程内判断都省了）。

与 `memory_service` 的分工：本模块只负责「问模型 + 解析」，写库规则在
`memory_service.upsert_extracted`，方便单独测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.memory import MemoryType
from app.services import memory_service
from app.services.model_service import build_chat_model, resolve_text_model
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

# 太短的一轮不可能包含稳定事实（「嗯」「好的」「几点」），
# 直接跳过比叫一次模型省钱。
MIN_USER_CHARS = 6

# 一轮最多记三条。抽得越多越像在写日记，而记忆的价值在于少而准。
MAX_ITEMS = 3
MAX_TITLE = 40
MAX_CONTENT = 300

# 低于这个置信度连候选都不留：列表里堆满模型猜的东西，用户就不看了。
MIN_CONFIDENCE = 0.6

# 输入截断：一轮对话的原文可能几万字符（附件 OCR、长回答），
# 判断有没有稳定事实用不着全部。
MAX_MESSAGE_CHARS = 800
MAX_ANSWER_CHARS = 1200
MAX_KNOWN_TITLES = 80

EXTRACT_TIMEOUT_SECONDS = 45


EXTRACT_SYSTEM_PROMPT = """你负责从一轮对话里挑出「值得长期记住的用户信息」。

可以记的五类（type 用英文键）：
- profile：称呼、身份、岗位、所在团队
- preference：稳定的偏好与禁忌（回答风格、语言、格式、称呼方式）
- decision：已经拍板的方案、技术选型、约定
- fact：长期有效的背景事实（项目、环境、设备、常用仓库或系统）
- working_note：正在推进的事的当前状态（做到哪一步、下一步做什么）

不要记：
- 本轮参数与一次性数据（站点编号、某天日期、某次查询的数值）；
- 工具返回的内容、助手自己的猜测或建议；
- 「用户问了一个问题」「用户希望解决问题」这类没有信息量的概括；
- 密钥、口令、令牌、任何凭据；
- known_memory_titles 里已经有、且本轮没有新说法的条目。

判定标准：这条信息下一轮还用得上吗？换个会话还成立吗？两个都「是」才记。

只输出 JSON，不要代码块，不要解释：
{"memories":[{"type":"profile","title":"称呼","content":"希望被称呼为 alex，不要用「您」。","confidence":0.95}]}

字段要求：
- title：3-12 个字的名词短语（称呼 / 回答风格 / 观测方案），同一件事每次都用同一个标题；
- content：一句话，40 字以内，第三人称陈述，不要写「用户说」；
- confidence：用户明确说了 0.9；从对话可合理推断 0.7；拿不准就不要输出。
- 没有值得记的就返回 {"memories":[]}，这是最常见的正确答案。"""


# 模型偶尔会把类型写成中文，或者写成「身份」这种口语说法。
_TYPE_ALIASES: dict[str, MemoryType] = {
    "profile": MemoryType.PROFILE,
    "身份": MemoryType.PROFILE,
    "称呼": MemoryType.PROFILE,
    "preference": MemoryType.PREFERENCE,
    "偏好": MemoryType.PREFERENCE,
    "decision": MemoryType.DECISION,
    "决策": MemoryType.DECISION,
    "fact": MemoryType.FACT,
    "事实": MemoryType.FACT,
    "working_note": MemoryType.WORKING_NOTE,
    "进展": MemoryType.WORKING_NOTE,
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


async def _invoke_text_model(system: str, human: str) -> str:
    """用当前生效的文字模型跑一次抽取。

    不强制关闭思考模式：这里不需要 tool_choice，也就没有那个冲突；
    抽取在后台跑，多几百毫秒思考不影响任何人。
    """
    config = await resolve_text_model()
    if not config.configured:
        raise RuntimeError("模型未配置")

    llm = build_chat_model(
        config,
        # 抽取要的是「照实记录」，不是创作，温度压到接近 0。
        temperature=0.1,
        timeout=EXTRACT_TIMEOUT_SECONDS,
        max_retries=0,
    )
    reply = await llm.ainvoke([("system", system), ("human", human)])
    content = getattr(reply, "content", "")
    # 兼容多段内容（部分供应商把正文拆成 list）。
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


_caller: Callable[[str, str], Awaitable[str]] = _invoke_text_model


def set_caller_for_tests(fn: Callable[[str, str], Awaitable[str]] | None) -> None:
    """测试注入点：替换「问模型」这一步，解析与写库仍走真实代码。"""
    global _caller
    _caller = fn or _invoke_text_model


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _as_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # 没给就按「可合理推断」算：它已经通过了内容校验，不该因为是字符串被丢掉。
        return 0.7
    return max(0.0, min(1.0, number))


def parse_candidates(raw: str) -> list[memory_service.AutoMemory]:
    """把模型输出解析成候选。

    容错三件事，因为这一步失败等于这轮白跑：
    - 代码块围栏（```json ... ```）；
    - 顶层是对象还是数组；
    - 单个字段不合法（类型写中文、置信度是字符串、内容带换行）。
    """
    text = (raw or "").strip()
    if not text:
        return []

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    # 顶层可能是对象，也可能直接是数组：以先出现的那个括号为准，
    # 否则 `[{...}]` 会被当成 `{...}]` 拿去解析。
    first_obj, first_arr = text.find("{"), text.find("[")
    if first_obj == -1 and first_arr == -1:
        return []
    if first_arr != -1 and (first_obj == -1 or first_arr < first_obj):
        start, close = first_arr, "]"
    else:
        start, close = first_obj, "}"
    end = text.rfind(close)
    if start == -1 or end <= start:
        return []

    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        logger.warning(
            "记忆抽取返回的内容不是合法 JSON，已丢弃。",
            extra={"event": "memory_extract_bad_json"},
        )
        return []

    items = data.get("memories") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    out: list[memory_service.AutoMemory] = []
    seen: set[tuple[str, str]] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        if len(out) >= MAX_ITEMS:
            break

        memory_type = _TYPE_ALIASES.get(str(item.get("type", "")).strip().lower())
        if memory_type is None:
            # 类型不认识时按「事实」收下：总比把一条真信息丢了强。
            memory_type = MemoryType.FACT

        title = _clean(item.get("title"))[:MAX_TITLE]
        content = _clean(item.get("content"))[:MAX_CONTENT]
        confidence = _as_confidence(item.get("confidence"))
        if not title or not content or confidence < MIN_CONFIDENCE:
            continue

        key = (memory_type.value, title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            memory_service.AutoMemory(
                memory_type=memory_type,
                title=title,
                content=content,
                confidence=confidence,
            )
        )

    return out


async def extract_candidates(
    *,
    user_message: str,
    answer: str,
    known_titles: Sequence[str] = (),
) -> list[memory_service.AutoMemory]:
    """问一次模型，返回这一轮的候选记忆。"""
    payload = {
        "user_message": (user_message or "")[:MAX_MESSAGE_CHARS],
        "assistant_answer": (answer or "")[:MAX_ANSWER_CHARS],
        "known_memory_titles": list(known_titles)[:MAX_KNOWN_TITLES],
    }
    raw = await _caller(EXTRACT_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
    return parse_candidates(raw)


async def run_extraction(
    *,
    tenant_id: str,
    user_id: str,
    thread_id: str | None,
    user_message: str,
    answer: str,
) -> dict[str, object]:
    """跑一次抽取并写库。返回统计，供日志与测试断言。

    读记忆取标题（防重复）与写库开两个短 session：
    中间隔着一次网络调用，不该在这期间占着数据库连接。
    """
    if not settings.memory_enabled or not settings.memory_extract_enabled:
        return {"skipped": "disabled"}

    message = (user_message or "").strip()
    if len(message) < MIN_USER_CHARS:
        return {"skipped": "too_short"}

    try:
        with tenant_scope(tenant_id):
            known: list[str] = []
            async with async_session_factory() as session:
                rows, _ = await memory_service.list_memories(
                    session, user_id=user_id, limit=200
                )
                known = [row.title for row in rows if row.title]

            candidates = await extract_candidates(
                user_message=message,
                answer=answer,
                known_titles=known,
            )
            if not candidates:
                return {"candidates": 0}

            async with async_session_factory() as session:
                async with session.begin():
                    created, refreshed, skipped = await memory_service.upsert_extracted(
                        session,
                        user_id=user_id,
                        items=candidates,
                        source_thread_id=thread_id,
                    )
                    await memory_service.enforce_cap(session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - 抽取失败不能影响任何别的东西
        logger.warning(
            "记忆抽取失败，已忽略。",
            extra={
                "event": "memory_extract_failed",
                "error_category": type(exc).__name__,
                "error": str(exc)[:300],
            },
        )
        return {"error": type(exc).__name__}

    logger.info(
        "记忆抽取完成。",
        extra={
            "event": "memory_extracted",
            # 不能用 created / name 这类 LogRecord 保留字段做 extra 键。
            "new_count": created,
            "updated_count": refreshed,
            "skipped_count": skipped,
        },
    )
    return {
        "candidates": len(candidates),
        "created": created,
        "refreshed": refreshed,
        "skipped": skipped,
    }


# 后台任务要留引用：事件循环只持弱引用，任务被 GC 掉就会「静默不执行」。
_TASKS: set[asyncio.Task[None]] = set()


def schedule_extraction(
    *,
    tenant_id: str,
    user_id: str,
    thread_id: str | None,
    user_message: str,
    answer: str,
) -> None:
    """在回答落地之后排一次抽取。不等待，也不抛异常。"""
    if not settings.memory_enabled or not settings.memory_extract_enabled:
        return
    if len((user_message or "").strip()) < MIN_USER_CHARS:
        return

    try:
        task = asyncio.create_task(
            run_extraction(
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread_id,
                user_message=user_message,
                answer=answer,
            ),
            name="memory-extract",
        )
    except RuntimeError:
        # 没有运行中的事件循环（同步调用或解释器收尾阶段），跳过即可。
        return

    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
