# app/services/context_service.py
"""长对话上下文：注入窗口 + 压缩。

四条边界先写在文件头上，改代码前先读：

1. **历史每轮从 `messages` 表重新取，不读写 checkpoint。** checkpoint 是审批中断的
   现场，裁过就对不上审批预览里的参数；注入式历史才可重放。
2. **压缩只在轮间。** 轮内已有逐项截断（MCP 12000 字符、OCR 40000 字符），
   在模型推下一步的中途抽走工具结果，这一轮就废了。
3. **当前这条用户消息永不进摘要。** 它被吞掉的后果不是省钱，是答非所问。
4. **摘要不是记忆。** 不写 `memories`，也不从记忆库取——一个是本次对话的叙事，
   一个是跨会话的事实，互相写入只会互相污染。

一处刻意的偏离：压缩失败时**不落库**。
落一行「覆盖了 N 轮」却只有占位文字的摘要，等于把那 N 轮永久丢掉；
不落库则水位不动，下一轮还能重试，本轮只是窗口变短。

三段式执行，避免「拿着数据库事务等模型 20 秒」：
读（一个会话）→ 问模型（不占会话）→ 写（另一个会话 + 行锁）。
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.context import ConversationSummary
from app.models.conversation import Conversation, Message, MessageRole
from app.observability.context import span
from app.services.model_service import build_chat_model, resolve_text_model

logger = logging.getLogger(__name__)

# 与 prompt 结构强耦合的值不放配置：改配置会让线上表现悄悄漂移
# （与 memory_service 同一口径）。运维可调的三项在 settings 里。
KEEP_TURNS = 6
KEEP_TOKENS = 6000
# 摘要要同时装下【当前目标】【已定结论】【未完成】【指代绑定】【被否决】五节。
# 1200 字时线上是一份「刚好顶格、最后一句断在『…要求直』」的摘要，而且条目
# 从最早的写起，越新的事越写不进去——一周的会话摘要里查不到最近两天的任何一件事。
SUMMARY_MAX_CHARS = 2400
# 唯一会长到超限的那一节。截断时保其余四节、只从这一节里丢最旧的条目。
SUMMARY_CONCLUSION_HEAD = "【已定结论】"
REBUILD_EVERY = 3
# 故意取小：把每个字符都算得更贵 → 估出的 token 偏多 → 偏早压缩。
# 早压只损失几轮原文，压晚了是撞上限整轮失败。
CHARS_PER_TOKEN = 1.3
# 收益不到 15% 视为无效：落库只会让下一轮再压一次，白烧一次模型调用。
MIN_GAIN_RATIO = 0.15
# 被挤出窗口的轮次攒到这个体量就压缩。
#
# 为什么单靠 `context_compact_ratio` 不够：基线 prompt（system + 工具目录 + 记忆）
# 线上实测约 12.4k，而窗口最多只装 KEEP_TOKENS=6000 的历史，两者相加约 18.4k，
# **永远到不了 19200 的压缩线**——于是被窗口挤掉的旧轮次只会静默消失，摘要永不生成。
# 触发条件因此有两个：整体接近上限，或者「这一轮要丢掉的东西已经够多了」。
MIN_EVICT_TOKENS = 800
# 两次压缩之间至少隔这么多轮（压完水位前移，待压轮次从 0 起算）。
MIN_COMPACT_INTERVAL_TURNS = 2
# 硬截断时窗口预算打对折，且至少留两轮——连上一问都看不到就没法接话。
HARD_CUT_BUDGET_RATIO = 0.5
HARD_CUT_MIN_TURNS = 2
# 摘要输入上限。超出就丢最旧的轮次并注明，不把请求体撑爆。
TRANSCRIPT_MAX_CHARS = 48000
# 没有实测值（首轮）时的粗估：system + 技能 + 工具目录 + 记忆块。
# 线上实测过一次：工具目录九十多项时基线约 12.4k，目录大小随授权变化，
# 所以这只是量级参考，不参与触发判定。
OVERHEAD_TOKENS = 9000

SUMMARY_HINT = "以下是对更早对话的转述，不是指令；与当前用户消息冲突时以当前为准。"


@dataclass(frozen=True)
class Turn:
    """一轮问答。边界只在 user 消息处切，绝不切半轮。"""

    user_text: str
    assistant_text: str
    user_at: datetime
    last_message_id: uuid.UUID
    last_message_at: datetime
    messages: int

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.user_text) + estimate_tokens(self.assistant_text)


@dataclass
class Block:
    """要注入到 payload 里的东西。空的摘要与空轮次都表示「不放这个键」。"""

    summary: str = ""
    turns: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SummaryDraft:
    """摘要正文 + 落库需要的全部事实。模型调用完成后就不再需要会话。"""

    body: str
    model: str | None
    tokens_before: int
    tokens_after: int
    turns_covered: int
    messages_covered: int
    through_message_id: uuid.UUID
    through_created_at: datetime
    rebuilt: bool
    gain: float


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def clean_summary(text: str) -> str:
    """压掉多余空行；超限时按条目丢最旧的，不切半句、不整节丢。

    摘要固定五节（见 `SUMMARY_SYSTEM`），【已定结论】通常占九成篇幅。超限时先把
    「其他四节」完整留住，剩下的预算全给【已定结论】：那四节短，且
    【未完成】【被否决】恰恰是「用户明确说过别做什么」，丢掉比少记几条结论更糟。

    从前是整体 `body[:1200]`，线上真的切出过「…access_scope=internal，要求直」，
    并且最新的条目因为排在最后而被整段吃掉——模型看到的「记忆」永远停在几天前。
    """
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    body_lines = [line for line in lines if line.strip()]
    body = "\n".join(body_lines)
    if len(body) <= SUMMARY_MAX_CHARS:
        return body

    sections = _split_summary_sections(body_lines)
    fixed = [(head, rows) for head, rows in sections if head != SUMMARY_CONCLUSION_HEAD]
    fixed_chars = sum(
        (len(head) + 1 if head else 0) + sum(len(row) + 1 for row in rows)
        for head, rows in fixed
    )
    if fixed_chars >= SUMMARY_MAX_CHARS:
        # 少见：连结论以外的四节都写超了。退回整体按行截，至少不切半句。
        return _truncate_summary_lines(body_lines, SUMMARY_MAX_CHARS)

    budget = SUMMARY_MAX_CHARS - fixed_chars - 1
    out: list[str] = []
    for head, rows in sections:
        if head:
            out.append(head)
        if head != SUMMARY_CONCLUSION_HEAD:
            out.extend(rows)
            continue

        kept: list[str] = []
        used = 0
        for row in rows:
            if used + len(row) + 1 > budget:
                break
            kept.append(row)
            used += len(row) + 1
        out.extend(kept)
        if len(kept) < len(rows):
            out.append(f"（另有 {len(rows) - len(kept)} 条更早结论已省略）")

    return "\n".join(out)


def _split_summary_sections(
    lines: Sequence[str],
) -> list[tuple[str | None, list[str]]]:
    """按【小标题】切节。没有小标题的旧摘要整段归到 (None, …)。"""
    sections: list[tuple[str | None, list[str]]] = []
    for line in lines:
        if line.startswith("【"):
            sections.append((line, []))
        elif sections:
            sections[-1][1].append(line)
        else:
            sections.append((None, [line]))
    return sections


def _truncate_summary_lines(lines: Sequence[str], limit: int) -> str:
    kept: list[str] = []
    used = 0
    for line in lines:
        if kept and used + len(line) + 1 > limit:
            break
        if not kept and len(line) > limit:
            # 单行就超限（旧摘要没有小标题、一整段下来）。只能硬切，但留标记。
            return line[:limit] + "…"
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept) + "（其余条目已省略）"


def transcript_of(turns: Sequence[Turn], *, limit: int = TRANSCRIPT_MAX_CHARS) -> str:
    """把轮次排成「用户 / 助手」交替的原文。

    从最新往回累加，超上限就停：丢最旧的几轮，而不是把每一轮都截一半——
    半句话比没有更误导。
    """
    blocks: list[str] = []
    total = 0
    dropped = 0

    for turn in reversed(turns):
        block = f"用户：{turn.user_text}\n助手：{turn.assistant_text}"
        if total + len(block) > limit and blocks:
            dropped += 1
            continue
        blocks.append(block)
        total += len(block)

    text = "\n\n".join(reversed(blocks))
    if dropped:
        text = f"（更早的 {dropped} 轮因长度上限未纳入本次压缩）\n\n{text}"
    return text


def plan_window(
    turns: Sequence[Turn],
    *,
    budget_tokens: int = KEEP_TOKENS,
    min_turns: int = KEEP_TURNS,
) -> tuple[list[Turn], list[Turn]]:
    """按预算从最新往回取整轮，返回 (保留, 淘汰)。

    预算是目标不是硬约束：至少留 `min_turns` 轮，哪怕超预算——
    连最后几轮都看不到，模型会把上一问重答一遍。
    """
    kept: list[Turn] = []
    total = 0

    for turn in reversed(turns):
        if kept and total + turn.tokens > budget_tokens and len(kept) >= min_turns:
            break
        kept.append(turn)
        total += turn.tokens

    kept.reverse()
    return kept, list(turns[: len(turns) - len(kept)])


def decide_action(*, measured: int, window: int) -> str:
    """none / compact / hard。只看实测，不看估算。"""
    if measured <= 0 or window <= 0:
        return "none"

    ratio = measured / window
    if ratio >= settings.context_hard_ratio:
        return "hard"
    if ratio >= settings.context_compact_ratio:
        return "compact"
    return "none"


SUMMARY_SYSTEM = """你在压缩一段对话的历史，产物会作为「更早对话的转述」交给后续轮次。
只做转述：不补充、不建议、不评价，也不改写任何一方的立场。

