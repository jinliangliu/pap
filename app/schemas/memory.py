# app/schemas/memory.py
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.models.memory import MemoryReviewAction, MemoryReviewStatus, MemoryType
from app.schemas.common import APIModel

# 单条内容的长度上限。写太长的「记忆」说明它不是记忆，是文档——
# 那种东西应该进知识库，进这里只会每轮烧 prompt。
MAX_TITLE = 120
MAX_CONTENT = 2000


class MemoryItem(APIModel):
    id: uuid.UUID
    memory_type: MemoryType
    title: str
    content: str
    source: str
    confidence: float
    is_user_confirmed: bool
    # self = 个人记忆；tenant = 团队共享（user_id 为空）。
    scope: str = "self"
    source_thread_id: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None


class MemoryListResponse(APIModel):
    items: list[MemoryItem] = Field(default_factory=list)
    total: int = 0
    # 关掉之后接口仍然可读（历史还在），但不再注入对话。
    enabled: bool = True
    can_manage_tenant: bool = False


class MemoryCreateRequest(APIModel):
    memory_type: MemoryType = MemoryType.FACT
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    scope: str = "self"

    @field_validator("scope")
    @classmethod
    def _scope_allowed(cls, value: str) -> str:
        if value not in ("self", "tenant"):
            raise ValueError("scope 只能是 self 或 tenant")
        return value


class MemoryUpdateRequest(APIModel):
    memory_type: MemoryType | None = None
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    content: str | None = Field(default=None, max_length=MAX_CONTENT)


class MemoryReviewItem(APIModel):
    id: uuid.UUID
    memory_id: uuid.UUID
    # 记忆当前的内容，方便页面直接对比「原样 vs 建议」。
    # 记忆被删时为空，页面显示「原记忆已删除」而不是报错。
    memory_title: str = ""
    memory_content: str = ""
    action: MemoryReviewAction
    status: MemoryReviewStatus
    reason: str
    proposed_title: str | None = None
    proposed_content: str | None = None
    merge_target_id: uuid.UUID | None = None
    evidence: list[dict] = Field(default_factory=list)
    confidence: float
    model: str = ""
    prompt_version: str = ""
    created_at: datetime
    resolved_at: datetime | None = None
    # 管理员能只读查看全租户建议，但只有本人能应用/忽略。
    can_apply: bool = True


class MemoryReviewListResponse(APIModel):
    items: list[MemoryReviewItem] = Field(default_factory=list)
    total: int = 0


class MemoryDreamRequest(APIModel):
    """手动触发一次梦境的参数。

    `days` 留空表示「用租户配置的默认回溯窗口」——界面上的配置项要真正生效，
    就不能让前端每次都硬编码一个 30 覆盖它。
    `limit` 是**这一次**跑几条，会被租户配置的 `max_per_run` 夹紧：
    配置管的是上限，而「这次点一下跑多少」由调用方决定。
    """

    days: int | None = Field(default=None, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=200)
    dry_run: bool = False


class MemoryDreamConfig(APIModel):
    """租户梦境配置。`enabled`/`days`/`max_per_run` 是合并后的生效值。"""

    enabled: bool = True
    days: int = 30
    max_per_run: int = 20

    # 平台层开关。关闭时租户无法自行打开，界面要说明原因而不是让开关看着是开的。
    platform_enabled: bool = True

    # 租户原始设置，用于界面上区分「跟随平台默认」与「已自定义」。
    # tenant_enabled 为 None = 跟随平台；tenant_days / tenant_max_per_run 为 0 = 跟随平台默认。
    tenant_enabled: bool | None = None
    tenant_days: int = 0
    tenant_max_per_run: int = 0

    disabled_reason: str | None = None

    # 范围随接口下发，前端不重复硬编码数字。
    days_min: int = 1
    days_max: int = 365
    max_per_run_min: int = 1
    max_per_run_max: int = 200

    # 能否修改由调用者权限决定，前端据此禁用表单。
    can_edit: bool = False


class MemoryDreamConfigUpdate(APIModel):
    """写租户配置。`None` / `0` 都表示「跟随平台默认」，不会被展开成具体值。"""

    enabled: bool | None = None
    days: int = Field(default=0, ge=0, le=365)
    max_per_run: int = Field(default=0, ge=0, le=200)


class MemoryDreamItem(APIModel):
    memory_id: str
    title: str
    action: MemoryReviewAction
    reason: str
    confidence: float
    proposed_title: str | None = None
    proposed_content: str | None = None
    merge_target_id: str | None = None
    evidence_count: int = 0


class MemoryDreamResponse(APIModel):
    scanned: int = 0
    suggested: int = 0
    kept: int = 0
    skipped_pending: int = 0
    failed: int = 0
    elapsed_ms: int = 0
    items: list[MemoryDreamItem] = Field(default_factory=list)


class MemoryExtractionStats(APIModel):
    days: int
    auto_total: int
    auto_confirmed: int
    auto_pending: int
    auto_superseded: int
    reviews_applied: int
    confirmation_rate: float
    # 口径写进响应，避免有人拿这个数当「精确通过率」。
    note: str = "分母只含仍存在的自动候选；被物理删除的候选查不到，因此该比例偏高。"
