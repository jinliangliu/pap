# app/services/memory_dream.py
"""梦境（dreaming）：离线校验自动抽取的记忆。

背景：`memory_extract` 在每轮对话结束后抽取候选，但抽取只看**那一轮**。
它无法发现三类问题：

1. 抽取时写错了（把计划写成已完成、把助手建议写成用户决定）；
2. 用户后来改口了，旧记忆还在；
3. 同一条事实攒成了两条标题不同的记忆。

本模块用「原始消息」作为 ground truth 跑一次梦境整理。参考 Anthropic
《The components of agent memory》里的 dreaming：不靠模型「记得更准」，
而是让它事后**对照 transcript 检查自己的笔记**。

五条边界：

1. 只整理 `source=auto_extract` 且 `is_user_confirmed=False` 的候选。
   用户确认过或手写的记忆一律不碰——改口是人的判断，模型没有这个权限。
2. 证据只取 **user 消息**。助手自己的话不能证明用户说过什么；
   拿模型输出当证据等于自证。
3. 模型只产出建议，不直接改库。`apply_review` 由接口按人的确认执行。
4. 证据不足一律 `keep`，不落库、不打扰人。宁可不动，不可猜。
5. 任何一条失败都不影响整批；模型未配置 / 超时 / 输出非法时跳过该条。

为什么证据取 `messages` 而不是 `obs_traces`：观测可以被 `OBS_ENABLED`
整体关掉、还有保留期，靠它做长期纠错会在某天突然「没有证据」。
会话消息是业务数据，生命周期与记忆本身一致。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.conversation import Conversation, Message, MessageRole
from app.models.memory import (
    Memory,
    MemoryReview,
    MemoryReviewAction,
    MemoryReviewStatus,
    MemoryStatus,
    TenantMemorySettings,
)
from app.services import memory_service
from app.services.model_service import build_chat_model, resolve_text_model

logger = logging.getLogger(__name__)

# 换 prompt 必须 +1。建议表里存了版本号，回看时才能解释「为什么同一批数据结论变了」。
PROMPT_VERSION = "2"

# 单次梦境的默认条数上限。脚本手动触发，跑太多会让一次调用串行等很久。
MAX_MEMORIES_PER_RUN = 20

# 每条记忆最多带几条证据。文章的做法是把相关 transcript 片段摆出来让模型对照，
# 太多片段反而稀释重点；6 条足够覆盖「原话 + 后续改口」。
MAX_EVIDENCE = 6

# 证据摘录长度。记忆本身最多 2000 字，证据只需要能判断语义的那一段。
MAX_EVIDENCE_CHARS = 400

# 近 N 天最多扫多少条 user 消息。全表扫在消息量大时不可接受，
# 而且越久远的消息越不可能是「当前仍然成立」的证据。
EVIDENCE_SCAN_LIMIT = 300

# 低于这个置信度的动作一律降级为 keep：拿不准的建议比没有建议更烦人。
MIN_ACTION_CONFIDENCE = 0.6

DREAM_TIMEOUT_SECONDS = 20.0

MAX_REASON_CHARS = 500
MAX_TITLE_CHARS = 256
MAX_CONTENT_CHARS = 2000


class DreamingError(Exception):
    """梦境链路的业务异常。"""


class DreamingUnavailable(DreamingError):
    """模型未配置或调用不可用。"""


class ReviewConflict(DreamingError):
    """建议已处理，或它指向的记忆已经不再满足应用条件。"""


@dataclass(frozen=True)
class Evidence:
    message_id: str
    conversation_id: str
    excerpt: str
    created_at: str
    # 是否与记忆有词面交集。未匹配的消息只作时间背景，
    # prompt 里会明确写「不得作为事实依据」。
    matched: bool = True


@dataclass(frozen=True)
class ReviewProposal:
    action: MemoryReviewAction
    reason: str
    confidence: float
    proposed_title: str | None = None
    proposed_content: str | None = None
    merge_target_id: uuid.UUID | None = None
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class DreamReport:
    scanned: int = 0
    suggested: int = 0
    kept: int = 0
    skipped_pending: int = 0
    failed: int = 0
    elapsed_ms: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "suggested": self.suggested,
            "kept": self.kept,
            "skipped_pending": self.skipped_pending,
            "failed": self.failed,
            "elapsed_ms": self.elapsed_ms,
            "items": self.items,
        }


DREAM_SYSTEM_PROMPT = """你负责为「梦境」核对一条「从历史对话自动抽取、尚未经用户确认」的长期记忆。

