# app/repositories/approval_repository.py
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import ApprovalRequest


class ApprovalRepository:
    """ApprovalRequest 的数据库访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        approval: ApprovalRequest,
    ) -> ApprovalRequest:
        self._session.add(approval)
        await self._session.flush()
        return approval

    async def get_for_user(
        self,
        *,
        approval_id: uuid.UUID,
        tenant_id: str,
        user_id: str,
    ) -> ApprovalRequest | None:
        statement: Select = select(ApprovalRequest).where(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.user_id == user_id,
        )

        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        *,
        approval_ids: Sequence[uuid.UUID],
        tenant_id: str,
        user_id: str,
    ) -> list[ApprovalRequest]:
        """按 id 批量取，仍然只返回本人的单子。

        刻意不抛「找不到」：一条历史会话里可能留着已被保留期清理掉的审批号，
        一次批量读里混进一个失效 id 不该让整张列表失败。调用方按「有没有这一条」
        自己区分，缺的那张自然会显示成「已失效」。
        """
        if not approval_ids:
            return []

        statement: Select = select(ApprovalRequest).where(
            ApprovalRequest.id.in_(list(approval_ids)),
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.user_id == user_id,
        )

        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def get_for_update(
        self,
        *,
        approval_id: uuid.UUID,
        tenant_id: str,
        user_id: str,
    ) -> ApprovalRequest | None:
        """
        行锁读取。

        用于确认、拒绝或执行前的状态转换，
        避免两次并发确认引发重复执行。
        """
        statement: Select = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.user_id == user_id,
            )
            .with_for_update()
        )

        result = await self._session.execute(statement)
        return result.scalar_one_or_none()
