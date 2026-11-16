# app/models/skill.py
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class Skill(TenantScoped, Base):
    """
    技能：一段可复用的作业指令 + 触发条件 + 可选的工具白名单。

    与工具的分工：
    - 工具回答「能调用什么」，注册在 ToolRegistry；
    - 技能回答「这类活按什么步骤、用哪些工具、产出什么格式」。

    技能**不授予任何权限**：`allowed_tools` 只能从当前身份可见的工具里取交集，
    写进技能却不可见的工具会被丢弃，不会因此变得可调用。
    """

    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),
        Index("ix_skills_tenant_enabled", "tenant_id", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    # 给人和 URL 用的稳定标识。保存后不可改：改名等于换了一个技能，
    # 历史命中与用户习惯记的 `/旧名` 都会失效。
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    display_name: Mapped[str] = mapped_column(String(64), nullable=False)

    # 一句话说明「什么时候用我」。既给人看，也给召回匹配做兜底参考。
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="")

    # 作业指令正文（Markdown）。长度上限在 skill_service 里，不写死进库。
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 触发词：消息里出现任意一个即命中。空数组合法（只能靠 /名字 显式调用）。
    trigger_keywords: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    # None = 不收窄工具目录；[] = 纯指令技能（一个工具都不给）；数组 = 精确白名单。
    # 三种语义必须区分开，别用空数组表示「不限制」——那样纯指令技能就没法表达。
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # 多个技能同时命中时取最大。同分时取触发词更长的那个（更具体的优先）。
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual"
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SkillHit(TenantScoped, Base):
    """一次技能命中。只追加，不修改。

    为什么不复用 `obs_spans`：观测有保留期（默认 14 天），还可以被
    `OBS_ENABLED=false` 整体关掉。技能命中趋势是「评估指令写得好不好」的依据，
    不该跟着诊断数据的开关一起消失，所以单独存一张小表。

    与 `obs_traces` 用 `trace_id` 软关联，不建外键——观测会被清理，
    命中记录不能跟着被约束住。
    """

    __tablename__ = "skill_hits"
    __table_args__ = (
        # 页面要的是「某技能近 7 天几次」，这条索引正好覆盖该查询。
        Index("ix_skill_hits_skill_created", "tenant_id", "skill_id", "created_at"),
        Index("ix_skill_hits_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # 技能删除时命中记录一并删除：留着指向不存在技能的计数没有意义，
    # 而「删掉重建」本来就是一次重新计数。
    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 冗余技能名：导出与排障时不必回连 skills，也不怕它已被改名。
    skill_name: Mapped[str] = mapped_column(String(64), nullable=False)

    # explicit | keyword。评估「显式调用 vs 自然语言触发」的比例，
    # 直接反映触发词配得好不好。
    matched_by: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    keyword: Mapped[str | None] = mapped_column(String(64), nullable=True)

    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
