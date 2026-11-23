# app/schemas/conversation.py
"""对话台会话历史的请求/响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.common import APIModel, as_utc


class ConversationSummary(APIModel):
    id: uuid.UUID
    # 客户端可见的线程标识：前端切换历史会话后必须用它继续对话，
    # 内部 ID（tenant:user:client）不对外暴露。
    thread_id: str
    title: str
    channel: str
    created_at: datetime
    last_active_at: datetime
    message_count: int = 0

    @field_validator("created_at", "last_active_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ConversationListResponse(APIModel):
    items: list[ConversationSummary] = Field(default_factory=list)
    total: int = 0


class ConversationAttachment(APIModel):
    """消息里的附件，供前端回显缩略图 / PDF 预览 / 文本文件卡。

    `url` 是取回原文件的接口地址，不是磁盘路径：
    磁盘路径属于服务端实现，暴露出去等于泄露目录结构。
    """

    attachment_id: str
    kind: str  # image | pdf | text
    mime: str
    size: int
    name: str | None = None
    url: str | None = None


class ConversationMessage(APIModel):
    id: uuid.UUID
    role: str
    content: str
    metadata: dict = Field(default_factory=dict)
    attachments: list[ConversationAttachment] = Field(default_factory=list)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ConversationContextSummary(APIModel):
    """一次压缩的产物，按时间正序给出全部历史。

    前端只用**最新一条**的水位来插分隔条（更早的轮次都已折进这一次的正文），
    但把历史整串返回：排查「摘要怎么越写越短」时要对比前后两版，
    只给最新一条等于逼人去翻 `conversation_summaries` 表。

    `through_message_id` 是水位：**到这条消息为止（含）**已被折叠。
    不返回 `tokens_before/after` 之外的统计——它们属于观测，不属于会话。
    """

    id: uuid.UUID
    through_message_id: uuid.UUID
    turns_covered: int = 0
    messages_covered: int = 0
    summary: str
    tokens_before: int = 0
    tokens_after: int = 0
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ConversationContextCleared(APIModel):
    """`/clear` 的清空点。

    `through_message_id` 是清空时那条回复的消息 id：前端要「插在它之后」，
    而消息表没有自增序号，只能靠 id 定位。

    为什么要回给前端：历史确实还在（那是审计），但已经不参与推断了。
    界面上不标注的话，唯一的解读就是「清空没生效」——用户会再点一次，
    然后发现还是这样。
    """

    at: datetime
    through_message_id: uuid.UUID

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class ConversationDetailResponse(APIModel):
    conversation: ConversationSummary
    messages: list[ConversationMessage] = Field(default_factory=list)
    # 没压过就是空数组，前端据此不插分隔条。默认值必须有：
    # 少数字段让老前端整页报错，比少一条分隔条严重得多。
    summaries: list[ConversationContextSummary] = Field(default_factory=list)
    # 没清过就是 None。同理，必须给默认值。
    context_cleared: ConversationContextCleared | None = None


class ConversationRenameRequest(APIModel):
    title: str = Field(min_length=1, max_length=120)
