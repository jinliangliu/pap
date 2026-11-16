# app/models/memory.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class MemoryType(str, enum.Enum):
    """长期记忆类别。"""

    PREFERENCE = "preference"
    PROFILE = "profile"
    FACT = "fact"
    DECISION = "decision"
    WORKING_NOTE = "working_note"


class MemoryStatus(str, enum.Enum):
    """记忆可用状态。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class Memory(TenantScoped, Base):
    """
    用户或租户级长期记忆。

    该表不等价于完整聊天记录：
    - 聊天记录由 LangGraph Checkpointer 或独立 Conversation 表管理；
    - Memory 仅存已经被确认、归纳或显式保存的稳定信息；
    - 不应自动把模型猜测写入这里。
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_tenant_user_status",
            "tenant_id",
            "user_id",
            "status",
        ),
        Index(
            "ix_memories_tenant_type_status",
            "tenant_id",
            "memory_type",
            "status",
        ),
        Index(
            "ix_memories_expires_at",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(
            MemoryType,
            name="memory_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=MemoryType.FACT,
    )
    status: Mapped[MemoryStatus] = mapped_column(
        Enum(
            MemoryStatus,
            name="memory_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=MemoryStatus.ACTIVE,
        server_default=MemoryStatus.ACTIVE.value,
    )

    title: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    source: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="user_confirmed",
        server_default="user_confirmed",
    )
    source_thread_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
    )
    is_user_confirmed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    metadata_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MemoryReviewAction(str, enum.Enum):
    """梦境建议的动作。

    四类之外不设「删除」：模型没有权限替用户决定哪条记忆该消失。
    判为无效时只能 `retire`（置 SUPERSEDED，留档可追溯），
    真正的删除仍由人在页面上点。
    """

    CORRECT = "correct"
    MERGE = "merge"
    RETIRE = "retire"
    KEEP = "keep"


class MemoryReviewStatus(str, enum.Enum):
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class MemoryReview(TenantScoped, Base):
    """一条待人工确认的梦境建议。

    为什么不把建议塞进 `memories.metadata_json`：

    - 建议有独立生命周期，同一条记忆会反复被整理，塞 JSON 会越滚越乱；
    - 要回答「是谁、基于哪几条消息、改成了什么」，这些必须能查；
    - 记忆被物理删除时建议应一起消失，外键 CASCADE 比在 JSON 里打标记干净。

    模型只产出建议，**不直接改记忆**。应用动作由接口按人的确认执行。
    """

    __tablename__ = "memory_reviews"
    __table_args__ = (
        Index(
            "ix_memory_reviews_tenant_user_status",
            "tenant_id",
            "user_id",
            "status",
        ),
        Index(
            "ix_memory_reviews_tenant_memory_status",
            "tenant_id",
            "memory_id",
            "status",
        ),
        Index(
            "ix_memory_reviews_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    # 最小版只整理个人记忆；团队共享记忆的改动涉及所有人，
    # 不在「离线跑一遍」的权限范围内。
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    action: Mapped[MemoryReviewAction] = mapped_column(
        Enum(
            MemoryReviewAction,
            name="memory_review_action",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    status: Mapped[MemoryReviewStatus] = mapped_column(
        Enum(
            MemoryReviewStatus,
            name="memory_review_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default=MemoryReviewStatus.PENDING,
        server_default=MemoryReviewStatus.PENDING.value,
    )

    # 为什么这么改。给页面直接显示，不让人再去翻模型输出。
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    proposed_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    proposed_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # merge 时指向保留的那条记忆。不设外键：目标被删后建议应变成
    # 「无法应用」，而不是级联把建议也删掉、让人不知道为什么少了一条。
    merge_target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # [{"message_id": "...", "conversation_id": "...", "excerpt": "...", "created_at": "..."}]
    # 只存摘录，不存整段会话；会话可以被清理，建议的审计价值不该依赖它。
    evidence_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0.0",
    )

    # 复现与回归用：换模型或改 prompt 后，同一批建议是否变化要能解释。
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    prompt_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default=""
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class TenantMemorySettings(TenantScoped, Base):
    """租户级记忆配置。**没有记录 = 全部沿用平台默认**。

    值的表达方式刻意沿用 `TenantQuota` 的约定——`0` 表示「沿用平台默认」，
    而不是把当前默认值复制一份进来。理由：平台后来把默认回溯窗口从 30 天调到 14 天，
    复制过默认值的租户会一直钉在 30 天，且没人知道为什么它不一样。

    `dream_enabled` 用三态（NULL / true / false）而不是 bool 默认 True：
    布尔列无法区分「没配过」和「明确关掉」，会把未配置的租户当成已关闭。
    """

    __tablename__ = "tenant_memory_settings"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # NULL = 跟随平台开关；False = 该租户主动停用梦境。
    # 注意方向：平台层关闭时租户**不能**反向打开，见 memory_dream.resolve_dream_config。
    dream_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # 0 = 沿用平台默认（取 settings.memory_dream_days）。
    dream_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # 0 = 沿用平台默认（取 settings.memory_dream_max_per_run）。
    dream_max_per_run: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # 谁最后改的。配置会影响「模型怎么判断用户记忆」，出问题要能追到人。
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
