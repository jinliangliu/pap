# app/repositories/model_provider.py
from __future__ import annotations

import datetime

from sqlalchemy import select, update

from app.models.model_provider import ModelProvider


class ModelProviderRepository:
    """model_providers 的读写。

    约束：同一时刻只允许一个 is_active=True。
    激活走 activate()，它在同一事务里先把其余行置为 False，
    避免出现「两个都在用」导致模型选择不确定。
    """

    def __init__(self, session) -> None:
        self.session = session

    async def list_all(self) -> list[ModelProvider]:
        rows = await self.session.scalars(
            select(ModelProvider).order_by(
                ModelProvider.is_active.desc(), ModelProvider.id.asc()
            )
        )
        return list(rows)

    async def get(self, provider_id: int) -> ModelProvider | None:
        return await self.session.get(ModelProvider, provider_id)

    async def get_by_name(self, name: str) -> ModelProvider | None:
        return await self.session.scalar(
            select(ModelProvider).where(ModelProvider.name == name)
        )

    async def get_active(self) -> ModelProvider | None:
        return await self.session.scalar(
            select(ModelProvider)
            .where(ModelProvider.is_active.is_(True))
            .order_by(ModelProvider.id.asc())
            .limit(1)
        )

    async def create(self, data: dict) -> ModelProvider:
        row = ModelProvider(**data)
        self.session.add(row)
        await self.session.flush()

        # 首个供应商自动生效：否则用户保存完发现「没有任何一个在用」。
        if not data.get("is_active") and await self.get_active() is None:
            row.is_active = True
            await self.session.flush()

        return row

    async def update(self, provider_id: int, data: dict) -> ModelProvider | None:
        row = await self.session.get(ModelProvider, provider_id)
        if row is None:
            return None

        for key, value in data.items():
            if key == "is_active":
                continue
            if value is not None:
                setattr(row, key, value)

        row.updated_at = datetime.datetime.now(datetime.UTC)
        await self.session.flush()
        return row

    async def activate(self, provider_id: int) -> ModelProvider | None:
        row = await self.session.get(ModelProvider, provider_id)
        if row is None:
            return None

        await self.session.execute(
            update(ModelProvider)
            .where(ModelProvider.id != provider_id)
            .values(is_active=False)
        )
        row.is_active = True
        row.updated_at = datetime.datetime.now(datetime.UTC)
        await self.session.flush()
        return row

    async def delete(self, provider_id: int) -> bool:
        row = await self.session.get(ModelProvider, provider_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True
