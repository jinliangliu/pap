# app/schemas/agent.py
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import APIModel


class AgentItem(APIModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str = ""
    instruction: str = ""

    # null = 不额外收窄；[] = 一个都不给。页面必须能区分这两者，
    # 否则「没配」会被渲染成「配了但空」。
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    # 连接器名。与 allowed_tools 是并集：接一个外部系统只需要一个名字。
    allowed_connectors: list[str] | None = None
    can_write: bool = False

    # 委派预算。null = 平台默认；0 = 不许转交（不是「没配」）。
    max_delegation_depth: int | None = None
    delegation_budget: int | None = None
    status: str
    is_default: bool = False
    # 内置智能体（构建助手）：边界与状态由平台管，前端据此隐藏编辑/停用/删除入口。
    is_builtin: bool = False
    revision: int = 1

    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None

    # 只有 active 能被对话选中。页面据此决定下拉里是否可选。
    usable: bool = False


class AgentListResponse(APIModel):
    items: list[AgentItem] = Field(default_factory=list)
    total: int = 0
    # 当前身份能否管理这些 Agent（新建 / 修改 / 停用）。
    # 只读成员也要能看到清单，否则不知道「对话时选谁」有哪些选项。
    can_manage: bool = False
    counts: dict[str, int] = Field(default_factory=dict)


class AgentCreateRequest(APIModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=512)
    instruction: str = Field(default="", max_length=4000)

    # 不传 = 不收窄（与平台默认一致）；传空数组 = 明确一个工具都不给。
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    allowed_connectors: list[str] | None = None

    # 默认只读：新建一个能写外部系统的 Agent 必须是显式动作。
    can_write: bool = False

    # 委派预算。不传 = 平台默认（当前 2 跳 / 3 次）；0 是「不许转交」。
    max_delegation_depth: int | None = Field(default=None, ge=0, le=5)
    delegation_budget: int | None = Field(default=None, ge=0, le=10)

    # 默认草稿：先配边界、再启用，避免一个半成品直接对外。
    status: str = "draft"


class AgentUpdateRequest(APIModel):
    """全部可选：未传的字段保持原值。

    `allowed_tools` / `allowed_skills` 用 `model_fields_set` 区分
    「没传」与「显式传 null」——后者表示明确取消收窄，语义完全不同。
    """

    display_name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=512)
    instruction: str | None = Field(default=None, max_length=4000)
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    allowed_connectors: list[str] | None = None
    can_write: bool | None = None
    # 同 allowed_tools：不传保持原值，显式传 null 才是「回到平台默认」。
    max_delegation_depth: int | None = Field(default=None, ge=0, le=5)
    delegation_budget: int | None = Field(default=None, ge=0, le=10)


class AgentStatusRequest(APIModel):
    status: str = Field(pattern=r"^(draft|active|disabled)$")


class AgentCapabilityTool(APIModel):
    name: str
    display_name: str
    risk_level: str
    requires_approval: bool


class AgentCapabilitiesResponse(APIModel):
    """一个智能体的能力目录。

    运行时算出来的，不落库：能力来自「调用者可见的工具 ∩ 该 Agent 的 allowed_tools」，
    把手写清单存进表里，第一次改边界之后就会过期。
    """

    agent: str
    display_name: str
    description: str
    revision: int
    status: str
    can_write: bool

    # 能否作为转交目标。原因写清楚，否则「为什么不能选它」只能靠猜。
    delegatable: bool = False
    delegatable_reason: str | None = None

    # 按**调用者**的可见性求交后的结果，不是该 Agent 的全量能力。
    tools: list[AgentCapabilityTool] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    # 该 Agent 被授权接入的连接器。工具已经按它展开过，这里列名字是为了
    # 让人能在审批时说出「它接的是哪几个外部系统」。
    connectors: list[str] = Field(default_factory=list)
