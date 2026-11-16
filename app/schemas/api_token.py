# app/schemas/api_token.py
"""个人访问令牌的接口契约。

一条贯穿的规则：**只有签发响应里出现过一次明文**。
列表、撤销等任何其它响应都不带 `token` 字段——这是契约层面的保证，
不是靠调用方自觉不去读。
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import Field

from app.schemas.common import APIModel


class TokenCreateRequest(APIModel):
    name: str = Field(
        default="",
        max_length=64,
        description="给人看的名字，用于区分用途（「笔记本」「工作站 A」）。",
    )
    # 0 或省略表示不过期。过期不是安全兜底，只是让「签过就忘了」的令牌
    # 自己退出；真正的止损手段是撤销。
    expires_in_days: int = Field(
        default=90,
        ge=0,
        le=3650,
        description="有效天数，0 表示不过期。默认 90。",
    )


class TokenItem(APIModel):
    id: uuid.UUID
    name: str
    # 明文前 12 个字符。列表靠它认出是哪一枚，它本身不能用来调用。
    prefix: str
    tenant_id: str
    created_at: datetime.datetime
    expires_at: datetime.datetime | None = None
    last_used_at: datetime.datetime | None = None
    # 撤销时间。与 expires_at 分开返回，页面才能把「已撤销」和「已过期」
    # 说成两句话——它们的处置方式完全不同（前者是有人主动收了，
    # 后者是忘了续期），合成一句「不可用」会让人去查一个不存在的问题。
    revoked_at: datetime.datetime | None = None
    # 是否还能用：没过期且没撤销。前端不用自己算，两边算法必然会出现分叉。
    active: bool = True


class TokenCreateResponse(TokenItem):
    # 明文，仅此一次。刷新页面、再查列表都不会再出现。
    token: str
    hint: str = (
        "这串令牌只显示这一次。复制到连接器配置里，"
        "之后在列表里只能看到前缀——忘了就撤销重签。"
    )


class TokenListResponse(APIModel):
    items: list[TokenItem] = Field(default_factory=list)
