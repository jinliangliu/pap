# app/models/api_token.py
"""个人访问令牌（PAT）：外部宿主以「某个人」的身份调用 PAP。

用于 WorkBuddy 连接器这类程序化入口。请求方带 `Authorization: Bearer <token>`，
服务端据此还原成一个**具体的 `Identity`**——审计里能查到「谁下的单」，
审批有归属，租户隔离照常生效。

## 为什么这张表不继承 `TenantScoped`

鉴权发生在**知道租户之前**：请求里只有一枚令牌，服务端必须先按哈希找到行，
才能知道它属于谁、哪个租户。而 `TenantScoped` 会给所有查询自动追加
`tenant_id = 当前租户`，这条查询当时还没有租户可填。

这和 `users` / `roles` 属于同一类问题——身份平面必须能被认证层跨租户读取，
所以它们同样不参与业务数据的自动过滤（见 `app/models/mixins.py` 与
`docs/MULTI_TENANCY.md`）。代价是：**读这张表的每一处都必须自己带上
`user_id` / `tenant_id` 条件**，写接口尤其不能只按主键取。

## 为什么不存明文、也不加 pepper

令牌是 32 字节随机（见 `api_token_service`），没有可枚举的搜索空间，
库泄露后 sha256 也反推不回去。此处的威胁模型里没有「口令字典攻击」这一项，
加 pepper 只会引入一个「忘了配就悄悄变弱」的配置项。

唯一需要加密的是**传输**——令牌在 HTTP 明文链路上跑时谁都读得到，
所以对外端点必须有 TLS，这一点不能靠哈希弥补。

## 为什么没有 scopes 列

授权一律以数据库为准：角色、权限、站点授权每次现查。
把权限快照写进令牌，撤角色就要等令牌过期才生效——那不是撤销，是延迟。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # 令牌绑定的租户：调用时以它为准，不从请求头取。
    # 同一个人可能在多个租户里有角色，一枚令牌只能代表其中一个身份。
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 主体归属。审计里的 actor_id 就是它。
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # 人给这张令牌起的名字（「笔记本」「工作站 A」），用于区分和回收。
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    # sha256(明文)，十六进制 64 字符。明文只在签发那一次返回，之后任何接口都不再出现。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 明文前 12 个字符，供列表页认出「是哪一枚」。这一段丢了也不足以还原令牌。
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)

    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 撤销是打标记，不是删行：删掉之后审计里的 actor 就对不回一张令牌了。
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 最近一次成功使用。用来回答「这枚还在用吗，能不能收掉」。
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_api_tokens_user_created", "user_id", "created_at"),
    )

    def is_active(self, now: datetime.datetime | None = None) -> bool:
        """能不能用。**只管这两条**，主体是否停用、是否还在该租户，交给身份解析。"""
        if self.revoked_at is not None:
            return False

        if self.expires_at is None:
            return True

        moment = now or datetime.datetime.now(datetime.timezone.utc)
        expires = self.expires_at

        # sqlite 取回来是 naive，PG 是 aware。比较前先对齐，
        # 否则 TypeError 会把一次鉴权失败变成 500。
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.timezone.utc)

        return expires > moment
