# app/models/conversation.py
"""会话、消息与运行记录。

第 2 批要求的五张基础表之一部分。此前 conversations / messages /
agent_runs 三张表在代码里完全缺失，只有 approval 与 audit。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TenantScoped


class MessageRole(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class RunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Conversation(TenantScoped, Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[str] = mapped_column(String(128), index=True)

    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="api")

    # LangGraph checkpointer 的会话标识，与 conversations.id 分离，
    # 便于重建图状态而不影响业务记录。
    thread_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # 上一轮实测的 prompt token 数（供应商报的值，经回调收集后落库）。
    # 放业务表而不是只记在观测里：观测可以整体关掉，而「这一轮该不该压缩」
    # 不能因为开关状态而失去依据。见 docs/CONTEXT.md §4。
    last_prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_conversations_tenant_user_active", "tenant_id", "user_id", "last_active_at"),
    )


class Message(TenantScoped, Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )

    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole))
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(32), default="text/plain")

    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentRun(TenantScoped, Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)

    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus),
        default=RunStatus.PENDING,
        index=True,
    )

    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
