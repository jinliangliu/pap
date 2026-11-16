# app/schemas/skill.py
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import APIModel
from app.services.skill_service import MAX_KEYWORD_CHARS, MAX_KEYWORDS


class SkillItem(APIModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str
    instruction: str
    trigger_keywords: list[str] = Field(default_factory=list)
    # null = 不收窄工具；[] = 一个工具都不给。
    allowed_tools: list[str] | None = None
    priority: int = 100
    enabled: bool = True
    source: str = "manual"
    created_at: datetime
    updated_at: datetime

    # 配了但当前身份看不到的工具。页面据此显示「不可用」，
    # 而不是让用户以为写了就会生效。
    missing_tools: list[str] = Field(default_factory=list)

    # 页面要的评估口径：这条指令到底有没有人用、用了几次。
    # 列表里只给计数，明细（recent_hits）只在详情里给，避免列表变重。
    hits_7d: int = 0
    last_hit_at: datetime | None = None


class SkillHitItem(APIModel):
    created_at: datetime
    matched_by: str = ""
    keyword: str | None = None
    thread_id: str | None = None
    user_id: str | None = None


class SkillDetail(SkillItem):
    """详情 = 列表项 + 最近几次命中。

    命中记录单独一列而不是塞进 `SkillItem`：列表页不需要它，
    多查一次聚合就是白花钱。
    """

    recent_hits: list[SkillHitItem] = Field(default_factory=list)


class SkillTemplateItem(APIModel):
    key: str
    name: str
    display_name: str
    description: str
    trigger_keywords: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    priority: int = 100
    instruction_chars: int = 0
    # 模板里的工具当前身份是否可见；不可见的会被交集丢弃，
    # 页面提前标出来，免得复制完才发现少了几个。
    missing_tools: list[str] = Field(default_factory=list)
    exists: bool = False
    # 正文全文：点了「预览」才展开看，列表里只给字数。
    instruction: str = ""


class SkillListResponse(APIModel):
    items: list[SkillItem] = Field(default_factory=list)
    total: int = 0
    # 关掉之后技能照常可编辑，只是不再参与规划。
    enabled: bool = True
    can_write: bool = False
    hits_window_days: int = 7


class SkillCreateRequest(APIModel):
    name: str = Field(min_length=2, max_length=64)
    display_name: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=256)
    instruction: str = Field(default="", max_length=20000)
    trigger_keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    allowed_tools: list[str] | None = None
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True


class SkillUpdateRequest(APIModel):
    """全部可选。`allowed_tools` 缺省与显式 null 语义不同，见路由层。"""

    display_name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=256)
    instruction: str | None = Field(default=None, max_length=20000)
    trigger_keywords: list[str] | None = Field(default=None, max_length=MAX_KEYWORDS)
    allowed_tools: list[str] | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool | None = None


class SkillRecallRequest(APIModel):
    """召回测试：拿一句话问「现在会命中哪个技能」。"""

    message: str = Field(min_length=1, max_length=2000)


class SkillRecallResponse(APIModel):
    matched: bool = False
    skill_id: uuid.UUID | None = None
    name: str | None = None
    display_name: str | None = None
    matched_by: str | None = None
    keyword: str | None = None
    tools: list[str] = Field(default_factory=list)
    instruction_chars: int = 0
    note: str = ""


__all__ = [
    "SkillCreateRequest",
    "SkillDetail",
    "SkillHitItem",
    "SkillItem",
    "SkillListResponse",
    "SkillRecallRequest",
    "SkillRecallResponse",
    "SkillTemplateItem",
    "SkillUpdateRequest",
    "MAX_KEYWORD_CHARS",
]
