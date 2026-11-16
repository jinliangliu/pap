# app/models/rbac.py
"""用户、角色、权限与站点授权。

访问站点资源必须同时满足：
    已认证 + 用户启用 + 属于该租户 + 角色含所需 permission + 已授予目标 station_id
    + 写操作要求 can_write

多租户约束：

- `users` / `roles` / `role_permissions` 属于**身份平面**，不参与业务数据自动过滤；
- `user_roles` 带 tenant_id：同一个账号在 A 租户是管理员，不会让他在 B 租户也是管理员；
- `station_grants` 带 tenant_id：站点属于租户，跨租户的同名 station_id 互不影响。
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TenantScoped


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # 用于对接 OIDC / SSO / 微信等身份系统，不存密码。
    external_subject: Mapped[str] = mapped_column(
        String(256),
        unique=True,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_users_enabled", "enabled"),)


class Role(Base):
    """平台级角色目录。

    角色与权限由平台定义，租户只能给成员分配角色，不能自定义权限集合——
    否则租户管理员可以通过造角色绕过平台的安全边界。
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)


class UserRole(TenantScoped, Base):
    """租户内的角色分配，同时表示「该用户是该租户成员」。

    用代理主键而不是 (tenant_id, user_id, role_id) 复合主键：
    同一用户在同一租户内只应有一个角色，这条约束由唯一索引表达；
    复合主键在跨库迁移时改动成本高，且会让 ORM 的 get() 语义变复杂。
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "role_id",
            name="uq_user_role_tenant_user_role",
        ),
        Index("ix_user_roles_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        index=True,
    )

    user: Mapped[User] = relationship(back_populates="user_roles")
    role: Mapped[Role] = relationship()


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission: Mapped[str] = mapped_column(String(128), primary_key=True)


class StationGrant(TenantScoped, Base):
    __tablename__ = "station_grants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    station_id: Mapped[str] = mapped_column(String(128), index=True)

    can_read: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_write: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "station_id",
            name="uq_station_grant_tenant_user_station",
        ),
    )
