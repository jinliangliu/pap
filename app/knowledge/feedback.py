# app/knowledge/feedback.py
"""使用反馈：点赞/踩的落库、bad case 清单、满意度统计、升格成评测用例。

## 为什么单独存一份，不去改消息行

消息行属于会话，会话会被清理（``purge_conversation``）。而 bad case 的价值
恰恰在于「会话都没了还能复盘」——用户当时问了什么、我们引了哪几段、
他说哪里不对，这三样必须存在知识库自己的表里。

同一条反馈可以点赞也可以改踩（同一个 message_id 再提交一次），所以写入是
**按 message_id 幂等 upsert**：同一个人对同一条回答反复点，库里不该堆十行。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.evaluation import case_to_dict
from app.models.knowledge_gov import (
    CASE_SOURCE_FEEDBACK,
    VALID_REASONS,
    VALID_VERDICTS,
    VERDICT_DOWN,
    KnowledgeEvalCase,
    KnowledgeFeedback,
)
from app.tenancy.context import tenant_scope

EXCERPT_CHARS = 800
LIST_LIMIT = 200


class FeedbackRejected(ValueError):
    """调用方能自己修正的输入问题（映射 400）。"""


def _validate(verdict: str, reason: str | None) -> None:
    if verdict not in VALID_VERDICTS:
        raise FeedbackRejected(f"verdict 只能是 {' / '.join(sorted(VALID_VERDICTS))}。")

    if reason and reason not in VALID_REASONS:
        raise FeedbackRejected(f"未知的 reason：{reason}")


async def record(
    session: AsyncSession,
    tenant_id: str,
    *,
    message_id: str | None,
    conversation_id: str | None,
    run_id: str | None,
    query: str,
    answer: str,
    verdict: str,
    reason: str | None,
    comment: str | None,
    chunk_ids: list[str],
    document_ids: list[str],
    actor: str | None,
) -> dict[str, Any]:
    """写入或覆盖一条反馈。返回落库后的样子。"""
    _validate(verdict, reason)

    with tenant_scope(tenant_id):
        existing: KnowledgeFeedback | None = None

        if message_id:
            existing = await session.scalar(
                select(KnowledgeFeedback).where(
                    KnowledgeFeedback.tenant_id == tenant_id,
                    KnowledgeFeedback.message_id == message_id,
                    # 同一消息只认同一人的反馈：多人协作时 A 的踩不该被 B 的赞覆盖。
                    KnowledgeFeedback.created_by == actor,
                )
            )

        row = existing or KnowledgeFeedback(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            created_at=datetime.now(UTC),
        )

        row.conversation_id = conversation_id
        row.message_id = message_id
        row.run_id = run_id
        row.query = (query or "")[:4000]
        row.answer_excerpt = (answer or "")[:EXCERPT_CHARS]
        row.verdict = verdict
        # 赞的时候不留原因：残留一个上次的「答非所问」会让统计全错。
        row.reason = reason if verdict == VERDICT_DOWN else None
        row.comment = (comment or None) and comment[:2000]
        row.chunk_ids = list(chunk_ids or [])
        row.document_ids = list(document_ids or [])
        row.created_by = actor

        if existing is None:
            session.add(row)
        else:
            # 改判之后原来的「已处理」结论不再成立，重置回待处理。
            row.handled = False
            row.handled_note = None

        await session.flush()
        payload = _to_dict(row)

    return payload


def _to_dict(row: KnowledgeFeedback) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "conversation_id": row.conversation_id,
        "message_id": row.message_id,
        "run_id": row.run_id,
        "query": row.query,
        "answer_excerpt": row.answer_excerpt,
        "verdict": row.verdict,
        "reason": row.reason,
        "comment": row.comment,
        "chunk_ids": list(row.chunk_ids or []),
        "document_ids": list(row.document_ids or []),
        "handled": bool(row.handled),
        "handled_note": row.handled_note,
        "case_id": str(row.case_id) if row.case_id else None,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


async def list_feedback(
    session: AsyncSession,
    tenant_id: str,
    *,
    verdict: str | None = None,
    handled: bool | None = None,
    limit: int = LIST_LIMIT,
) -> dict[str, Any]:
    """bad case 清单。默认按时间倒序，未处理的在最前（前端自己筛）。"""
    with tenant_scope(tenant_id):
        statement = select(KnowledgeFeedback).where(KnowledgeFeedback.tenant_id == tenant_id)

        if verdict:
            statement = statement.where(KnowledgeFeedback.verdict == verdict)

        if handled is not None:
            statement = statement.where(KnowledgeFeedback.handled.is_(handled))

        statement = statement.order_by(KnowledgeFeedback.created_at.desc()).limit(
            max(1, min(limit, 500))
        )
        rows = list((await session.scalars(statement)).all())

    return {"items": [_to_dict(row) for row in rows], "total": len(rows)}


async def mark_handled(
    session: AsyncSession,
    tenant_id: str,
    feedback_id: str,
    *,
    handled: bool,
    note: str | None,
) -> dict[str, Any] | None:
    target = _as_uuid(feedback_id)

    if target is None:
        return None

    with tenant_scope(tenant_id):
        row = await session.get(KnowledgeFeedback, target)

        if row is None or row.tenant_id != tenant_id:
            return None

        row.handled = handled
        row.handled_note = (note or None) and note[:1000]
        await session.flush()

        return _to_dict(row)


async def promote_to_case(
    session: AsyncSession,
    tenant_id: str,
    feedback_id: str,
    *,
    expect_document: list[str],
    expect_section: str | None,
    actor: str | None,
) -> dict[str, Any] | None:
    """把一条差评升格成评测用例。

    ``expect_document`` 允许为空：从事故里捞出来的问题，很多时候只有「它答错了」
    这个事实，还不知道正确答案在哪一份资料里。存成未标注状态、界面上标出来，
    比硬要求先填完更接近真实排查流程——**但绝不替用户猜一份填进去**，
    那会让评测集变成噪声源。
    """
    target = _as_uuid(feedback_id)

    if target is None:
        return None

    with tenant_scope(tenant_id):
        row = await session.get(KnowledgeFeedback, target)

        if row is None or row.tenant_id != tenant_id:
            return None

        if row.case_id:
            existing = await session.get(KnowledgeEvalCase, row.case_id)

            if existing is not None:
                return case_to_dict(existing)

        case = KnowledgeEvalCase(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            query=row.query[:4000],
            expect_found=True,
            expect_document=list(expect_document or []),
            expect_section=(expect_section or None),
            category=None,
            note=(row.comment or "")[:2000] or None,
            source=CASE_SOURCE_FEEDBACK,
            enabled=True,
            created_by=actor,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(case)
        await session.flush()

        row.case_id = case.id
        row.handled = True
        row.handled_note = "已转评测用例"
        await session.flush()

        return case_to_dict(case)


async def satisfaction(
    session: AsyncSession,
    tenant_id: str,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """满意度：只有两个真实来源——点赞率和差评原因分布。

    不编「满意度百分比 = 好答案 / 总答案」：我们不知道没点赞的那 90% 是满意
    还是懒得点。分母用「有反馈的回答数」，页面上的措辞也必须是「有反馈的回答中
    点赞占 X%」。
    """
    since = datetime.now(UTC) - timedelta(days=max(1, days))

    with tenant_scope(tenant_id):
        rows = (
            await session.execute(
                select(KnowledgeFeedback.verdict, KnowledgeFeedback.reason, func.count())
                .where(
                    KnowledgeFeedback.tenant_id == tenant_id,
                    KnowledgeFeedback.created_at >= since,
                )
                .group_by(KnowledgeFeedback.verdict, KnowledgeFeedback.reason)
            )
        ).all()

        unhandled = await session.scalar(
            select(func.count(KnowledgeFeedback.id)).where(
                KnowledgeFeedback.tenant_id == tenant_id,
                KnowledgeFeedback.verdict == VERDICT_DOWN,
                KnowledgeFeedback.handled.is_(False),
            )
        )

    up = sum(int(count) for verdict, _, count in rows if verdict == "up")
    down = sum(int(count) for verdict, _, count in rows if verdict == VERDICT_DOWN)
    reasons = {
        str(reason or "unspecified"): int(count) for verdict, reason, count in rows if reason
    }
    total = up + down

    return {
        "window_days": days,
        "up": up,
        "down": down,
        "total": total,
        "like_rate": round(up / total, 4) if total else 0.0,
        "unhandled_down": int(unhandled or 0),
        "reasons": dict(sorted(reasons.items(), key=lambda item: -item[1])),
        "note": "分母是「有反馈的回答数」，不是全部回答数；未表态不代表满意。",
    }


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
