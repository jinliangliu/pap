# app/services/memory_service.py
"""长期记忆。

三条边界，先写在这里，免得后面越做越像聊天记录：

1. 只存**稳定事实**：称呼、身份、长期偏好、明确决策、项目背景。
   不存本轮参数、不存一次性的查询结果、不存模型的猜测。
2. 来源分两种：`user`（用户自己写的/确认的）与 `auto_extract`（对话结束后抽出来的候选）。
   候选默认 `is_user_confirmed=False`，页面上能一键确认或删除——
   模型自己觉得「这人大概喜欢简洁」，不该悄悄变成人格设定。
3. 用户主动删除 = **物理删除**。记忆是隐私的一部分，软删留档等于没删。
   `SUPERSEDED` 只用在「改口」场景：新版本替代旧版本时，旧版本留档可追溯。

与聊天记录的关系：`messages` 是原始流水，`memories` 是提炼后的结论。
两者不互相替代，也不做同步——删会话不该删记忆，删记忆也不该动会话。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory, MemoryStatus, MemoryType

logger = logging.getLogger(__name__)

# 单用户记忆上限。超过就按「最不重要 + 最久没更新」淘汰，
# 不设上限的话抽取任务跑半年就能把一个人的 prompt 撑爆。
MAX_PER_USER = 200

# 自动抽取的 working_note 默认 30 天后过期。
# 「正在做 M4」这种话三个月后还在 prompt 里，只会误导模型。
WORKING_NOTE_TTL_DAYS = 30

# 未确认的自动候选要进 prompt，置信度必须到这条线。
# 抽取时被要求「用户明确说的给 0.9、可合理推断的给 0.7」，
# 所以 0.8 正好把「明确说过」和「模型猜的」分开：
# 后者留在记忆页等人判断，不能未经确认就变成人格设定。
UNCONFIRMED_PROMPT_MIN_CONFIDENCE = 0.8

# 注入 prompt 的预算（字符）。中文一个字约 1 token，
# 加上标题和符号，1600 字符大致 800-1000 token——相对 15k 的规划 prompt 是小头。
PROMPT_BLOCK_BUDGET = 1600

# 注入顺序：越靠前越影响模型判断。
# 身份/偏好比具体事实更稳定，working_note 最易过期，放最后。
_TYPE_PRIORITY = {
    MemoryType.PROFILE: 0,
    MemoryType.PREFERENCE: 1,
    MemoryType.DECISION: 2,
    MemoryType.FACT: 3,
    MemoryType.WORKING_NOTE: 4,
}

_TYPE_LABEL = {
    MemoryType.PROFILE: "身份",
    MemoryType.PREFERENCE: "偏好",
    MemoryType.DECISION: "决策",
    MemoryType.FACT: "事实",
    MemoryType.WORKING_NOTE: "进展",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _visible_to(user_id: str | None) -> Any:
    """个人记忆 + 租户共享记忆。

    `user_id IS NULL` 是租户级共享记忆（比如团队约定），
    这类记录对所有成员可见，但只有带管理权限的人能改。
    """
    if user_id is None:
        return Memory.user_id.is_(None)
    return or_(Memory.user_id == user_id, Memory.user_id.is_(None))


async def list_memories(
    session: AsyncSession,
    *,
    user_id: str | None,
    status: MemoryStatus | None = MemoryStatus.ACTIVE,
    memory_type: MemoryType | None = None,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Memory], int]:
    """列记忆，附总数（分页用）。"""
    conditions = [_visible_to(user_id)]
    if status is not None:
        conditions.append(Memory.status == status)
    if memory_type is not None:
        conditions.append(Memory.memory_type == memory_type)
    if query:
        pattern = f"%{query.strip()}%"
        conditions.append(
            or_(Memory.title.ilike(pattern), Memory.content.ilike(pattern))
        )

    where = and_(*conditions)
    total = int(await session.scalar(select(func.count()).where(where)) or 0)
    rows = (
        await session.scalars(
            select(Memory)
            .where(where)
            .order_by(Memory.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), total


async def get_memory(session: AsyncSession, memory_id: uuid.UUID) -> Memory | None:
    return await session.get(Memory, memory_id)


async def find_duplicate(
    session: AsyncSession,
    *,
    user_id: str | None,
    memory_type: MemoryType,
    title: str,
) -> Memory | None:
    """同一个人、同一类、同标题视为同一条记忆。

    标题归一化（去空格、忽略大小写）后再比：用户手打的和抽取出来的
    「称呼」「称呼 」不该变成两条。
    """
    normalized = title.strip().lower()
    rows = (
        await session.scalars(
            select(Memory).where(
                Memory.status == MemoryStatus.ACTIVE,
                Memory.memory_type == memory_type,
                Memory.user_id == user_id
                if user_id is not None
                else Memory.user_id.is_(None),
            )
        )
    ).all()
    for row in rows:
        if (row.title or "").strip().lower() == normalized:
            return row
    return None


async def create_memory(
    session: AsyncSession,
    *,
    user_id: str | None,
    memory_type: MemoryType,
    title: str,
    content: str,
    source: str = "user",
    confidence: float = 1.0,
    is_user_confirmed: bool = True,
    source_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> tuple[Memory, bool]:
    """新建或更新同标题记忆。返回 (记录, 是否新建)。

    同标题直接更新而不是新建：否则「用户的称呼」会被记成十条，
    注入时十条一起进 prompt，互相打架。
    """
    existing = await find_duplicate(
        session, user_id=user_id, memory_type=memory_type, title=title
    )
    if existing is not None:
        existing.content = content
        existing.confidence = max(existing.confidence, confidence)
        existing.updated_at = _now()
        if is_user_confirmed:
            existing.is_user_confirmed = True
            existing.source = source
        if metadata:
            existing.metadata_json = {**(existing.metadata_json or {}), **metadata}
        if expires_at is not None:
            existing.expires_at = expires_at
        await session.flush()
        return existing, False

    row = Memory(
        user_id=user_id,
        memory_type=memory_type,
        status=MemoryStatus.ACTIVE,
        title=title.strip()[:256],
        content=content.strip(),
        source=source,
        source_thread_id=source_thread_id,
        confidence=max(0.0, min(1.0, confidence)),
        is_user_confirmed=is_user_confirmed,
        metadata_json=metadata or {},
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row, True


@dataclass(frozen=True)
class AutoMemory:
    """自动抽取出来的一条候选。

    不复用 `create_memory` 的参数列表，是因为抽取多一条规则：
    不能覆盖用户自己写过或确认过的记忆。这条规则写在 `upsert_extracted` 里，
    用独立类型提醒调用方「这不是用户输入」。
    """

    memory_type: MemoryType
    title: str
    content: str
    confidence: float = 0.6


async def upsert_extracted(
    session: AsyncSession,
    *,
    user_id: str,
    items: Sequence[AutoMemory],
    source_thread_id: str | None = None,
) -> tuple[int, int, int]:
    """写入抽取结果，返回 (新增, 刷新, 跳过)。

    三条规则：

    1. 已存在的记忆如果用户确认过、或本来就是他自己写的，**只跳过、不改内容**。
       模型推断出来的话不能悄悄替换用户的原话；要改由人在页面上改。
    2. 已存在的自动候选则刷新内容与置信度，避免同一条事实攒出多个版本。
    3. working_note 默认 30 天后过期——它最容易过期，留着只会误导模型。
    """
    created = refreshed = skipped = 0

    for item in items:
        existing = await find_duplicate(
            session,
            user_id=user_id,
            memory_type=item.memory_type,
            title=item.title,
        )
        if existing is not None and (
            existing.is_user_confirmed or existing.source == "user"
        ):
            skipped += 1
            continue

        expires_at = (
            _now() + timedelta(days=WORKING_NOTE_TTL_DAYS)
            if item.memory_type is MemoryType.WORKING_NOTE
            else None
        )
        _, is_new = await create_memory(
            session,
            user_id=user_id,
            memory_type=item.memory_type,
            title=item.title,
            content=item.content,
            source="auto_extract",
            confidence=item.confidence,
            is_user_confirmed=False,
            source_thread_id=source_thread_id,
            metadata={"extracted": True},
            expires_at=expires_at,
        )
        if is_new:
            created += 1
        else:
            refreshed += 1

    return created, refreshed, skipped


async def update_memory(
    session: AsyncSession,
    row: Memory,
    *,
    title: str | None = None,
    content: str | None = None,
    memory_type: MemoryType | None = None,
    is_user_confirmed: bool | None = None,
) -> Memory:
    """编辑。用户手动改过就等于确认过，顺手把标记置上。"""
    if title is not None:
        row.title = title.strip()[:256]
    if content is not None:
        row.content = content.strip()
    if memory_type is not None:
        row.memory_type = memory_type
    if is_user_confirmed is not None:
        row.is_user_confirmed = is_user_confirmed
    row.updated_at = _now()
    await session.flush()
    return row


async def confirm_memory(session: AsyncSession, row: Memory) -> Memory:
    row.is_user_confirmed = True
    row.confidence = 1.0
    row.updated_at = _now()
    await session.flush()
    return row


async def delete_memory(session: AsyncSession, row: Memory) -> None:
    """物理删除。"""
    await session.delete(row)
    await session.flush()


async def supersede(
    session: AsyncSession, old: Memory, new: Memory
) -> None:
    old.status = MemoryStatus.SUPERSEDED
    old.superseded_by_id = new.id
    old.updated_at = _now()
    await session.flush()


async def expire_overdue(session: AsyncSession, *, user_id: str | None = None) -> int:
    """把过期的记忆置为 SUPERSEDED。

    不物理删：过期不等于用户撤销，留档能解释「为什么以前那样答」。
    """
    conditions = [Memory.status == MemoryStatus.ACTIVE, Memory.expires_at.is_not(None), Memory.expires_at < _now()]
    if user_id is not None:
        conditions.append(Memory.user_id == user_id)
    rows = (await session.scalars(select(Memory).where(and_(*conditions)))).all()
    for row in rows:
        row.status = MemoryStatus.SUPERSEDED
        row.updated_at = _now()
    if rows:
        await session.flush()
    return len(rows)


async def enforce_cap(session: AsyncSession, *, user_id: str) -> int:
    """超过上限时淘汰最不重要的。

    淘汰顺序：working_note 先走，其次最久没更新的。
    用户确认过的记忆不参与淘汰——那是他自己留的。
    """
    total = int(
        await session.scalar(
            select(func.count()).where(
                Memory.user_id == user_id, Memory.status == MemoryStatus.ACTIVE
            )
        )
        or 0
    )
    if total <= MAX_PER_USER:
        return 0

    rows = (
        await session.scalars(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.status == MemoryStatus.ACTIVE)
            .order_by(Memory.is_user_confirmed.asc(), Memory.updated_at.asc())
        )
    ).all()
    drop = total - MAX_PER_USER
    removed = 0
    for row in rows:
        if removed >= drop:
            break
        if row.is_user_confirmed:
            continue
        await session.delete(row)
        removed += 1
    if removed:
        await session.flush()
        logger.info(
            "记忆超出上限，淘汰 %s 条。", removed, extra={"event": "memory_cap_enforced"}
        )
    return removed


async def load_for_prompt(
    session: AsyncSession, *, user_id: str | None, limit: int = 40
) -> list[Memory]:
    """取本轮要注入的记忆。

    只取 ACTIVE、未过期、置信度够高的。条数上限比 MAX_PER_USER 小得多：
    注入是为了让模型少问几句，不是为了把档案全塞给它。
    """
    conditions = [
        _visible_to(user_id),
        Memory.status == MemoryStatus.ACTIVE,
        Memory.confidence >= 0.5,
        # 自动抽取的候选默认不进 prompt：模型推断出来的东西先留在记忆页等确认。
        # 唯一例外是置信度到 0.8 的——那是「用户明确说过」这一档，
        # 模型只是把它记下来，没有发挥。
        or_(
            Memory.is_user_confirmed.is_(True),
            Memory.confidence >= UNCONFIRMED_PROMPT_MIN_CONFIDENCE,
        ),
        or_(Memory.expires_at.is_(None), Memory.expires_at > _now()),
    ]
    rows = list(
        (
            await session.scalars(
                select(Memory)
                .where(and_(*conditions))
                .order_by(Memory.updated_at.desc())
                .limit(limit * 3)
            )
        ).all()
    )
    # 类型优先级优先于时间：身份和偏好比三天前的一条 working_note 更该出现在前面。
    rows.sort(key=lambda r: (_TYPE_PRIORITY.get(r.memory_type, 9), -r.updated_at.timestamp()))
    return rows[:limit]


def render_prompt_block(rows: Sequence[Memory]) -> str:
    """把记忆拼成一段可直接塞进 prompt 的文本。

    刻意写得很短，并且明确「与当前对话冲突时以当前为准」——
    记忆是旧结论，用户改主意是常态，不能让模型拿旧记录反驳用户。
    """
    if not rows:
        return ""

    lines: list[str] = ["[已知用户信息]"]
    for row in rows:
        label = _TYPE_LABEL.get(row.memory_type, "信息")
        text = (row.content or "").strip().replace("\n", " ")
        if not text:
            continue
        line = f"- {label}｜{row.title}：{text}"
        if sum(len(x) for x in lines) + len(line) > PROMPT_BLOCK_BUDGET:
            lines.append("- （其余记忆已省略）")
            break
        lines.append(line)

    if len(lines) == 1:
        return ""

    lines.append(
        "以上是历史对话中确认过的信息，仅作背景参考；"
        "与用户本轮明确表述冲突时，以本轮为准，不要用这些信息反推用户意图。"
    )
    return "\n".join(lines)
