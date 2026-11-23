# app/models/write_backup.py
"""写前备份：允许一次写操作之前，先把「将被覆盖的那份状态」原样存下来。

为什么备份挂在平台侧，而不是交给每个工具自己存：

工具自己存，就等于每个写工具都要重新回答同一组问题（存哪、多大算大、
存满了怎么办）。回答得不一致时，最坏的结果不是「没备份」，而是
「看起来备份了」——审计里有一条 backup 记录，实际内容早被裁掉了。

为什么是「快照」而不是「可回滚事务」：

PAP 的写有相当一部分落在外部系统（连接器、MCP、站点），没有跨系统的
回滚。能提供的诚实保证只有一条：**原件还在**。至于怎么用它恢复，
是人事后决定的，平台只负责别让它消失。

表只增不改；`size_bytes` 是为了在写入前算总量（上限语义见
`write_backup_service`），不是为了统计报表。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class WriteBackup(TenantScoped, Base):
    __tablename__ = "write_backups"
    __table_args__ = (
        # 查「这个工具最近备了哪些」与「这轮对话备了什么」是两条主要路径。
        Index("ix_write_backups_tenant_created", "tenant_id", "created_at"),
        Index("ix_write_backups_tenant_tool", "tenant_id", "tool_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 被覆盖的东西是什么。与备份内容同源，不接受调用方另行拼装。
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # 备份时的决策与策略版本：回答「这份备份是在哪一版策略下做的」。
    # 不存这两个，事后只能看到「某天有个备份」，说不出它属于哪套规则。
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # 原样保存的 JSON。**不做压缩**：压缩后的大小与阈值语义就对不上了，
    # 而阈值正是「这份值不值得备」的判据。
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
