# app/schemas/chat.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import APIModel


class ChatAttachment(APIModel):
    """对话附带的文件（图片或 PDF）。

    当前只支持 base64 data URL 内联：服务端解码后落盘成句柄，
    不把 base64 写进会话状态。`name` 仅用于前端回显与消息记录，
    不参与类型判断（类型一律按内容嗅探）。
    """

    data_url: str | None = None
    file_id: str | None = None
    name: str | None = Field(default=None, max_length=120)


class ChatRequest(APIModel):
    """
    thread_id 标识一个会话。同一用户不同会话必须使用不同 thread_id。

    thread_id 不是权限凭据：服务端会用 (tenant, user, thread_id) 生成内部线程 ID，
    因此即使猜到别人的 thread_id 也无法读取或恢复对方的会话。
    """

    message: str = Field(min_length=1, max_length=8000)
    thread_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    stream: bool = False

    # 用哪个智能体。留空 = 平台默认 Agent（等价于这套机制不存在）。
    # 只接受租户内已启用的 Agent；草稿或已停用会返回 409，不会静默回落到默认——
    # 「我选了它，它却换了一个人回答」比报错更难排查。
    agent: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="智能体标识或 ID；留空使用默认智能体",
    )
    attachments: list[ChatAttachment] = Field(default_factory=list)
    # 旧字段，等价于 attachments。保留是为了不让已接入的客户端在升级后直接 422；
    # 服务端会把它合并进 attachments，不在别处再读一次。
    images: list[ChatAttachment] = Field(default_factory=list)

    def merged_attachments(self) -> list[ChatAttachment]:
        return [*self.images, *self.attachments]


class ApprovalPendingResponse(APIModel):
    approval_id: str
    preview: dict[str, Any]
    expires_at: str | None = None
    message: str


class ChatResponse(APIModel):
    thread_id: str
    run_id: str

    # `needs_input` 与 `completed` 必须分开。
    #
    # 对人这两种没差别——正文里都写清楚了「请补充站点编号」，界面照常显示。
    # 对程序化调用方（MCP 委派、脚本、其它智能体）差别是致命的：
    # `completed` + 一句反问会被当成「这就是结论」原样转述出去，
    # 而事实是这一轮什么都没办成。状态是平台事实，不该由每个客户端
    # 各自拿 `metadata.clarify` 非空去猜。
    status: Literal[
        "completed",
        "needs_input",
        "awaiting_approval",
        "error",
        "rejected",
    ]

    answer: str | None = None

    approval: ApprovalPendingResponse | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ocr_text: str | None = None
    error_category: str | None = None
    # 两条消息在库里的落库时刻。前端气泡上标的是这个值，不是浏览器本地时钟——
    # 否则同一句话「刚发出去」和「刷新后」会显示两个时间（客户端时钟不一定准）。
    user_message_at: datetime | None = None
    assistant_message_at: datetime | None = None