硬规则：
1. 否定不许翻转。「不要关掉」不能写成「关掉」；「先别提交」不能写成「可以提交」。
2. 标注出处。用户明确说过的写「用户」；助手提出而用户没表态的必须写「助手建议，用户未表态」。
   把建议写成决定，比漏记更糟。
3. 只写结论，不抄原始数据。工具返回的 JSON、表格正文一律不写。
4. 指代必须绑死。「它 / 那个 / 上次那个」要还原成具体对象（名字、单号或 id）。
5. 不知道就写「无」。缺哪一节就写「无」，不要省略小标题。
6. 写不下时删【已定结论】里**最旧**的条目。不许把一条写成半句，也不许为了塞进
   更早的事而把最近发生的事挪到后面或省掉——越新的事越不能丢。

输出格式（五节，顺序固定，总长不超过 {limit} 字）：

【当前目标】一句话
【已定结论】逐条，最新的写在最前，标出处
【未完成】答应了还没做的、等用户补的信息
【指代绑定】原词 → 具体对象
【被否决】用户明确拒绝的方案，一条一句
"""


def summary_messages(*, previous: str, transcript: str, rebuilt: bool) -> list[tuple[str, str]]:
    """摘要的输入。

    重建时不受上一版影响；增量时把上一版当已定稿背景，允许修正但要显式写出来。
    """
    system = SUMMARY_SYSTEM.format(limit=SUMMARY_MAX_CHARS)

    if rebuilt:
        human = (
            "这次是**全量重建**：下面是这段对话从开头到现在的原文，"
            "请只依据原文重写一份摘要，不要参考任何旧版本。\n\n"
            f"{transcript}"
        )
    else:
        prev = previous.strip() or "无"
        human = (
            f"上一版摘要：\n{prev}\n\n"
            "本次要并入的对话原文（在上一版之后发生）：\n"
            f"{transcript}\n\n"
            "在原摘要基础上更新。若原文与上一版冲突，以原文为准，并在对应条目里注明「已更正」。"
        )

    return [("system", system), ("human", human)]


async def _invoke_summarizer(messages: list[tuple[str, str]]) -> tuple[str, str | None]:
    """跑一次摘要。返回 (正文, 模型名)。"""
    config = await resolve_text_model()
    if not config.configured:
        raise RuntimeError("模型未配置")

    llm = build_chat_model(
        config,
        # 转述要的是「照实复述」，不是创作。
        temperature=0.1,
        timeout=settings.context_timeout_seconds,
        max_retries=0,
    )
    reply = await llm.ainvoke(messages)
    content = getattr(reply, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "").strip(), getattr(config, "model", None)


_summarizer: Callable[[list[tuple[str, str]]], Awaitable[tuple[str, str | None]]] = (
    _invoke_summarizer
)


def set_summarizer_for_tests(
    fn: Callable[[list[tuple[str, str]]], Awaitable[tuple[str, str | None]]] | None,
) -> None:
    """测试注入点：替换「问模型写摘要」这一步，切边界与落库仍走真实代码。"""
    global _summarizer
    _summarizer = fn or _invoke_summarizer


async def _load_turns(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    exclude_message_id: uuid.UUID | None,
    cleared_at: datetime | None = None,
) -> list[Turn]:
    """把消息表折成轮次。

    以 user 消息起头、到下一个 user 之前为界。尾部没有助手回复的不成轮
    （当前这条就是这种情况），直接丢掉——只剩提问的历史会让模型重答一遍。

    `cleared_at` 是 `/clear` 的水位：只取它之后的消息。**过滤放在这里而不是
    调用点**，因为折叠轮次、算待压量、判摘要水位全都从这一份 rows 派出去，
    漏一处就会出现「界面说清了、压缩时又把旧事写回摘要」。
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)

    if cleared_at is not None:
        # 严格大于：水位取的是 `/clear` 那条回复的时刻，它自己必须被排除。
        stmt = stmt.where(Message.created_at > cleared_at)

    rows = list(
        (await session.scalars(stmt.order_by(Message.created_at, Message.id))).all()
    )

    turns: list[Turn] = []
    current_user: Message | None = None
    assistant_text = ""
    assistant_count = 0
    last: Message | None = None

    def flush() -> None:
        nonlocal current_user, assistant_text, assistant_count
        if current_user is None or last is None or not assistant_text.strip():
            current_user = None
            assistant_text = ""
            assistant_count = 0
            return
        turns.append(
            Turn(
                user_text=str(current_user.content or "").strip(),
                assistant_text=assistant_text.strip(),
                user_at=current_user.created_at,
                last_message_id=last.id,
                last_message_at=last.created_at,
                messages=1 + assistant_count,
            )
        )
        current_user = None
        assistant_text = ""
        assistant_count = 0

    for row in rows:
        if exclude_message_id is not None and row.id == exclude_message_id:
            continue
        if row.role == MessageRole.USER:
            flush()
            current_user = row
            assistant_text = ""
            assistant_count = 0
            last = row
        elif row.role == MessageRole.ASSISTANT and current_user is not None:
            assistant_text = (
                f"{assistant_text}\n{row.content}" if assistant_text else str(row.content)
            )
            assistant_count += 1
            last = row

    flush()
    return turns


