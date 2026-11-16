# app/models/agent.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
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


class AgentStatus(str, enum.Enum):
    """智能体生命周期。

    draft    已创建但未启用：可编辑、不可对话，用于先配好边界再放人用；
    active   可被对话选择；
    disabled 已停用：历史会话仍可查看，新请求一律拒绝。

    不设 `archived`：归档与停用在「能不能再启用」上没有区别，
    多一个状态只是让状态机与界面各多一个分支。
    """

    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class Agent(TenantScoped, Base):
    """
    智能体：一个有名有姓、有职责、有边界的执行主体。

    与 Skill 的分工（最容易混的一处）：

    - Agent 回答「**谁**在为用户做事、它的职责边界到哪」——是一个长期存在的角色；
    - Skill 回答「这类活**按什么步骤**做」——是随消息命中、只在当轮生效的作业指令。

    所以 Agent 有生命周期（草稿 / 启用 / 停用）、有审计版本（`revision`）、
    可以被人挑选；Skill 没有这些，它只是提示词片段。

    **边界只收窄，绝不放宽。** 三个字段都不授予任何权限：

    - `allowed_tools`：在「当前身份可见的工具」上再做一次交集，写进来的工具
      若对当前身份不可见，仍然不可调用；
    - `allowed_skills`：同上，只是少给几条作业指令；
    - `can_write`：更严的开关，关掉后写类工具连目录都不出现；打开也**不是**
      审批豁免，写操作照样要人确认。

    反过来，Agent 不会让任何人看到原本看不到的东西：有效权限恒等于
    「人的权限 ∩ Agent 边界」，这条在 ToolGateway 里落地（见 app/tools/gateway.py）。
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
        Index("ix_agents_tenant_status", "tenant_id", "status"),
        # 每个租户只能有一个默认 Agent：不传 agent 的请求全部落到它身上，
        # 有两个就等于「不传时行为不确定」。用部分唯一索引在库里兜住。
        Index(
            "uq_agents_tenant_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
        # 内置智能体（构建助手）同理由库里兜住唯一性：它由首次访问惰性创建，
        # 并发时出现两行就意味着「控制台有两个构建助手」，而它们共享同一个 thread 约定。
        Index(
            "uq_agents_tenant_builtin",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_builtin"),
            sqlite_where=text("is_builtin = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    # 对话时用来指定 Agent 的稳定标识（租户内唯一），改名会破坏已接入的客户端。
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    display_name: Mapped[str] = mapped_column(String(64), nullable=False)

    # 职责说明：会进系统提示词，所以要写成「你负责什么」而不是内部备注。
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    # 更细的作业约束（语气、输出格式、禁止事项）。为空表示只靠 description。
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # None = 不额外收窄；[] = 明确不给任何工具。二者语义不同，不能混用。
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    allowed_skills: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # 允许使用的连接器名。与 `allowed_tools` 是**并集**关系：
    # 工具命中任一即可见，两者都为 None 才是「不额外收窄」。
    #
    # 为什么不复用 `allowed_tools` 把工具名全列上：接一个连接器就是二十个名字，
    # 每次连接器加工具都要改一遍所有相关智能体，改漏一个的表现是
    # 「智能体说它没有这个能力」，而界面看起来完全正常。
    #
    # 代价是连接器内新增的工具会自动进入边界。这是刻意的：连接器是**能力组**，
    # 人对「接入这个系统」的授权本来就包含它后续提供的接口。为了让这件事可追溯，
    # 绑定新工具时会记 `connector_tools_bound` 审计，带上受影响的智能体清单。
    allowed_connectors: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    can_write: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # 委派预算（见 `app/agents/delegation_limits.py` 与 `docs/A2A.md` §8）。
    #
    # 可空而不是给默认值：空表示「没另作要求，走平台默认」。写成 2/3 看起来更
    # 省事，但平台默认值一旦调整，所有存量 Agent 会在无人知情的情况下被钉在旧值上，
    # 而界面上显示的是一个用户从没设过的数字。
    #
    # 两者的语义都是「只能更严」：折进生效上限时取 min，不能凭这一行把整条链放宽。
    # 0 是有意义的配置（禁止转交 / 本轮不许转交），与空值不是一回事。
    max_delegation_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)

    delegation_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[AgentStatus] = mapped_column(
        Enum(
            AgentStatus,
            name="agent_status",
            # 默认按「成员名」落库（ACTIVE），而 PG 原生枚举 agent_status 的标签
            # 是小写 value（draft/active/disabled）——迁移与运维手写 SQL 用的都是后者。
            # 不统一就是线上 500：invalid input value for enum agent_status: "ACTIVE"。
            # 与 tenant.status 同样的坑，同样的解法：一律按 value 落库。
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AgentStatus.DRAFT,
        server_default=AgentStatus.DRAFT.value,
    )

    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # 系统内置：目前只有构建助手（`studio`）。
    #
    # 内置行不允许改边界、停用、删除——「只有内置助手能改配置」这条设计，
    # 一旦允许从旁边改它自己的 allowed_tools，就等于没有。
    # 职责说明（description / instruction）仍可编辑：那是语气偏好，不是能力。
    is_builtin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # 每次边界变更 +1。审批记录与观测会带上它，用来回答
    # 「这次调用发生在哪一版职责边界下」——只记 agent_id 答不了。
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

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
