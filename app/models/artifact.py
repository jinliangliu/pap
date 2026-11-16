# app/models/artifact.py
"""上传附件与 Agent 产物的元数据。

对象本身放在对象存储（当前实现为本地目录，路径带租户前缀），
本表只存元数据与对象键。下载必须先经本表校验租户与权限，
不能把对象路径直接给客户端——路径不是权限。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class Artifact(TenantScoped, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_artifacts_tenant_conversation", "tenant_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 形如 tenants/<tenant>/users/<user>/artifacts/<uuid><ext>；
    # 服务端生成，永不接受客户端传入。
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="application/octet-stream",
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
