# app/models/context.py
"""会话上下文摘要（长对话压缩的产物）。

与 `memory` 的分工：记忆是跨会话的长期事实，
摘要是本次对话的叙事与指代，跟着会话一起删。

append-only：每次压缩落一行，最新一行生效。留着旧行才能回答
「这次压缩前后各多少 token」「重建之后和上一版差在哪」——
只存一个字段的话，压缩质量永远无法复盘。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class ConversationSummary(TenantScoped, Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )

    # 水位：这份摘要覆盖到哪条消息为止。用「时间 + id」两个字段，
    # 因为 messages 表没有自增序号，同一毫秒写入的两条消息只能靠 id 定序。
    through_message_id: Mapped[uuid.UUID] = mapped_column()
    through_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    turns_covered: Mapped[int] = mapped_column(Integer, default=0)
    messages_covered: Mapped[int] = mapped_column(Integer, default=0)

    # 被压缩部分与压缩结果的体量：两个数放在一起才看得出「压了有没有用」。
    tokens_before: Mapped[int] = mapped_column(Integer, default=0)
    tokens_after: Mapped[int] = mapped_column(Integer, default=0)

    summary: Mapped[str] = mapped_column(Text)
    summary_chars: Mapped[int] = mapped_column(Integer, default=0)

    # 产出摘要的模型。复盘「摘要变差了」时要先排除「模型换了」。
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 全量重建还是增量。
    rebuilt: Mapped[bool] = mapped_column(Boolean, default=False)
    # 模型失败/超时落下的兜底摘要。这类行照常注入，但要在观测里能分辨。
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_conversation_summaries_conv_created",
            "conversation_id",
            "created_at",
        ),
    )
