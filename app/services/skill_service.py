# app/services/skill_service.py
"""技能：作业指令 + 触发条件 + 工具白名单。

三件事必须说清楚，否则这一层很容易变成「第二套工具注册表」：

1. **技能不授予权限。** `allowed_tools` 只会和当前身份可见的工具取交集。
   技能里写了 `send_email` 但这个身份没有，结果是没有，而不是报错，更不是放行。
2. **匹配是确定性的。** `/技能名` 显式优先，其次触发词命中，再按优先级和
   触发词长度排序。不花一次模型调用——选技能如果需要问模型，延迟翻倍，
   而且模型选的技能不可预测，出问题时无法从日志里复原原因。
3. **注入有预算。** 正文超长截断并打日志，不把上下文窗口吃满。
   技能是辅助，规划失败才是真的不可用。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill, SkillHit

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")

# 正文上限。8000 字符≈4000 token，已经够写一份很细的作业手册；
# 再长说明它该进知识库，由检索按需取，而不是每轮都塞进 system prompt。
MAX_INSTRUCTION_CHARS = 8000
MAX_DESCRIPTION_CHARS = 256
MAX_KEYWORDS = 12
MAX_KEYWORD_CHARS = 32

# 显式调用前缀：消息以 /技能名 开头。用空格或中文标点分隔，
# 避免 `/巡检比对下这段` 这种连写被误判成技能名。
EXPLICIT_PATTERN = re.compile(r"^/([a-z][a-z0-9_-]{1,63})(?:\s|$)")

# 「近 7 天命中」的口径。7 天够看出趋势，又不至于让一次历史高峰
# 在页面上挂一个月。
HITS_WINDOW_DAYS = 7


class SkillError(ValueError):
    """技能内容不合法。接口层转 400。"""


class SkillExistsError(SkillError):
    """同名技能已存在。"""


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_name(raw: str) -> str:
    value = (raw or "").strip().lower().replace(" ", "-")
    if not NAME_PATTERN.match(value):
        raise SkillError("Skill ID 只能用小写字母开头，包含小写字母、数字、- 或 _，长度 2–64。")
    return value


def normalize_keywords(raw: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        word = str(item).strip()
        if not word:
            continue
        if len(word) > MAX_KEYWORD_CHARS:
            raise SkillError(f"触发词「{word[:8]}…」超过 {MAX_KEYWORD_CHARS} 字符。")
        if word not in out:
            out.append(word)
    if len(out) > MAX_KEYWORDS:
        raise SkillError(f"触发词最多 {MAX_KEYWORDS} 个。")
    return out


def normalize_tools(raw: Sequence[str] | None) -> list[str] | None:
    """None 与 [] 语义不同：前者不收窄，后者一个都不给。"""
    if raw is None:
        return None
    out: list[str] = []
    for item in raw:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out


def clip_instruction(text: str) -> tuple[str, bool]:
    """返回 (正文, 是否被截断)。"""
    value = (text or "").strip()
    if len(value) <= MAX_INSTRUCTION_CHARS:
        return value, False
    return value[:MAX_INSTRUCTION_CHARS], True


def _explicit_name(message: str) -> str | None:
    match = EXPLICIT_PATTERN.match((message or "").strip())
    return match.group(1) if match else None


def match_skill(message: str, skills: Sequence[Skill]) -> tuple[Skill, str, str] | None:
    """
    命中一个技能，返回 (技能, 命中方式, 命中的触发词)。

    排序口径：优先级高的先；同优先级取触发词更长的（更具体）；
    再同就按名字稳定排序，避免同一句话两次请求命中不同技能。
    """
    text = (message or "").strip()
    if not text:
        return None

    explicit = _explicit_name(text)
    if explicit:
        for skill in skills:
            if skill.name == explicit:
                return skill, "explicit", ""

    best: tuple[int, int, str, Skill, str] | None = None
    for skill in skills:
        hit = ""
        for word in skill.trigger_keywords or []:
            if word and word in text:
                # 同一技能命中多个词，取最长的那个（信息量最大）。
                if len(word) > len(hit):
                    hit = word
        if not hit:
            continue
        key = (skill.priority, len(hit), skill.name)
        if best is None or key > (best[0], best[1], best[2]):
            best = (skill.priority, len(hit), skill.name, skill, hit)

    if best is None:
        return None
    return best[3], "keyword", best[4]


def narrow_catalog(catalog: Sequence[dict[str, Any]], skill: Skill) -> list[dict[str, Any]]:
    """技能白名单与可见工具取交集。

    只收窄，不放大：白名单里出现当前身份看不到的工具，静默丢弃。
    报错等于告诉调用方「这个工具存在但你没权限」，那是个探针。
    """
    allowed = skill.allowed_tools
    if allowed is None:
        return list(catalog)
    names = set(allowed)
    return [item for item in catalog if item.get("tool_name") in names]


def missing_tools(skill: Skill, catalog: Sequence[dict[str, Any]]) -> list[str]:
    """白名单里配了、但当前身份看不到的工具。页面用来标「不可用」。"""
    if skill.allowed_tools is None:
        return []
    visible = {item.get("tool_name") for item in catalog}
    return [name for name in skill.allowed_tools if name not in visible]


def render_block(
    skill: Skill,
    *,
    tools: Sequence[str] = (),
    truncated: bool = False,
) -> str:
    """拼进 system prompt 的技能段。

    必须写明「工具已被限定」：不说的话，模型看到工具变少会去猜自己是不是
    被禁用了，然后在回答里解释一堆与问题无关的东西。
    """
    body, clipped = clip_instruction(skill.instruction)
    lines = [
        f"## 当前技能：{skill.display_name or skill.name}",
        "用户已进入该技能。下面的作业要求优先于通用规则，但它不授予任何额外权限："
        "仍然只能调用 tools 列表中出现的工具。",
        "",
        body,
    ]
    if truncated or clipped:
        lines.append("")
        lines.append("（技能指令超过长度上限，已截断。）")
    if skill.allowed_tools is not None:
        lines.append("")
        lines.append(
            "可用工具已被限定为："
            + ("、".join(tools) if tools else "无（本轮只用对话作答）")
            + "。"
        )
    return "\n".join(lines).strip()


async def list_skills(session: AsyncSession) -> list[Skill]:
    result = await session.execute(
        select(Skill).order_by(Skill.enabled.desc(), Skill.updated_at.desc())
    )
    return list(result.scalars().all())


async def load_enabled(session: AsyncSession) -> list[Skill]:
    result = await session.execute(
        select(Skill).where(Skill.enabled.is_(True)).order_by(Skill.priority.desc())
    )
    return list(result.scalars().all())


async def get_skill(session: AsyncSession, skill_id: uuid.UUID) -> Skill | None:
    return await session.get(Skill, skill_id)


async def get_by_name(session: AsyncSession, name: str) -> Skill | None:
    result = await session.execute(select(Skill).where(Skill.name == name))
    return result.scalars().first()


async def create_skill(
    session: AsyncSession,
    *,
    name: str,
    display_name: str,
    description: str = "",
    instruction: str = "",
    trigger_keywords: Sequence[str] | None = None,
    allowed_tools: Sequence[str] | None = None,
    priority: int = 100,
    enabled: bool = True,
    source: str = "manual",
    created_by: str | None = None,
) -> Skill:
    clean_name = normalize_name(name)
    if await get_by_name(session, clean_name) is not None:
        raise SkillExistsError(f"Skill ID「{clean_name}」已存在。")

    body, _ = clip_instruction(instruction)
    row = Skill(
        name=clean_name,
        display_name=(display_name or clean_name).strip()[:64],
        description=(description or "").strip()[:MAX_DESCRIPTION_CHARS],
        instruction=body,
        trigger_keywords=normalize_keywords(trigger_keywords),
        allowed_tools=normalize_tools(allowed_tools),
        priority=int(priority),
        enabled=bool(enabled),
        source=source,
        created_by=created_by,
    )
    # 时间在 Python 侧赋值，不依赖 server_default：
    # 服务端生成的列在 flush 后是「过期」状态，异步会话里再读会触发一次隐式
    # SELECT，轻则多一次往返，重则 MissingGreenlet（接口 500 而数据已提交）。
    row.created_at = _now()
    row.updated_at = row.created_at
    session.add(row)
    await session.flush()
    return row


async def update_skill(
    session: AsyncSession,
    row: Skill,
    *,
    display_name: str | None = None,
    description: str | None = None,
    instruction: str | None = None,
    trigger_keywords: Sequence[str] | None = None,
    allowed_tools: Sequence[str] | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
    tools_provided: bool = False,
) -> Skill:
    """局部更新。

    `allowed_tools` 用 `tools_provided` 单独标记「这次到底改没改」：
    None 本身就是合法值（= 不收窄），只靠 None 无法区分「没传」和「传了 null」。
    """
    if display_name is not None:
        row.display_name = display_name.strip()[:64] or row.name
    if description is not None:
        row.description = description.strip()[:MAX_DESCRIPTION_CHARS]
    if instruction is not None:
        row.instruction, _ = clip_instruction(instruction)
    if trigger_keywords is not None:
        row.trigger_keywords = normalize_keywords(trigger_keywords)
    if tools_provided:
        row.allowed_tools = normalize_tools(allowed_tools)
    if priority is not None:
        row.priority = int(priority)
    if enabled is not None:
        row.enabled = bool(enabled)
    row.updated_at = _now()
    await session.flush()
    return row


async def delete_skill(session: AsyncSession, row: Skill) -> None:
    await session.delete(row)


async def upsert_imported_skills(
    session: AsyncSession,
    entries: Sequence[dict[str, Any]],
    *,
    created_by: str | None,
    source: str = "imported",
) -> tuple[int, int]:
    """按名字导入一批技能，返回 (新建数, 覆盖数)。

    同名**覆盖**而不是跳过：最常见的用法就是「在这台改完导出、到那台导入」，
    跳过会让两边悄悄不一致，而人以为自己导入成功了。

    名字不合规的条目直接跳过而不是让整批失败：批量导入里有一条脏数据就全废，
    等于逼人手工删掉重来。
    """
    created = updated = 0

    for raw in entries:
        if not isinstance(raw, dict):
            continue

        try:
            name = normalize_name(str(raw.get("name", "")))
        except SkillError:
            continue

        row = await get_by_name(session, name)
        instruction = str(raw.get("instruction") or "")
        keywords = raw.get("trigger_keywords") or []
        tools = raw.get("allowed_tools")

        if row is None:
            await create_skill(
                session,
                name=name,
                display_name=str(raw.get("display_name") or ""),
                description=str(raw.get("description") or ""),
                instruction=instruction,
                trigger_keywords=keywords,
                allowed_tools=tools,
                priority=int(raw.get("priority") or 100),
                enabled=bool(raw.get("enabled", True)),
                source=source,
                created_by=created_by,
            )
            created += 1
            continue

        await update_skill(
            session,
            row,
            display_name=str(raw.get("display_name") or row.display_name),
            description=str(raw.get("description") or ""),
            instruction=instruction,
            trigger_keywords=keywords,
            allowed_tools=tools,
            tools_provided=True,
            priority=int(raw.get("priority") or row.priority),
            enabled=bool(raw.get("enabled", row.enabled)),
        )
        updated += 1

    return created, updated


async def pick(session: AsyncSession, message: str) -> tuple[Skill, str, str] | None:
    """本轮该用哪个技能。查询失败由调用方兜底，不在这里吞异常。"""
    skills = await load_enabled(session)
    if not skills:
        return None
    return match_skill(message, skills)


# ---------------- 命中记录 ----------------
#
# 只记录「这一轮用上了」，不记录召回测试：测试点一次就算一次命中，
# 会把趋势画成假的——调指令的人一天点几十次，真实使用可能只有两次。


async def record_hit(
    session: AsyncSession,
    *,
    skill: Skill,
    matched_by: str,
    keyword: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    user_id: str | None = None,
) -> SkillHit:
    row = SkillHit(
        skill_id=skill.id,
        skill_name=skill.name,
        matched_by=matched_by or "",
        keyword=(keyword or None),
        thread_id=thread_id,
        run_id=run_id,
        trace_id=trace_id,
        user_id=user_id,
    )
    # 同 skills：时间在 Python 侧赋值，别让异步会话去读 server_default。
    row.created_at = _now()
    session.add(row)
    await session.flush()
    return row


def _since(days: int = HITS_WINDOW_DAYS) -> datetime:
    return _now() - timedelta(days=days)


async def counts_since(
    session: AsyncSession,
    *,
    days: int = HITS_WINDOW_DAYS,
) -> dict[uuid.UUID, int]:
    """窗口内每个技能被命中多少次。列表页一次查回来，别按行 N+1。"""
    rows = await session.execute(
        select(SkillHit.skill_id, func.count(SkillHit.id))
        .where(SkillHit.created_at >= _since(days))
        .group_by(SkillHit.skill_id)
    )
    return {skill_id: int(count) for skill_id, count in rows}


async def last_hit_at(
    session: AsyncSession,
    skill_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, datetime]:
    if not skill_ids:
        return {}
    rows = await session.execute(
        select(SkillHit.skill_id, func.max(SkillHit.created_at))
        .where(SkillHit.skill_id.in_(list(skill_ids)))
        .group_by(SkillHit.skill_id)
    )
    return {skill_id: when for skill_id, when in rows if when is not None}


async def was_hit(session: AsyncSession, run_id: str) -> bool:
    """这一轮（run）是否已经记过命中。

    `plan_request` 会因为「工具执行完再来一次」被重复调用，不挡的话
    一次任务走三步就会记三次命中，趋势图直接失真。
    用 run_id 而不是进程内集合：多 worker 部署下内存标记不可靠。
    """
    if not run_id:
        return False
    row = await session.scalar(select(SkillHit.id).where(SkillHit.run_id == run_id).limit(1))
    return row is not None


async def recent_hits(
    session: AsyncSession,
    skill_id: uuid.UUID,
    *,
    limit: int = 5,
) -> list[SkillHit]:
    rows = await session.execute(
        select(SkillHit)
        .where(SkillHit.skill_id == skill_id)
        .order_by(SkillHit.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
