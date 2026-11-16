# app/schemas/approvals.py
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.models.approval import ApprovalStatus, RiskLevel
from app.schemas.common import APIModel


class ApprovalPreviewResponse(APIModel):
    """展示给用户的待确认操作。"""

    approval_id: UUID
    status: ApprovalStatus

    tool_name: str
    tool_display_name: str
    risk_level: RiskLevel

    reason: str
    preview: dict[str, Any]

    # 委派来的单子：谁把它转交出来的、第几跳。非委派为空 / 0。
    #
    # 必须出现在**预览**上：审批人看到的若是「ops 要改工单」，他无从判断
    # 这是不是一条越权链的末端；「ops 在 cs 的委派下要改工单」才可判断。
    # 派生自 `delegation_chain`，不单独存列（见 `app/models/approval.py`）。
    delegated_from: str | None = None
    delegation_depth: int = 0

    expires_at: datetime
    created_at: datetime

    # 这张卡现在还能不能点。由服务端算，不让浏览器算：
    # 过期与否取决于时钟，客户端差几分钟就会把还能确认的显示成已过期。
    # 前端据此决定渲染按钮还是渲染结论，与服务端确认时的拒绝条件同一个函数
    # （`app/services/approval_service.py::is_actionable`）。
    actionable: bool = False

    # 单子结束后的两个事实：什么时候执行的、为什么没执行成。
    #
    # `error_code` 不能省。卡面上「执行失败」与「被上游否决」长得一模一样，
    # 而后者说明执行方可能完全健康——只有错误码能让运维知道该去查谁。
    executed_at: datetime | None = None
    error_code: str | None = None


class ApprovalBatchResponse(APIModel):
    """按 id 批量取回的审批单。

    刷新会话时用：一条历史消息里只留了审批号，卡上的参数与当前状态都在单子上，
    所以要按号取回来。批量而不是逐条，是因为一条长会话可能攒了几十张卡。
    """

    items: list[ApprovalPreviewResponse] = Field(default_factory=list)


class ConfirmApprovalRequest(APIModel):
    """
    确认时提交的参数镜像。

    服务端与已保存参数做哈希比对；
    不允许“只传 approval_id 就执行”，防止确认内容与展示内容不一致。
    """

    parameters: dict[str, Any] = Field(default_factory=dict)


class ApprovalExecutionResponse(APIModel):
    """审批执行后的用户可见状态。"""

    approval_id: UUID
    status: ApprovalStatus

    message: str

    success: bool | None = None
    verified: bool | None = None

    result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None

    error_code: str | None = None
