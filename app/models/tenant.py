# app/models/tenant.py
"""租户、配额与租户级工具策略。

租户是数据归属与资源配额的单位；用户是全局身份，通过 `user_roles.tenant_id`
成为某个租户的成员。角色定义（roles / role_permissions）是平台级安全目录，
不允许租户自行扩大权限范围——租户只能在平台给出的角色里选人。
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class TenantStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class Tenant(Base):
    """租户/组织。id 用可读 slug（tenant_acme），便于放入键名与审计。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[TenantStatus] = mapped_column(
        Enum(
            TenantStatus,
            name="tenant_status",
            native_enum=False,
            validate_strings=True,
            # 默认按「成员名」落库（ACTIVE），但迁移种子与运维手写 SQL 用的都是
            # 小写 value（active）。两边不一致会让读取抛 KeyError('active')，
            # 也让 `status == TenantStatus.ACTIVE` 查不到行。
            # 统一按 value 落库，值就是数据库里看到的东西。
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=TenantStatus.ACTIVE,
        server_default=TenantStatus.ACTIVE.value,
        index=True,
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

    def is_active(self) -> bool:
        return self.status is TenantStatus.ACTIVE


class TenantQuota(TenantScoped, Base):
    """租户配额。值为 0 表示「沿用平台默认上限」。"""

    __tablename__ = "tenant_quotas"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )

    daily_chat_requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    tool_calls_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    knowledge_max_documents: Mapped[int] = mapped_column(
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


# 工具策略的三种动作。它不是「开关」的三种写法，而是三种不同的执行结局：
#   allow   → 只要平台边界不强制，就直接执行；
#   confirm → 无论风险等级，一律先生成审批单；
#   deny    → 目录里看不见，绕过目录直接调用也会被拒。
# 没有记录（action IS NULL）才是「未配置」，此时按平台默认动作处理——
# 与「显式允许」必须区分：默认动作改成拒绝时，显式允许的工具仍要能用。
TOOL_POLICY_ACTIONS = ("allow", "confirm", "deny")

# 风险覆盖只允许**上调**：read → write 是把工具收紧到必须审批。
# 反方向不提供：把 POST 标成「读取」等于给它开一条免审批直通，
# 而这条直通一旦被点过，从返回值与审计上都看不出来。
TOOL_POLICY_RISKS = ("read", "write")


class TenantToolPolicy(TenantScoped, Base):
    """租户级工具策略、凭据引用与风险覆盖。

    - 没有记录 = 未配置，按平台默认动作（`permissions.default_action`）处理；
    - `action` 三态见 `TOOL_POLICY_ACTIONS`；`deny` 的工具模型目录里直接看不到；
    - `risk_override` 只能把风险往上调（读取→写入），保存时校验；
    - `allowed_roles` 非空 = 在平台权限之上再收窄到这些角色；
    - `credential_ref` 只是**环境变量名/密钥管理器里的引用**，绝不存明文密钥。

    为什么不再留一个布尔 `enabled`：`enabled` 与 `action != "deny"` 是同一事实
    的两种写法，两份并存时总有一处先被改。这里只留 `action`，布尔由它派生。
    """

    __tablename__ = "tenant_tool_policies"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), primary_key=True)

    # NULL = 未配置（走平台默认动作）。必须在库里可空，否则「没配过」与
    # 「配成允许」会变成同一行，默认动作一改就把显式允许一起改掉了。
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # NULL = 不覆盖，沿用工具注册时的风险等级。
    risk_override: Mapped[str | None] = mapped_column(String(8), nullable=True)

    allowed_roles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