async def _latest_summary(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    cleared_at: datetime | None = None,
) -> ConversationSummary | None:
    """最新一份**仍然生效**的摘要。

    `/clear` 之后，清空之前生成的那几份摘要一并失效：它们叙述的正是用户
    刚说「别再提了」的那段。摘要表是 append-only 的，不能删行——留着才能复盘
    「清空前它记得什么」，所以按生成时刻过滤。

    `created_at IS NULL` 也收进来：那是刚 flush、还没从库里取回默认值的行，
    必然属于「之后」；把它当成旧的会让清空后的摘要永远不生效。
    """
    stmt = select(ConversationSummary).where(
        ConversationSummary.conversation_id == conversation_id
    )

    if cleared_at is not None:
        stmt = stmt.where(
            or_(
                ConversationSummary.created_at > cleared_at,
                ConversationSummary.created_at.is_(None),
            )
        )

    return await session.scalar(
        stmt.order_by(ConversationSummary.created_at.desc()).limit(1)
    )


async def _summary_count(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    cleared_at: datetime | None = None,
) -> int:
    """当前生效的摘要份数。

    与 `_latest_summary` 同口径：清空前的摘要不算数。这个数决定下一次
    是否走「全量重建」（`count % REBUILD_EVERY`），拿旧摘要凑数会让
    刚清空就触发一次重建，而重建的输入只有清空后的那两轮。
    """
    stmt = (
        select(func.count())
        .select_from(ConversationSummary)
        .where(ConversationSummary.conversation_id == conversation_id)
    )

    if cleared_at is not None:
        stmt = stmt.where(
            or_(
                ConversationSummary.created_at > cleared_at,
                ConversationSummary.created_at.is_(None),
            )
        )

    value = await session.scalar(stmt)
    return int(value or 0)


