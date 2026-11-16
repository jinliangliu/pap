# app/schemas/workflow.py
"""编排接口的请求/响应。

`definition` 只在详情与写入时出现：列表页需要的是「有几个节点、最长几跳、
最近一次跑成什么样」，把整张图塞进列表会让一次请求变成几十 KB 的无用载荷。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkflowNodeRun(BaseModel):
    node_id: str
    kind: str
    status: str
    agent: str = ""
    preview: str = ""
    truncated: bool = False
    depth: int = 0
    error_code: str = ""
    message: str = ""
    missing: list[str] = Field(default_factory=list)


class WorkflowRunItem(BaseModel):
    id: str
    workflow_name: str
    revision: int
    status: str
    cursor: str | None = None
    nodes_used: int
    output_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    approval_id: str | None = None
    started_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    nodes: list[WorkflowNodeRun] = Field(default_factory=list)


class WorkflowRunListResponse(BaseModel):
    items: list[WorkflowRunItem]
    total: int


class WorkflowSummary(BaseModel):
    name: str
    display_name: str
    description: str | None = None
    revision: int
    enabled: bool
    # 图的两个规模指标。`depth_hops` 是「最长路径上有几步会真的调智能体」，
    # **不是**运行时深度——编排每跳都以发起方为父，深度恒为 1（`ORCHESTRATION.md` §12.9）。
    # 字段名沿用是为了不改接口；界面上说的是「其中 N 步调用智能体」。
    node_count: int
    depth_hops: int
    max_nodes: int
    updated_at: datetime | None = None
    last_run: WorkflowRunItem | None = None


class WorkflowDetail(WorkflowSummary):
    definition: dict[str, Any]


class WorkflowListResponse(BaseModel):
    items: list[WorkflowSummary]
    total: int
    # 能不能改图只影响界面上出现哪些按钮。真正的判定在写接口上：
    # 前端藏按钮挡不住直接调接口，反过来前端留着按钮也只是让人点出一堆 403。
    can_manage: bool = False


class WorkflowSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any]
    enabled: bool = True


class WorkflowToggleRequest(BaseModel):
    enabled: bool


# 触发走对话（`/workflow <name> <输入>`），所以这里没有「立即运行」的请求体。
# 单独开一个 POST /run 会绕开「结论要回到一个会话」这条——编排的价值之一
# 正是它的输出能被用户看见、被审计查到。


class WorkflowRunStatusLiteral(BaseModel):
    """给文档用的状态枚举（不参与序列化）。"""

    status: Literal["RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]
