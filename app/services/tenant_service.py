# app/services/tenant_service.py
"""租户级配置：配额、工具策略、成员管理。

被三处使用：

- ChatService：每日对话配额；
- ToolGateway：租户是否允许该工具、可使用的角色、租户凭据引用；
- 平台管理 API：开户、加/移成员、配额与策略维护。

缓存：策略与配额读多写少，进程内缓存 `TENANT_POLICY_CACHE_SECONDS`（默认 5 秒）。
管理接口写入后调用 `invalidate()`，因此同进程立即生效；
多实例部署时其他实例最多晚一个缓存周期。这个窗口是刻意的：
把它设成 0 等于每次工具调用都多一次数据库往返。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.rbac import Role, User, UserRole
from app.models.tenant import (
    TOOL_POLICY_ACTIONS,
    TOOL_POLICY_RISKS,
    Tenant,
    TenantQuota,
    TenantStatus,
    TenantToolPolicy,
)
from app.services.rate_limit_service import RateLimiterProtocol, get_rate_limiter
from app.tenancy.context import normalize_tenant_id, tenant_scope

logger = logging.getLogger(__name__)

# 凭据引用只能是环境变量名：真正的密钥不进数据库，也不会出现在日志里。
_CREDENTIAL_REF_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    daily_chat_requests: int = 0
    tool_calls_per_minute: int = 0
    knowledge_max_documents: int = 0

    def resolved_daily_chat_requests(self) -> int:
        return self.daily_chat_requests or settings.default_daily_chat_requests

    def resolved_tool_calls_per_minute(self) -> int:
        return self.tool_calls_per_minute or settings.default_tenant_tool_calls_per_minute


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """一个工具在某租户下的策略快照。

    `action=None` 是「未配置」，不是「允许」：平台默认动作改成拒绝时，
    只有显式配成 allow 的工具还能用——这正是区分两者的理由。
    """

    action: str | None = None
    allowed_roles: tuple[str, ...] = ()
    credential_ref: str | None = None
    risk_override: str | None = None

    @property
    def configured(self) -> bool:
        return self.action is not None

    @property
    def enabled(self) -> bool:
        """是否对该租户开放。`deny` 之外都算开放（confirm 只是要审批）。"""
        return self.action != "deny"


@dataclass(frozen=True, slots=True)
class TenantSettings:
    tenant_id: str
    quota: QuotaLimits = field(default_factory=QuotaLimits)
    tools: dict[str, ToolPolicy] = field(default_factory=dict)
    loaded_at: float = 0.0

    def policy_for(self, tool_name: str) -> ToolPolicy:
        # 未配置 = 未配置（action=None），由调用方按平台默认动作解释。
        return self.tools.get(tool_name, ToolPolicy())

    def action_for(self, tool_name: str, *, default_action: str = "allow") -> str:
        """解析最终动作：库里配了以库为准，没配才用平台默认。"""
        return self.policy_for(tool_name).action or default_action

    def allow_tool(
        self,
        tool_name: str,
        roles: tuple[str, ...],
        *,
        default_action: str = "allow",
    ) -> bool:
        policy = self.policy_for(tool_name)

        # 显式拒绝优先于角色判断：一个被下线的工具不该因为「角色对上了」
        # 又回到某些人的目录里。
        if not policy.enabled:
            return False

        if policy.action is None and default_action == "deny":
            # 未配置 + 平台默认拒绝 = 只有显式 allow / confirm 的行才放行。
            return False

        if policy.allowed_roles and not set(policy.allowed_roles) & set(roles):
            return False

        return True


class TenantService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        limiter: RateLimiterProtocol | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session_factory
        self._limiter = limiter or get_rate_limiter()
        self._cache: dict[str, TenantSettings] = {}

    # ---------------- 读 ----------------

    async def settings_for(self, tenant_id: str, *, force: bool = False) -> TenantSettings:
        target = normalize_tenant_id(tenant_id)
        cached = self._cache.get(target)

        if (
            cached is not None
            and not force
            and time.monotonic() - cached.loaded_at < settings.tenant_policy_cache_seconds
        ):
            return cached

        settings_ = await self._load(target)
        self._cache[target] = settings_
        return settings_

    def cached(self, tenant_id: str) -> TenantSettings | None:
        return self._cache.get(normalize_tenant_id(tenant_id))

    async def _load(self, tenant_id: str) -> TenantSettings:
        async with self._session_factory() as session:
            with tenant_scope(tenant_id):
                quota = await session.get(TenantQuota, tenant_id)
                rows = (
                    await session.scalars(
                        select(TenantToolPolicy).where(
                            TenantToolPolicy.tenant_id == tenant_id
                        )
                    )
                ).all()

        quota_limits = QuotaLimits(
            daily_chat_requests=int(quota.daily_chat_requests) if quota else 0,
            tool_calls_per_minute=int(quota.tool_calls_per_minute) if quota else 0,
            knowledge_max_documents=int(quota.knowledge_max_documents) if quota else 0,
        )

        tools = {
            row.tool_name: ToolPolicy(
                action=row.action,
                allowed_roles=tuple(row.allowed_roles or ()),
                credential_ref=row.credential_ref,
                risk_override=row.risk_override,
            )
            for row in rows
        }

        return TenantSettings(
            tenant_id=tenant_id,
            quota=quota_limits,
            tools=tools,
            loaded_at=time.monotonic(),
        )

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._cache.clear()
            return

        self._cache.pop(normalize_tenant_id(tenant_id), None)

    # ---------------- 配额 ----------------

    async def consume_chat_quota(self, tenant_id: str) -> None:
        """消耗一次对话配额。超限抛 TenantQuotaExceeded。"""
        tenant_settings = await self.settings_for(tenant_id)

        await self._limiter.enforce_daily_quota(
            tenant_id=tenant_id,
            resource="chat",
            limit=tenant_settings.quota.resolved_daily_chat_requests(),
        )

    # ---------------- 成员 ----------------

    async def add_member(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        subject: str,
        role_name: str,
    ) -> tuple[User, Role]:
        """把账号加入租户。账号不存在时创建（仅限管理通道调用）。"""
        target = normalize_tenant_id(tenant_id)

        role = await session.scalar(select(Role).where(Role.name == role_name))

        if role is None:
            raise ValueError(f"角色不存在：{role_name}")

        user = await session.scalar(select(User).where(User.external_subject == subject))

        if user is None:
            user = User(external_subject=subject, display_name=subject, enabled=True)
            session.add(user)
            await session.flush()

        existing = await session.scalar(
            select(UserRole).where(
                UserRole.tenant_id == target,
                UserRole.user_id == user.id,
            )
        )

        if existing is None:
            session.add(
                UserRole(tenant_id=target, user_id=user.id, role_id=role.id)
            )
        else:
            existing.role_id = role.id

        await session.flush()
        return user, role

    async def remove_member(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        subject: str,
    ) -> bool:
        target = normalize_tenant_id(tenant_id)

        user = await session.scalar(select(User).where(User.external_subject == subject))

        if user is None:
            return False

        result = await session.execute(
            delete(UserRole).where(
                UserRole.tenant_id == target,
                UserRole.user_id == user.id,
            )
        )

        return bool(result.rowcount)

    async def list_members(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
    ) -> list[dict[str, object]]:
        target = normalize_tenant_id(tenant_id)

        rows = (
            await session.execute(
                select(User.external_subject, User.display_name, Role.name, User.enabled)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.tenant_id == target)
                .order_by(User.external_subject)
            )
        ).all()

        return [
            {
                "subject": subject,
                "display_name": display_name,
                "role": role,
                "enabled": bool(enabled),
            }
            for subject, display_name, role, enabled in rows
        ]

    # ---------------- 租户 ----------------

    async def create_tenant(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        name: str,
        daily_chat_requests: int = 0,
        tool_calls_per_minute: int = 0,
    ) -> Tenant:
        target = normalize_tenant_id(tenant_id)

        if await session.get(Tenant, target) is not None:
            raise ValueError("租户已存在。")

        tenant = Tenant(id=target, name=name or target, status=TenantStatus.ACTIVE)
        session.add(tenant)
        await session.flush()

        with tenant_scope(target):
            session.add(
                TenantQuota(
                    tenant_id=target,
                    daily_chat_requests=max(0, daily_chat_requests),
                    tool_calls_per_minute=max(0, tool_calls_per_minute),
                )
            )
            await session.flush()

        return tenant

    async def set_status(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        status: TenantStatus,
    ) -> Tenant:
        tenant = await session.get(Tenant, normalize_tenant_id(tenant_id))

        if tenant is None:
            raise ValueError("租户不存在。")

        tenant.status = status
        await session.flush()
        return tenant

    async def set_quota(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        daily_chat_requests: int,
        tool_calls_per_minute: int,
        knowledge_max_documents: int = 0,
    ) -> TenantQuota:
        target = normalize_tenant_id(tenant_id)

        with tenant_scope(target):
            quota = await session.get(TenantQuota, target)

            if quota is None:
                quota = TenantQuota(tenant_id=target)
                session.add(quota)

            quota.daily_chat_requests = max(0, daily_chat_requests)
            quota.tool_calls_per_minute = max(0, tool_calls_per_minute)
            quota.knowledge_max_documents = max(0, knowledge_max_documents)
            await session.flush()

        self.invalidate(target)
        return quota

    async def set_tool_policy(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        tool_name: str,
        action: str | None,
        allowed_roles: list[str] | None = None,
        credential_ref: str | None = None,
        risk_override: str | None = None,
    ) -> TenantToolPolicy | None:
        """写入一个工具策略。

        `action=None` 表示「恢复为未配置」——此时若其余字段也为空，整行删除。
        留一行全空的记录会让「没配过」与「配过又清空」在库里长得不一样，
        而它们的行为必须完全一致。

        风险覆盖只能上调这条规则**不在这里**判：服务层看不到工具注册表，
        只有路由层知道这个工具真实的风险等级。
        """
        target = normalize_tenant_id(tenant_id)
        ref = (credential_ref or "").strip() or None

        if ref is not None and not _CREDENTIAL_REF_PATTERN.match(ref):
            raise ValueError(
                "credential_ref 必须是环境变量名（大写字母开头，仅含大写字母/数字/下划线）。"
            )

        if action is not None and action not in TOOL_POLICY_ACTIONS:
            raise ValueError(f"action 只能是 {'/'.join(TOOL_POLICY_ACTIONS)}。")

        if risk_override is not None and risk_override not in TOOL_POLICY_RISKS:
            raise ValueError(f"risk_override 只能是 {'/'.join(TOOL_POLICY_RISKS)}。")

        roles = list(allowed_roles or [])

        with tenant_scope(target):
            row = await session.get(
                TenantToolPolicy,
                {"tenant_id": target, "tool_name": tool_name},
            )

            if action is None and risk_override is None and not roles and ref is None:
                if row is not None:
                    await session.delete(row)
                    await session.flush()
                self.invalidate(target)
                return None

            if row is None:
                row = TenantToolPolicy(tenant_id=target, tool_name=tool_name)
                session.add(row)

            row.action = action
            row.risk_override = risk_override
            row.allowed_roles = roles
            row.credential_ref = ref
            await session.flush()

        self.invalidate(target)
        return row

    # ---------------- 凭据 ----------------

    @staticmethod
    def resolve_credential(ref: str | None) -> str | None:
        """把引用解析成密钥。只从环境变量读，绝不落库、绝不记日志。"""
        if not ref:
            return None

        if not _CREDENTIAL_REF_PATTERN.match(ref):
            logger.warning(
                "租户凭据引用格式非法，已忽略。",
                extra={"event": "tenant_credential_ref_invalid"},
            )
            return None

        value = os.environ.get(ref)

        if not value:
            logger.warning(
                "租户凭据未配置。",
                extra={"event": "tenant_credential_missing", "credential_ref": ref},
            )
            return None

        return value


@lru_cache(maxsize=1)
def get_tenant_service() -> TenantService:
    return TenantService()


def reset_tenant_service() -> None:
    """测试用：清空单例与缓存。"""
    get_tenant_service.cache_clear()