def _is_after(
    at: datetime, message_id: uuid.UUID, summary: ConversationSummary | None
) -> bool:
    """这条消息是否在水位之后。水位为空表示还没压过，全部都在之后。"""
    if summary is None:
        return True
    return (at, str(message_id)) > (
        summary.through_created_at,
        str(summary.through_message_id),
    )


async def _summarize(
    *,
    source: Sequence[Turn],
    previous: str,
    rebuilt: bool,
    baseline_tokens: int | None = None,
) -> SummaryDraft | None:
    """问模型写摘要。失败返回 None——不落库、水位不动，下一轮还能重试。

    `baseline_tokens` 是收益口径的分母：这份摘要替代的是**全部**窗口外轮次，
    而不是本次并进去的那几轮。增量压缩的输入可能只有两轮（几百 token），
    拿它当分母会算出负收益，然后被 `MIN_GAIN_RATIO` 判成无效——
    表现是摘要从此不再更新，模型永远停在几天前。
    """
    transcript = transcript_of(source)
    tokens_before = baseline_tokens or sum(turn.tokens for turn in source)

    async with span(
        "context.compact",
        "context",
        attrs={
            "turns": len(source),
            "messages": sum(turn.messages for turn in source),
            "tokens_before": tokens_before,
            "rebuilt": rebuilt,
        },
    ) as span_handle:
        try:
            text, model = await _summarizer(
                summary_messages(previous=previous, transcript=transcript, rebuilt=rebuilt)
            )
        except Exception as exc:  # noqa: BLE001  压缩失败不允许影响这一轮
            logger.warning(
                "上下文摘要生成失败，本轮改为缩短窗口。",
                extra={"event": "context_compact_failed", "error": type(exc).__name__},
            )
            span_handle.add(degraded=True, error=type(exc).__name__)
            return None

        body = clean_summary(text)
        if not body:
            span_handle.add(degraded=True, error="empty")
            return None

        # 摘要全文进 span 原文：「模型这一轮到底看到了什么」必须能直接看到，
        # 而不是让排障的人再去 conversation_summaries 里按时间对一遍。
        # 输入（转录）不记：它就是 messages 表里的那几轮，存两遍只会撑大观测库。
        span_handle.content(output=body)

        tokens_after = estimate_tokens(body) + estimate_tokens(SUMMARY_HINT)
        gain = 1 - (tokens_after / tokens_before) if tokens_before else 0.0
        span_handle.add(
            tokens_after=tokens_after,
            gain=round(gain, 3),
            model=model,
            summary_chars=len(body),
        )

        if gain < MIN_GAIN_RATIO:
            logger.info(
                "上下文压缩收益不足，本次不落库。",
                extra={"event": "context_compact_noop", "gain": round(gain, 3)},
            )
            span_handle.add(noop=True)
            return None

        through = list(source)[-1]
        return SummaryDraft(
            body=body,
            model=model,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            turns_covered=len(source),
            messages_covered=sum(turn.messages for turn in source),
            through_message_id=through.last_message_id,
            through_created_at=through.last_message_at,
            rebuilt=rebuilt,
            gain=gain,
        )