任务不是润色，而是判断它在给定证据下是否仍然成立。

四类动作，只能选一个：
- keep：证据不足，或用户原话支持这条记忆；不改。
- correct：记忆与用户原话不符（写错、夸大、把计划写成已完成、把助手的建议写成用户的决定），给出改写后的 title 与 content。
- merge：这条与 sibling_memories 里的另一条说同一件事，给出 merge_target_id。
- retire：用户后来明确改口或否定了它，或它已过期失效；建议下架，不删除。

硬规则：
1. 只能依据给定证据，不得用常识补全，不得引用证据之外的信息。
2. 用户没有明确说过的事，不能因为「合理」就写成事实。
3. 不得把失败、计划、建议描述成已完成或已确认。
4. 不得写入密钥、口令、令牌等凭据；发现就 retire 并说明。
5. evidence 里 matched=false 的消息只作时间背景，不得作为事实依据。
6. 证据不足一律 keep，reason 写「证据不足」。宁可不动，不可猜。
7. 这条记忆尚未被用户确认；你的建议仍需人工确认后才生效。

只输出 JSON，不要代码块，不要解释：
{"action":"keep","reason":"一句话说明依据","title":"","content":"","merge_target_id":"","confidence":0.8}

字段要求：
- reason：不超过 80 字，必须点明依据了哪条证据；
- title：correct 时必填，3-12 字名词短语；
- content：correct 时必填，一句话、第三人称、40 字以内；
- merge_target_id：merge 时必填，只能取 sibling_memories 里的 id；
- confidence：0-1，证据直接支持给 0.9，推断给 0.7，拿不准给 0.5 以下。"""

_ACTION_ALIASES: dict[str, MemoryReviewAction] = {
    "correct": MemoryReviewAction.CORRECT,
    "纠正": MemoryReviewAction.CORRECT,
    "改写": MemoryReviewAction.CORRECT,
    "merge": MemoryReviewAction.MERGE,
    "合并": MemoryReviewAction.MERGE,
    "retire": MemoryReviewAction.RETIRE,
    "下架": MemoryReviewAction.RETIRE,
    "过期": MemoryReviewAction.RETIRE,
    "keep": MemoryReviewAction.KEEP,
    "保留": MemoryReviewAction.KEEP,
    "不变": MemoryReviewAction.KEEP,
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _now() -> datetime:
    return datetime.now(UTC)


def _clean(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def _keywords(text: str) -> set[str]:
    """中文 bigram + 英文/数字词。不引入分词器，避免与检索侧的词表耦合。"""
    lowered = (text or "").lower()
    out = set(_ASCII_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        for index in range(len(run) - 1):
            out.add(run[index : index + 2])
    return out


def _memory_keywords(memory: Memory) -> set[str]:
    keys = _keywords(memory.title) | _keywords(memory.content)
    # 标题短且信息密度高，补上单字，避免「称呼」这类两字标题只能靠 bigram 命中。
    keys |= set(_CJK_RUN.findall((memory.title or "").lower()))
    return keys


def _score(text: str, keys: set[str]) -> int:
    return len(keys & _keywords(text))


async def collect_evidence(
    session: AsyncSession,
    memory: Memory,
    *,
    days: int = 30,
    limit: int = MAX_EVIDENCE,
) -> list[Evidence]:
    """从该用户的会话消息里找证据。

    来源 thread 的消息优先但最多 2 条：抽取本身就是从那条 thread 来的，
    全看它等于让模型自证。其余名额留给后续对话——「改口」通常发生在后面。
    """
    if not memory.user_id:
        return []

    since = _now() - timedelta(days=max(1, days))
    conditions = [
        Conversation.user_id == memory.user_id,
        Message.role == MessageRole.USER,
        Message.created_at >= since,
    ]
    rows = (
        await session.execute(
            select(Message, Conversation)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(and_(*conditions))
            .order_by(Message.created_at.desc())
            .limit(EVIDENCE_SCAN_LIMIT)
        )
    ).all()

    keys = _memory_keywords(memory)
    source_thread = (memory.source_thread_id or "").strip()

    matched: list[tuple[int, int, Message, Conversation]] = []
    for message, conversation in rows:
        text = message.content or ""
        score = _score(text, keys)
        if score <= 0:
            continue
        # 来源 thread 加权，但不压过明确的词面命中（最多 +3）。
        in_source = 1 if source_thread and conversation.thread_id == source_thread else 0
        matched.append((score + 3 * in_source, in_source, message, conversation))

    matched.sort(key=lambda item: (item[0], item[2].created_at), reverse=True)

    picked: list[tuple[Message, Conversation, bool]] = []
    seen: set[uuid.UUID] = set()
    source_count = 0
    for _, in_source, message, conversation in matched:
        if len(picked) >= limit:
            break
        if message.id in seen:
            continue
        if in_source and source_count >= 2:
            continue
        seen.add(message.id)
        source_count += in_source
        picked.append((message, conversation, True))

    # 词面完全没有交集时，仍给模型几条最近的背景消息，让它能明确说「证据不足」。
    # 这些消息标记 matched=false，prompt 禁止把它们当作事实依据。
    if len(picked) < 2:
        for message, conversation in rows[: 3 - len(picked)]:
            if message.id in seen:
                continue
            seen.add(message.id)
            picked.append((message, conversation, False))

    return [
        Evidence(
            message_id=str(message.id),
            conversation_id=str(conversation.id),
            excerpt=_clean(message.content, MAX_EVIDENCE_CHARS),
            created_at=(message.created_at or _now()).isoformat(),
            matched=is_matched,
        )
        for message, conversation, is_matched in picked
    ]


def _build_human(
    memory: Memory,
    evidence: Sequence[Evidence],
    siblings: Sequence[Memory],
) -> str:
    payload = {
        "memory": {
            "id": str(memory.id),
            "memory_type": memory.memory_type.value,
            "title": memory.title,
            "content": memory.content,
            "confidence": memory.confidence,
            "created_at": memory.created_at.isoformat() if memory.created_at else "",
        },
        "sibling_memories": [
            {
                "id": str(row.id),
                "title": row.title,
                "content": row.content,
                "memory_type": row.memory_type.value,
            }
            for row in siblings
        ],
        "evidence": [
            {
                "message_id": item.message_id,
                "conversation_id": item.conversation_id,
                "created_at": item.created_at,
                "matched": item.matched,
                "excerpt": item.excerpt,
            }
            for item in evidence
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _as_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def parse_proposal(
    raw: str,
    *,
    allowed_merge_ids: set[uuid.UUID],
    evidence: Sequence[Evidence] = (),
) -> ReviewProposal:
    """把模型输出解析成建议。

    所有「不合法」都降级为 keep 而不是抛异常：梦境是后台批量任务，
    一条输出坏了不该让整批停下。降级原因写进 reason，页面上能看见。
    """
    data = _extract_json_object(raw)
    if data is None:
        return ReviewProposal(
            action=MemoryReviewAction.KEEP,
            reason="模型输出不是合法 JSON，已跳过。",
            confidence=0.0,
            evidence=list(evidence),
        )

    action = _ACTION_ALIASES.get(str(data.get("action", "")).strip().lower())
    reason = _clean(data.get("reason"), MAX_REASON_CHARS)
    confidence = _as_confidence(data.get("confidence"))

    if action is None:
        return ReviewProposal(
            action=MemoryReviewAction.KEEP,
            reason=reason or "动作无法识别，按保留处理。",
            confidence=0.0,
            evidence=list(evidence),
        )

    if confidence < MIN_ACTION_CONFIDENCE:
        return ReviewProposal(
            action=MemoryReviewAction.KEEP,
            reason=reason or "置信度不足，按保留处理。",
            confidence=confidence,
            evidence=list(evidence),
        )

    proposed_title = _clean(data.get("title"), MAX_TITLE_CHARS) or None
    proposed_content = _clean(data.get("content"), MAX_CONTENT_CHARS) or None

    if action == MemoryReviewAction.CORRECT:
        if not proposed_title or not proposed_content:
            return ReviewProposal(
                action=MemoryReviewAction.KEEP,
                reason="建议改写但缺少标题或内容，按保留处理。",
                confidence=confidence,
                evidence=list(evidence),
            )

    merge_target: uuid.UUID | None = None
    if action == MemoryReviewAction.MERGE:
        raw_target = str(data.get("merge_target_id") or "").strip()
        try:
            merge_target = uuid.UUID(raw_target)
        except (ValueError, AttributeError):
            merge_target = None
        # 只接受本次给过的那几个 id。模型编一个不存在的 id 是常见幻觉，
        # 直接应用会把两条无关记忆合并掉。
        if merge_target not in allowed_merge_ids:
            return ReviewProposal(
                action=MemoryReviewAction.KEEP,
                reason="合并目标不在候选列表内，按保留处理。",
                confidence=confidence,
                evidence=list(evidence),
            )

    return ReviewProposal(
        action=action,
        reason=reason or "未给出理由。",
        confidence=confidence,
        proposed_title=proposed_title,
        proposed_content=proposed_content,
        merge_target_id=merge_target,
        evidence=list(evidence),
    )


async def _invoke_text_model(system: str, human: str) -> str:
    config = await resolve_text_model()
    if not config.configured:
        raise DreamingUnavailable("模型未配置")

    llm = build_chat_model(
        config,
        # 梦境要的是「照实判断」，温度压到接近 0。
        temperature=0.1,
        timeout=DREAM_TIMEOUT_SECONDS,
        max_retries=0,
    )
    reply = await llm.ainvoke([("system", system), ("human", human)])
    content = getattr(reply, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


_caller: Callable[[str, str], Awaitable[str]] = _invoke_text_model


def set_caller_for_tests(fn: Callable[[str, str], Awaitable[str]] | None) -> None:
    """测试注入点：替换「问模型」，解析与写库仍走真实代码。"""
    global _caller
    _caller = fn or _invoke_text_model


async def review_memory(
    *,
    memory: Memory,
    evidence: Sequence[Evidence],
    siblings: Sequence[Memory],
) -> tuple[ReviewProposal, str]:
    """问一次模型。返回 (建议, 模型名)。"""
    config = await resolve_text_model()
    if not config.configured:
        raise DreamingUnavailable("模型未配置")

    raw = await _caller(
        DREAM_SYSTEM_PROMPT,
        _build_human(memory, evidence, siblings),
    )
    proposal = parse_proposal(
        raw,
        allowed_merge_ids={row.id for row in siblings},
        evidence=evidence,
    )
    return proposal, config.model


def can_review(memory: Memory) -> bool:
    """红线：只碰自动抽取且未经用户确认的候选。

    用户确认过、或本来就是他自己写的记忆，一律不生成建议；
    这是 `memory_service.upsert_extracted` 同一套规则的延伸，
    不能因为「换了个离线任务」就放宽。
    """
    return (
        memory.user_id is not None
        and memory.source == "auto_extract"
        and not memory.is_user_confirmed
        and memory.status == MemoryStatus.ACTIVE
    )


async def find_pending(session: AsyncSession, memory_id: uuid.UUID) -> MemoryReview | None:
    return await session.scalar(
        select(MemoryReview)
        .where(
            MemoryReview.memory_id == memory_id,
            MemoryReview.status == MemoryReviewStatus.PENDING,
        )
        .limit(1)
    )


async def create_review(
    session: AsyncSession,
    *,
    memory: Memory,
    proposal: ReviewProposal,
    model: str,
) -> MemoryReview:
    review = MemoryReview(
        user_id=memory.user_id or "",
        memory_id=memory.id,
        action=proposal.action,
        status=MemoryReviewStatus.PENDING,
        reason=proposal.reason,
        proposed_title=proposal.proposed_title,
        proposed_content=proposal.proposed_content,
        merge_target_id=proposal.merge_target_id,
        evidence_json=[
            {
                "message_id": item.message_id,
                "conversation_id": item.conversation_id,
                "excerpt": item.excerpt,
                "created_at": item.created_at,
                "matched": item.matched,
            }
            for item in proposal.evidence
        ],
        confidence=proposal.confidence,
        model=model[:128],
        prompt_version=PROMPT_VERSION,
    )
    session.add(review)
    await session.flush()
    return review


async def list_reviews(
    session: AsyncSession,
    *,
    user_id: str | None,
    status: MemoryReviewStatus | None = MemoryReviewStatus.PENDING,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[MemoryReview], int]:
    conditions = []
    if user_id is not None:
        conditions.append(MemoryReview.user_id == user_id)
    if status is not None:
        conditions.append(MemoryReview.status == status)

    where = and_(*conditions) if conditions else True
    total = int(await session.scalar(select(func.count()).where(where)) or 0)
    rows = (
        await session.scalars(
            select(MemoryReview)
            .where(where)
            .order_by(MemoryReview.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total


async def run_dreaming(
    *,
    tenant_id: str,
    user_id: str,
    days: int = 30,
    limit: int = MAX_MEMORIES_PER_RUN,
    dry_run: bool = False,
) -> DreamReport:
    """为一位用户整理一次梦境。调用方负责开 tenant_scope 与 session。

    不放进后台任务：它不是每轮都该跑的事，手动触发/定时脚本更合适。
    """
    from app.db.session import async_session_factory
    from app.tenancy.context import tenant_scope

    started = time.monotonic()
    report = DreamReport()

    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            candidates = list(
                (
                    await session.scalars(
                        select(Memory)
                        .where(
                            Memory.user_id == user_id,
                            Memory.status == MemoryStatus.ACTIVE,
                            Memory.source == "auto_extract",
                            Memory.is_user_confirmed.is_(False),
                            or_(
                                Memory.expires_at.is_(None),
                                Memory.expires_at > _now(),
                            ),
                        )
                        .order_by(Memory.updated_at.asc())
                        .limit(max(1, limit))
                    )
                ).all()
            )
            report.scanned = len(candidates)

            siblings = list(
                (
                    await session.scalars(
                        select(Memory)
                        .where(
                            Memory.user_id == user_id,
                            Memory.status == MemoryStatus.ACTIVE,
                            Memory.is_user_confirmed.is_(False),
                        )
                        .order_by(Memory.updated_at.desc())
                        .limit(50)
                    )
                ).all()
            )

            for memory in candidates:
                if not can_review(memory):
                    continue
                if await find_pending(session, memory.id) is not None:
                    report.skipped_pending += 1
                    continue

                try:
                    evidence = await collect_evidence(session, memory, days=days)
                    proposal, model = await review_memory(
                        memory=memory,
                        evidence=evidence,
                        siblings=[row for row in siblings if row.id != memory.id],
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批
                    report.failed += 1
                    logger.warning(
                        "梦境处理失败，已跳过该条。",
                        extra={
                            "event": "memory_dream_item_failed",
                            "error_category": type(exc).__name__,
                            "error": str(exc)[:300],
                        },
                    )
                    continue

                if proposal.action == MemoryReviewAction.KEEP:
                    report.kept += 1
                    continue

                if dry_run:
                    report.suggested += 1
                    report.items.append(
                        {
                            "memory_id": str(memory.id),
                            "title": memory.title,
                            "action": proposal.action.value,
                            "reason": proposal.reason,
                            "confidence": proposal.confidence,
                            "proposed_title": proposal.proposed_title,
                            "proposed_content": proposal.proposed_content,
                            "merge_target_id": (
                                str(proposal.merge_target_id) if proposal.merge_target_id else None
                            ),
                            "evidence_count": len(proposal.evidence),
                        }
                    )
                    continue

                async with session.begin_nested():
                    await create_review(session, memory=memory, proposal=proposal, model=model)
                report.suggested += 1

            if not dry_run and report.suggested:
                await session.commit()

    report.elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "梦境完成。",
        extra={
            "event": "memory_dream_completed",
            # 键名避开 LogRecord 保留字段（created / message / name）。
            "scanned_count": report.scanned,
            "suggested_count": report.suggested,
            "kept_count": report.kept,
            "skipped_count": report.skipped_pending,
            "failed_count": report.failed,
            "elapsed_ms": report.elapsed_ms,
        },
    )
    return report


async def apply_review(
    session: AsyncSession,
    review: MemoryReview,
    *,
    actor_user_id: str,
) -> Memory:
    """按人的确认执行建议。

    应用前重新校验记忆状态：建议生成后用户可能已经确认、编辑或删除了它，
    这时旧建议必须作废，不能拿过期结论覆盖人的决定。
    """
    if review.status != MemoryReviewStatus.PENDING:
        raise ReviewConflict("这条建议已经处理过。")

    memory = await session.get(Memory, review.memory_id)
    if memory is None:
        raise ReviewConflict("记忆已被删除。")
    if memory.user_id != actor_user_id:
        raise ReviewConflict("只能处理自己的记忆。")
    if not can_review(memory):
        raise ReviewConflict("记忆已被确认或改写，建议已失效。")

    if review.action == MemoryReviewAction.CORRECT:
        if not review.proposed_title or not review.proposed_content:
            raise ReviewConflict("建议缺少改写内容。")
        # 刻意不置 is_user_confirmed：模型纠错不等于用户确认。
        await memory_service.update_memory(
            session,
            memory,
            title=review.proposed_title,
            content=review.proposed_content,
        )
    elif review.action == MemoryReviewAction.MERGE:
        if review.merge_target_id is None:
            raise ReviewConflict("建议缺少合并目标。")
        target = await session.get(Memory, review.merge_target_id)
        if target is None or target.user_id != actor_user_id or not can_review(target):
            raise ReviewConflict("合并目标已不可用。")
        if review.proposed_title or review.proposed_content:
            await memory_service.update_memory(
                session,
                target,
                title=review.proposed_title,
                content=review.proposed_content,
            )
        await memory_service.supersede(session, memory, target)
    elif review.action == MemoryReviewAction.RETIRE:
        memory.status = MemoryStatus.SUPERSEDED
        memory.updated_at = _now()
        await session.flush()
    else:  # keep 不应落成 pending 建议
        raise ReviewConflict("该建议没有可执行的动作。")

    review.status = MemoryReviewStatus.APPLIED
    review.resolved_at = _now()
    await session.flush()

    logger.info(
        "梦境建议已应用。",
        extra={
            "event": "memory_review_applied",
            "review_id": str(review.id),
            "memory_id": str(memory.id),
            "action": review.action.value,
            "actor": actor_user_id,
        },
    )
    return memory


async def dismiss_review(session: AsyncSession, review: MemoryReview) -> None:
    if review.status != MemoryReviewStatus.PENDING:
        raise ReviewConflict("这条建议已经处理过。")
    review.status = MemoryReviewStatus.DISMISSED
    review.resolved_at = _now()
    await session.flush()


@dataclass(frozen=True)
class ExtractionStats:
    """自动抽取的确认率。

    口径：窗口内仍存在的自动候选里，已被用户确认的比例。
    **物理删除的候选查不到**，所以这个数会高估通过率——用户删得越多，
    分母越小。要精确统计得靠审计日志，但那会把「删了什么」也记下来，
    与记忆的隐私边界冲突；这里选择接受偏差，在页面上标明口径。
    """

    days: int
    auto_total: int
    auto_confirmed: int
    auto_pending: int
    auto_superseded: int
    reviews_applied: int
    confirmation_rate: float


async def extraction_stats(
    session: AsyncSession, *, user_id: str, days: int = 7
) -> ExtractionStats:
    since = _now() - timedelta(days=max(1, days))
    base = and_(
        Memory.user_id == user_id,
        Memory.source == "auto_extract",
        Memory.created_at >= since,
    )

    async def count(*extra: Any) -> int:
        return int(await session.scalar(select(func.count()).where(base, *extra)) or 0)

    total = await count()
    confirmed = await count(Memory.is_user_confirmed.is_(True))
    pending = await count(
        Memory.status == MemoryStatus.ACTIVE,
        Memory.is_user_confirmed.is_(False),
    )
    superseded = await count(Memory.status == MemoryStatus.SUPERSEDED)
    reviews_applied = int(
        await session.scalar(
            select(func.count()).where(
                MemoryReview.user_id == user_id,
                MemoryReview.status == MemoryReviewStatus.APPLIED,
                MemoryReview.resolved_at >= since,
            )
        )
        or 0
    )

    return ExtractionStats(
        days=days,
        auto_total=total,
        auto_confirmed=confirmed,
        auto_pending=pending,
        auto_superseded=superseded,
        reviews_applied=reviews_applied,
        confirmation_rate=round(confirmed / total, 4) if total else 0.0,
    )


def dreaming_enabled() -> bool:
    return bool(settings.memory_enabled and settings.memory_dream_enabled)


# ---------------------------------------------------------------- 租户级配置
#
# 三个值（开关 / 回溯天数 / 单次条数）原本只来自环境变量，改一次要重启服务，
# 等于「只有运维能调」。但它们的日常使用场景是「这个租户想不想跑、跑多狠」，
# 该由租户管理员在界面上决定。于是加一层租户覆盖，**环境变量降级为平台默认值**。
#
# 合并顺序（后者不能越过前者）：
#
#     平台开关（硬关）  →  租户开关  →  生效开关
#     平台默认天数      →  租户天数  →  生效天数
#
# 平台关闭是**急停**：租户配了 true 也照样关。否则运维想临时止血时，
# 得挨个去改租户配置，急停就没有意义了。

# 与 config.py 的 Field 约束同源。改范围要两处一起改；放在这里是因为接口
# 校验也要用，而 config 不该反向依赖 schema。
DAYS_MIN, DAYS_MAX = 1, 365
MAX_PER_RUN_MIN, MAX_PER_RUN_MAX = 1, 200


@dataclass(frozen=True)
class DreamConfig:
    """一次梦境实际生效的配置，含全部原始值。

    保留原始值是为了界面能说「跟随平台默认（30 天）」而不是只显示一个数字——
    只显示 30 天的话，用户改完平台默认后会发现某租户数字没动，却看不出原因。
    """

    enabled: bool
    days: int
    max_per_run: int
    platform_enabled: bool
    tenant_enabled: bool | None
    tenant_days: int
    tenant_max_per_run: int

    @property
    def disabled_reason(self) -> str | None:
        if not self.platform_enabled:
            return "平台层已关闭梦境（MEMORY_ENABLED / MEMORY_DREAM_ENABLED）"
        if self.tenant_enabled is False:
            return "本租户已关闭梦境"
        return None

    def clamp_limit(self, requested: int) -> int:
        """请求条数超过租户上限时按上限跑，不报错。

        这里刻意不抛 400：前端滑块和配置上限可能不同步，
        为了「你填多了」让整次梦境失败没有价值。
        """
        return max(MAX_PER_RUN_MIN, min(requested, self.max_per_run))

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "days": self.days,
            "max_per_run": self.max_per_run,
            "platform_enabled": self.platform_enabled,
            "tenant_enabled": self.tenant_enabled,
            "tenant_days": self.tenant_days,
            "tenant_max_per_run": self.tenant_max_per_run,
            "disabled_reason": self.disabled_reason,
            "days_min": DAYS_MIN,
            "days_max": DAYS_MAX,
            "max_per_run_min": MAX_PER_RUN_MIN,
            "max_per_run_max": MAX_PER_RUN_MAX,
        }


async def resolve_dream_config(session: AsyncSession, *, tenant_id: str) -> DreamConfig:
    """合并平台默认值与租户设置。调用方负责开 `tenant_scope`。

    没有配置行是常态（存量为空），此时全部回退平台默认。
    """
    row = await session.get(TenantMemorySettings, tenant_id)
    platform_enabled = dreaming_enabled()
    tenant_enabled = row.dream_enabled if row is not None else None

    # 0 表示「跟随平台默认」，所以这里用真值判断而不是 `is not None`。
    tenant_days = row.dream_days if row is not None else 0
    tenant_max = row.dream_max_per_run if row is not None else 0

    return DreamConfig(
        enabled=platform_enabled and tenant_enabled is not False,
        days=tenant_days or settings.memory_dream_days,
        max_per_run=tenant_max or settings.memory_dream_max_per_run,
        platform_enabled=platform_enabled,
        tenant_enabled=tenant_enabled,
        tenant_days=tenant_days,
        tenant_max_per_run=tenant_max,
    )


async def save_dream_config(
    session: AsyncSession,
    *,
    tenant_id: str,
    enabled: bool | None,
    days: int,
    max_per_run: int,
    actor_user_id: str | None,
) -> DreamConfig:
    """写入租户配置（upsert）。调用方负责 commit 与开 `tenant_scope`。

    `0` / `None` 原样落库，**不展开成当前默认值**——展开就等于把今天的默认值
    钉死在这个租户上，以后平台调默认值时它会悄悄不跟随。
    """
    if days != 0 and not DAYS_MIN <= days <= DAYS_MAX:
        raise ValueError(f"days 必须是 0 或 {DAYS_MIN}-{DAYS_MAX}")
    if max_per_run != 0 and not MAX_PER_RUN_MIN <= max_per_run <= MAX_PER_RUN_MAX:
        raise ValueError(f"max_per_run 必须是 0 或 {MAX_PER_RUN_MIN}-{MAX_PER_RUN_MAX}")

    row = await session.get(TenantMemorySettings, tenant_id)
    if row is None:
        row = TenantMemorySettings(tenant_id=tenant_id)
        session.add(row)

    row.dream_enabled = enabled
    row.dream_days = days
    row.dream_max_per_run = max_per_run
    row.updated_by = actor_user_id

    await session.flush()
    return await resolve_dream_config(session, tenant_id=tenant_id)
