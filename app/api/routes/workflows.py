# app/api/routes/workflows.py
"""编排的管理与运行记录接口。

读开放给所有成员：不知道有哪些流程，就没法在对话里用 `/workflow`；
写需要 `agent:write`——定义「谁在第几步做什么」是治理动作，不是日常操作。

**这里没有「立即运行」的接口。** 触发只有对话里的 `/workflow <name> <输入>`：
编排的输出必须落在一个会话里，用户才能看见、审计才能查到、写操作才能落到
同一条审批链上。开一个 `POST /run` 会让这三件事同时断掉，而它多出来的能力
只是「可以被脚本调用」——那是 P3 的定时触发要解决的问题，不是这里。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.models.workflow import Workflow, WorkflowRun
from app.schemas.workflow import (
    WorkflowDetail,
    WorkflowListResponse,
    WorkflowNodeRun,
    WorkflowRunItem,
    WorkflowRunListResponse,
    WorkflowSaveRequest,
    WorkflowSummary,
    WorkflowToggleRequest,
)
from app.services import workflow_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])
runs_router = APIRouter(prefix="/workflow-runs", tags=["workflows"])

WRITE_PERMISSION = "agent:write"


def _can_manage(identity: Identity) -> bool:
    return identity.is_platform_admin or WRITE_PERMISSION in identity.scopes


def _require_manage(identity: Identity) -> None:
    if not _can_manage(identity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="缺少 agent:write 权限，无法管理编排。",
        )


def _run_item(row: WorkflowRun) -> WorkflowRunItem:
    nodes = [
        WorkflowNodeRun(**item)
        for item in (row.nodes_json or [])
        if isinstance(item, dict) and item.get("node_id")
    ]

    return WorkflowRunItem(
        id=str(row.id),
        workflow_name=str(row.workflow_name),
        revision=int(row.revision or 0),
        status=str(row.status.value if hasattr(row.status, "value") else row.status),
        cursor=row.cursor,
        nodes_used=int(row.nodes_used or 0),
        output_text=row.output_text,
        error_code=row.error_code,
        error_message=row.error_message,
        approval_id=row.approval_id,
        started_by=row.started_by,
        started_at=row.started_at,
        finished_at=row.finished_at,
        nodes=nodes,
    )


def _summary(row: Workflow, last_run: WorkflowRun | None) -> WorkflowSummary:
    plan = workflow_service.plan_of(row)

    return WorkflowSummary(
        name=str(row.name),
        display_name=str(row.display_name),
        description=row.description,
        revision=int(row.revision or 0),
        enabled=bool(row.enabled),
        node_count=plan.size if plan else 0,
        depth_hops=plan.depth_hops if plan else 0,
        max_nodes=plan.max_nodes if plan else 0,
        updated_at=row.updated_at,
        last_run=_run_item(last_run) if last_run is not None else None,
    )


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    identity: Identity = Depends(get_current_identity),
) -> WorkflowListResponse:
    rows = await workflow_service.list_workflows(identity.tenant_id)
    runs = await workflow_service.list_runs(tenant_id=identity.tenant_id, limit=200)

    # 一次取回最近 200 条、在内存里按名字取第一条，而不是每个流程查一次：
    # 流程数量是个位数，N+1 查询的代价却会随着页面上多一张卡线性增长。
    latest: dict[str, WorkflowRun] = {}
    for run in runs:
        latest.setdefault(str(run.workflow_name), run)

    items = [_summary(row, latest.get(str(row.name))) for row in rows]
    return WorkflowListResponse(
        items=items, total=len(items), can_manage=_can_manage(identity)
    )


@router.get("/{name}", response_model=WorkflowDetail)
async def get_workflow(
    name: str,
    identity: Identity = Depends(get_current_identity),
) -> WorkflowDetail:
    row = await workflow_service.get_workflow(identity.tenant_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有名为「{name}」的流程。")

    runs = await workflow_service.recent_runs(
        tenant_id=identity.tenant_id, workflow_name=str(row.name), limit=1
    )
    summary = _summary(row, runs[0] if runs else None)
    return WorkflowDetail(**summary.model_dump(), definition=row.definition or {})


@router.post("", response_model=WorkflowDetail)
async def save_workflow(
    payload: WorkflowSaveRequest,
    identity: Identity = Depends(get_current_identity),
) -> WorkflowDetail:
    """新建或更新（同名覆盖，revision +1）。

    保存即编译：图不成立时这里就拒绝，并指出是哪个节点。
    存进库的一定是能跑的图——否则「保存成功」之后才在对话里报错，
    配图的人根本不知道自己错在哪。
    """
    _require_manage(identity)

    try:
        row = await workflow_service.save_workflow(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            definition=payload.definition,
            enabled=payload.enabled,
        )
    except workflow_service.WorkflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    summary = _summary(row, None)
    return WorkflowDetail(**summary.model_dump(), definition=row.definition or {})


@router.patch("/{name}", response_model=WorkflowSummary)
async def toggle_workflow(
    name: str,
    payload: WorkflowToggleRequest,
    identity: Identity = Depends(get_current_identity),
) -> WorkflowSummary:
    """停用 / 启用。不改图，也不动 revision。

    停用是「保留配置但不再能被触发」——与删除的区别是历史 run 仍然对得上
    一个存在的名字。
    """
    _require_manage(identity)

    row = await workflow_service.get_workflow(identity.tenant_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有名为「{name}」的流程。")

    updated = await workflow_service.save_workflow(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        name=str(row.name),
        display_name=str(row.display_name),
        description=row.description,
        definition=row.definition,
        enabled=payload.enabled,
    )
    return _summary(updated, None)


@router.delete("/{name}", status_code=204)
async def delete_workflow(
    name: str,
    identity: Identity = Depends(get_current_identity),
) -> None:
    _require_manage(identity)

    deleted = await workflow_service.delete_workflow(
        tenant_id=identity.tenant_id, name=name
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"没有名为「{name}」的流程。")


@router.get("/{name}/runs", response_model=WorkflowRunListResponse)
async def list_workflow_runs(
    name: str,
    limit: int = Query(default=20, ge=1, le=100),
    identity: Identity = Depends(get_current_identity),
) -> WorkflowRunListResponse:
    runs = await workflow_service.recent_runs(
        tenant_id=identity.tenant_id, workflow_name=name, limit=limit
    )
    items = [_run_item(row) for row in runs]
    return WorkflowRunListResponse(items=items, total=len(items))


@runs_router.get("/{run_id}", response_model=WorkflowRunItem)
async def get_workflow_run(
    run_id: str,
    identity: Identity = Depends(get_current_identity),
) -> WorkflowRunItem:
    row = await workflow_service.get_run(identity.tenant_id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这次运行记录。")
    return _run_item(row)