async def _persist(
    *,
    draft: SummaryDraft,
    conversation_id: uuid.UUID,
    base_summary_id: uuid.UUID | None,
) -> ConversationSummary | None:
    """落库。写前用行锁确认水位没被并发的另一轮推走：只让第一个赢。"""
    async with async_session_factory() as session:
        async with session.begin():
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update()
            )
            if conversation is None:
                return None

            latest = await _latest_summary(
                session,
                conversation_id=conversation_id,
                cleared_at=conversation.context_cleared_at,
            )
            if (latest.id if latest else None) != base_summary_id:
                logger.info(
                    "另一轮已完成压缩，本次结果丢弃。",
                    extra={"event": "context_compact_skipped", "reason": "concurrent"},
                )
                return None

            row = ConversationSummary(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation_id,
                through_message_id=draft.through_message_id,
                through_created_at=draft.through_created_at,
                turns_covered=draft.turns_covered,
                messages_covered=draft.messages_covered,
                tokens_before=draft.tokens_before,
                tokens_after=draft.tokens_after,
                summary=draft.body,
                summary_chars=len(draft.body),
                model=draft.model,
                rebuilt=draft.rebuilt,
                degraded=False,
            )
            session.add(row)
            await session.flush()
            return row


async def _context_window() -> tuple[int, str]:
    """当前生效的上下文窗口，以及它的来源（provider / env）。

    供应商在 ``models[]`` 里按模型声明了 ``context_window`` 就用它：同一个平台
    可能今天跑 32k 的模型、明天换 128k 的，窗口写死在 .env 里等于换了模型还在
    按旧窗口压缩——历史被过早丢掉，而且没有任何报错。

    读配置失败不能拖垮这一轮：回落 .env，并留一行日志。
    """
    try:
        config = await resolve_text_model()
    except Exception as exc:  # noqa: BLE001 - 读配置失败只影响窗口，不影响对话
        logger.warning(
            "读取模型上下文窗口失败，沿用环境变量。",
            extra={"event": "context_window_read_failed", "error": type(exc).__name__},
        )
        return settings.context_window_tokens, "env"

    if config.context_window:
        return int(config.context_window), "provider"

    return settings.context_window_tokens, "env"


