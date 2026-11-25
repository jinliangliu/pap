# app/services/workspace_service.py
"""按人持久工作区（单机版）。

一条硬约束：**路径只能由服务端生成**。客户端传来的任何路径片段都不得
拼进 `root_path`——否则一次传入 `../../` 就能让沙箱读写进程可写的任何位置。
`user_id` 先做字符集校验再进路径，与对象存储同一套纪律。

工作区不是对象存储：它会被反复读写、装着 git 仓库与依赖缓存，所以单独一个
根目录。软删除不物理删目录——快照与回滚还指着它，真正回收是独立的一次动作。

RLS 已经按 tenant 过滤，这里额外校验 `user_id`：同一租户内的两个人不能靠
猜 workspace_id 互相读写。应用层这层校验不能省，数据库看不到 user 归属。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditEventType
from app.models.task import Workspace, WorkspaceStatus
from app.services.audit_service import write_audit_event

logger = logging.getLogger(__name__)

# 用户标识进路径前必须过这一关：与 tenancy.context 的租户字符集一致，
# 但允许更长的用户主体（OIDC subject 可能带 @ 与 |）。
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@|-]{0,127}$")


class WorkspaceError(RuntimeError):
    """工作区不可创建或不可访问。"""


class WorkspaceNotFound(WorkspaceError):
    """工作区不存在或不属于当前用户。"""


class WorkspaceDeleted(WorkspaceError):
    """工作区已软删除，拒绝继续写入。"""


def validate_user_id(user_id: str) -> str:
    candidate = (user_id or "").strip()
    if not _USER_ID_PATTERN.match(candidate):
        raise WorkspaceError("用户标识格式无效，不能用作工作区路径。")
    return candidate


def _root_base() -> Path:
    return Path(settings.workspace_storage_dir).resolve()


def build_root_path(tenant_id: str, user_id: str, workspace_id: uuid.UUID) -> Path:
    """服务端构造绝对路径，并确保它仍在配置的根目录内。

    最终再 `resolve()` 一次并校验前缀：即便 tenant/user 的字符集校验都被绕过，
    路径穿越也过不了这一关。三段都来自服务端，正常不会触发，但这条断言是
    「工作区能读写任意文件」的最后一道闸。
    """
    base = _root_base()
    target = (base / tenant_id / validate_user_id(user_id) / str(workspace_id)).resolve()

    if target != base and base not in target.parents:
        raise WorkspaceError("工作区路径越界，拒绝创建。")

    return target


def effective_quota_bytes(workspace: Workspace) -> int:
    """工作区自己的配额；0 表示沿用平台默认。"""
    return workspace.quota_bytes or settings.workspace_default_max_bytes


def directory_size_bytes(path: Path) -> int:
    """目录占用（含快照）。跟随符号链接可能重复计数，工作区内禁止符号链接。"""
    if not path.exists():
        return 0

    total = 0
    for entry in path.rglob("*"):
        if entry.is_symlink():
            # 符号链接可能指到工作区外，计入既重复又危险。直接报错让上层看见，
            # 而不是静默少算——配额一旦算错，第一个超限的人是最后一个知道的。
            raise WorkspaceError(f"工作区内存在符号链接，拒绝核算：{entry}")
        if entry.is_file():
            total += entry.stat().st_size
    return total


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"无法创建工作区目录：{exc}") from exc


async def get_or_create(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    actor_id: str | None = None,
) -> tuple[Workspace, bool]:
    """取该用户的工作区；没有则创建（目录 + 库行 + 审计同一事务）。

    返回 `(工作区, 是否新建)`，与 `task_service.create_task` 同构。
    唯一约束 `(tenant_id, user_id)` 兜底并发创建。
    """
    validate_user_id(user_id)

    row = await session.scalar(
        select(Workspace).where(Workspace.user_id == user_id)
    )
    if row is not None:
        if row.status == WorkspaceStatus.DELETED:
            raise WorkspaceDeleted("工作区已删除，请先恢复或新建。")
        return row, False

    workspace_id = uuid.uuid4()
    root_path = build_root_path(tenant_id, user_id, workspace_id)
    _ensure_dir(root_path)

    row = Workspace(
        id=workspace_id,
        tenant_id=tenant_id,
        user_id=user_id,
        root_path=str(root_path),
        status=WorkspaceStatus.ACTIVE,
    )
    session.add(row)

    try:
        await session.flush()
    except Exception:
        # 目录已建但库行没落（多半是并发撞唯一键）：孤儿目录留给清理任务，
        # 不在这里回滚文件系统——文件系统没有事务，硬删可能删掉别人刚建的目录。
        logger.warning(
            "工作区落库失败，目录保留待清理。",
            extra={"event": "workspace_orphan", "root_path": str(root_path)},
            exc_info=True,
        )
        # 让调用方的事务整体回滚；任务入队有幂等键，重试会拿到已建的那行。
        raise

    await write_audit_event(
        session,
        event_type=AuditEventType.WORKSPACE_CREATED,
        tenant_id=tenant_id,
        actor_id=actor_id or user_id,
        resource_type="workspace",
        resource_id=str(row.id),
        message="创建按人工作区。",
        metadata={"user_id": user_id, "root_path": str(row.root_path)},
    )
    await session.flush()
    return row, True


async def get_owned(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: str,
) -> Workspace:
    """按 id 取工作区，并校验属于当前用户。查不到一律同一出口。"""
    row = await session.get(Workspace, workspace_id)
    if row is None or row.user_id != user_id or row.status == WorkspaceStatus.DELETED:
        raise WorkspaceNotFound("工作区不存在或不属于当前用户。")
    return row


async def set_status(
    session: AsyncSession,
    *,
    workspace: Workspace,
    status: WorkspaceStatus,
    actor_id: str,
) -> Workspace:
    """归档/恢复/删除。删除是软删除，目录仍留着供回滚。"""
    if status == workspace.status:
        return workspace

    previous = workspace.status
    workspace.status = status

    event = {
        WorkspaceStatus.ARCHIVED: AuditEventType.WORKSPACE_ARCHIVED,
        WorkspaceStatus.DELETED: AuditEventType.WORKSPACE_DELETED,
    }.get(status)

    if event is not None:
        await write_audit_event(
            session,
            event_type=event,
            tenant_id=workspace.tenant_id,
            actor_id=actor_id,
            resource_type="workspace",
            resource_id=str(workspace.id),
            message=f"工作区状态：{previous.value} -> {status.value}。",
            metadata={"user_id": workspace.user_id, "from": previous.value, "to": status.value},
        )
    await session.flush()
    return workspace


def usage_bytes(workspace: Workspace) -> int:
    """工作区当前占用（含快照），用于配额核算。纯文件操作，不碰库。"""
    return directory_size_bytes(Path(workspace.root_path))