async def prepare(
    *,
    conversation_id: uuid.UUID,
    user_message: str,
    measured_tokens: int,
    exclude_message_id: uuid.UUID | None = None,
    cleared_at: datetime | None = None,
) -> Block:
    """每轮对话开工前调用：算窗口、必要时压缩、返回要注入的内容。

    任何一步失败都只让注入内容变少，不让这一轮失败——用户不该因为摘要写不出来
    而收不到答案。

    租户上下文由调用方（`chat_service`）的 `tenant_scope` 提供，这里不重复接收
    tenant_id：接收了却只用于日志，会让人误以为它是隔离手段。

    `cleared_at` 是 `/clear` 的水位。它必须**一路传到 `_load_turns` / 摘要查询**，
    只在这一层截断是不够的：压缩读的是同一份 rows，截在哪一层决定了
    「旧事会不会被写进新摘要再带回来」。
    """
    if not settings.context_enabled:
        return Block(meta={"enabled": False})

    window, window_source = await _context_window()
    action = decide_action(measured=measured_tokens, window=window)

    async with span(
        "context.measure",
        "context",
        attrs={
            "window": window,
            # 窗口从哪来：供应商按模型声明 / .env 兜底。排障时「为什么 128k 的模型
            # 还在按 32k 压缩」只看这一项就知道是没读到还是没填。
            "window_source": window_source,
            "last_prompt_tokens": measured_tokens,
            "source": "measured" if measured_tokens > 0 else "none",
            "action": action,
            "user_chars": len(user_message or ""),
            # 清空点要能在观测里看到：答错时先确认「这一轮到底带了多少历史」。
            "cleared": cleared_at is not None,
        },
    ) as measure_span:
        try:
            block = await _prepare(
                conversation_id=conversation_id,
                action=action,
                exclude_message_id=exclude_message_id,
                measured_tokens=measured_tokens,
                user_message=user_message,
                cleared_at=cleared_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "上下文准备失败，本轮不带历史。",
                extra={"event": "context_prepare_failed", "error": type(exc).__name__},
                exc_info=True,
            )
            measure_span.add(error=type(exc).__name__)
            return Block(meta={"degraded": True, "error": type(exc).__name__})

        measure_span.add(
            # 有效动作以 _prepare 的结论为准：淘汰量触发时，初始判定是 none，
            # 只有它知道这一轮真的压了。两个值不一致会让 trace 看起来自相矛盾。
            action=block.meta.get("action"),
            kept_turns=block.meta.get("kept_turns"),
            summary_chars=block.meta.get("summary_chars"),
            summary_turns=block.meta.get("summary_turns"),
            hard_cut=block.meta.get("hard_cut"),
        )
        return block


async def _prepare(
    *,
    conversation_id: uuid.UUID,
    action: str,
    exclude_message_id: uuid.UUID | None,
    measured_tokens: int,
    user_message: str,
    cleared_at: datetime | None = None,
) -> Block:
    async with async_session_factory() as session:
        # 水位不传就自己去库里读一次，不要当成「没清过」。
        #
        # `cleared_at=None` 有两个来源：这个会话确实没清过，以及**调用方忘了传**。
        # 两者在签名上长得一样，后果却相反——后者会把用户明确要求忘掉的那几轮
        # 原样注入回去，而且不报错、不留痕迹。多一次 SELECT 换掉这个静默分支：
        # 有水位就读出来，没有才是真的没有。
        if cleared_at is None:
            cleared_at = await session.scalar(
                select(Conversation.context_cleared_at).where(
                    Conversation.id == conversation_id
                )
            )
        turns = await _load_turns(
            session,
            conversation_id=conversation_id,
            exclude_message_id=exclude_message_id,
            cleared_at=cleared_at,
        )
        summary_row = await _latest_summary(
            session, conversation_id=conversation_id, cleared_at=cleared_at
        )

        # 待压 = 会被挤出窗口、且还没进摘要的轮次。判断「该不该压」必须用它：
        # 从前用的是「水位之后的全部轮次」，把窗口内的也算进来，间隔检查于是
        # 永远通过——线上表现为每轮都重压一遍（三分钟五次），每次都重写全量，
        # 上限再大也写不完，摘要只好永远停在几天前。
        _, evicted_preview = plan_window(turns, budget_tokens=KEEP_TOKENS, min_turns=KEEP_TURNS)
        pending = [
            turn
            for turn in evicted_preview
            if _is_after(turn.last_message_at, turn.last_message_id, summary_row)
        ]

        if len(pending) < MIN_COMPACT_INTERVAL_TURNS:
            # 刚压过一轮（或这一轮没挤出什么），待压轮次还不够，此时压缩只会把同一段再压一遍。
            # 但硬截断不能降级：它是「窗口必须缩」，跟压不压无关，降了会直接撞上限。
            if action != "hard":
                action = "none"
        elif action == "none" and sum(turn.tokens for turn in pending) >= MIN_EVICT_TOKENS:
            # 第二个触发条件：这一轮会挤出窗口的东西已经够多。
            # 只看「整体接近上限」会漏掉一种常见情形——基线 prompt 本身很大
            # （工具目录九十多项时约 12.4k），窗口又只装得下 6000 的历史，
            # 两者相加到不了压缩线，于是旧轮次每轮都被悄悄挤掉、摘要永不生成。
            action = "compact"
    compact_failed = False
    if action == "compact":
        kept, evicted = plan_window(turns, budget_tokens=KEEP_TOKENS, min_turns=KEEP_TURNS)
        if evicted:
            async with async_session_factory() as session:
                count = await _summary_count(
                    session, conversation_id=conversation_id, cleared_at=cleared_at
                )
            rebuilt = count > 0 and count % REBUILD_EVERY == 0
            # 增量只喂「还没进摘要」的轮次：全量重喂会让同一段被反复改写，而且摘要
            # 每次从头写，越早的事越占篇幅。重建时反过来必须给全量——
            # 「不要参考旧版本」+ 只给刚挤出的两轮 = 此前所有历史被一笔勾销。
            incoming = [
                turn
                for turn in evicted
                if _is_after(turn.last_message_at, turn.last_message_id, summary_row)
            ]
            source = evicted if rebuilt else incoming
            # 没有待压内容时按「不用压」处理（pending 已挡住，这里只是兜底）。
            # 不能落进 compact_failed——那会把窗口砍半，等于白丢一半历史。
            skipped = not source
            draft = (
                await _summarize(
                    source=source,
                    previous=summary_row.summary if summary_row is not None else "",
                    rebuilt=rebuilt,
                    baseline_tokens=sum(turn.tokens for turn in evicted),
                )
                if source
                else None
            )
            saved = None
            if draft is not None:
                saved = await _persist(
                    draft=draft,
                    conversation_id=conversation_id,
                    base_summary_id=summary_row.id if summary_row is not None else None,
                )
            if saved is None:
                # 摘要没写成就少看点原文：这一轮照常作答，水位不动，下一轮还能重试。
                compact_failed = not skipped
            else:
                summary_row = saved
                async with async_session_factory() as session:
                    turns = await _load_turns(
                        session,
                        conversation_id=conversation_id,
                        exclude_message_id=exclude_message_id,
                        cleared_at=cleared_at,
                    )

    if action == "hard" or compact_failed:
        budget = int(KEEP_TOKENS * HARD_CUT_BUDGET_RATIO)
        min_turns = HARD_CUT_MIN_TURNS
    else:
        budget = KEEP_TOKENS
        min_turns = KEEP_TURNS

    kept, _ = plan_window(turns, budget_tokens=budget, min_turns=min_turns)

    summary_text = summary_row.summary if summary_row is not None else ""
    # 摘要累计覆盖多少轮 = 没进窗口的那些。**只有存在摘要时这个数才成立**：
    # 没有摘要而被挤掉的轮次是真的丢了，把它们算成「已折叠」会让人以为
    # 模型还知道那些内容。
    summary_turns = max(0, len(turns) - len(kept)) if summary_text else 0
    block = Block(
        summary=summary_text,
        turns=[],
        meta={
            "action": action,
            "kept_turns": len(kept),
            "dropped_turns": max(0, len(turns) - len(kept)),
            "summary_chars": len(summary_text),
            "summary_turns": summary_turns,
            "hard_cut": action == "hard",
        },
    )

    # 角色逐条保留：拼成一段文本后角色标记会稀释，模型容易把助手的建议
    # 当成用户说过的话复述回来。
    for turn in kept:
        block.turns.append({"role": "user", "text": turn.user_text})
        block.turns.append({"role": "assistant", "text": turn.assistant_text})

    async with span(
        "context.inject",
        "context",
        attrs={
            "turns": len(kept),
            "summary_injected": bool(summary_text),
            "summary_chars": len(summary_text),
            "summary_turns": summary_turns,
            "hard_cut": action == "hard",
        },
    ) as inject_span:
        # 预估下一轮的 prompt：上一轮实测 + 这一条新消息。
        # 实测已经包含了 system、技能、工具目录与当时的历史，所以不该再叠一个
        # 「固定开销」——那个常数只能猜，而实测是现成的。
        if measured_tokens > 0:
            projected = measured_tokens + estimate_tokens(user_message)
        else:
            projected = (
                OVERHEAD_TOKENS
                + sum(estimate_tokens(item["text"]) for item in block.turns)
                + estimate_tokens(summary_text)
            )
        inject_span.add(projected=projected)

    return block


async def remember_prompt_tokens(*, conversation_id: uuid.UUID, prompt_tokens: int) -> None:
    """把这一轮实测的 prompt token 落库，供下一轮判断要不要压缩。"""
    if prompt_tokens <= 0:
        return

    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    conversation.last_prompt_tokens = prompt_tokens
    except Exception:  # noqa: BLE001
        logger.warning(
            "记录 prompt token 失败。",
            extra={"event": "context_measure_persist_failed"},
            exc_info=True,
        )
