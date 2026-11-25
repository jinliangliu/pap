# app/services/snapshot_service.py
"""任务前快照与时间轮转。

快照是任务生命周期的一部分，不是可选备份：**每次任务开始前打一份**，
失败时按它回滚。保留规则见 docs/TRANSACTION.md §4：

    保留 = 最近 N 次 ∪ 每个半小时桶内最早一次（桶上限 48）

两者取并集，总上限 = N + 桶上限 + 1（新打的那份）。同一份"保留清单"既决定
留下哪些，也决定删掉哪些——不写第二套删除规则，避免两套规则漂移后
"该留的被删、该删的留着"。

**reflink 只在 XFS/Btrfs 且同一文件系统内可用**。这里用 Linux FICLONE ioctl
（`ctypes` 直发，避免依赖 fcntl——Windows 开发机没有）；不支持时回落普通复制，
并显式告警，不默默变慢。快照目录与工作区同一父目录，满足"同一文件系统"前提。

审计：删除写 `SNAPSHOT_DELETED`（含 snapshot_id 与桶），创建不逐份写审计
（每次任务都打，逐份会淹没审计表）——但工作区的 `last_snapshot_id` 会更新，
回滚时据此定位最近一份。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditEventType
from app.services.audit_service import write_audit_event

logger = logging.getLogger(__name__)

_SNAPSHOTS_DIR = "snapshots"

# 时间戳嵌进 snapshot_id，排序与分桶都不用读文件 mtime（mtime 会被 reflink 改）。
# 格式：20261125T093046-<8位uuid>，字典序即时间序。
_ID_TIME_FORMAT = "%Y%m%dT%H%M%S"


class SnapshotError(RuntimeError):
    """快照创建或回滚失败。"""


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    snapshot_id: str
    path: Path
    created_at: datetime

    @property
    def bucket(self) -> datetime:
        """所属半小时桶的起始时刻（UTC）。"""
        minutes = settings.snapshot_bucket_minutes
        floor = (self.created_at.minute // minutes) * minutes
        return self.created_at.replace(minute=floor, second=0, microsecond=0)


def _parse_id(snapshot_id: str) -> datetime | None:
    head = snapshot_id.split("-", 1)[0]
    try:
        return datetime.strptime(head, _ID_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _snapshots_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / _SNAPSHOTS_DIR


# ---- reflink 能力探测 -------------------------------------------------------

_FICLONE = 0x40049409
_linux = os.name == "posix"
_reflink_available: bool | None = None


def _try_reflink(src: Path, dst: Path) -> bool:
    """Linux 上用 FICLONE 做写时复制；不支持则返回 False 让调用方普通复制。"""
    if not _linux:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fd_src = os.open(src, os.O_RDONLY)
        try:
            fd_dst = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                result = libc.ioctl(
                    ctypes.c_int(fd_dst),
                    ctypes.c_ulong(_FICLONE),
                    ctypes.c_int(fd_src),
                )
                return result == 0
            finally:
                os.close(fd_dst)
        finally:
            os.close(fd_src)
    except (OSError, AttributeError):
        return False


def reflink_supported() -> bool:
    """启动自检用：探一次能力，不支持就告警。结果进程内缓存。"""
    global _reflink_available
    if _reflink_available is not None:
        return _reflink_available

    _reflink_available = False
    if not _linux:
        logger.warning(
            "当前平台不支持 reflink，快照退化为普通复制；请控制工作区体积。",
            extra={"event": "snapshot_reflink_unavailable", "reason": "platform"},
        )
        return False

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        dst = Path(tmp) / "dst"
        src.write_bytes(b"probe")
        _reflink_available = _try_reflink(src, dst)

    if not _reflink_available:
        logger.warning(
            "文件系统不支持 reflink（需 XFS/Btrfs 且同文件系统），"
            "快照退化为普通复制；请给工作区设硬上限。",
            extra={"event": "snapshot_reflink_unavailable", "reason": "filesystem"},
        )
    return _reflink_available


def _copy_tree(src: Path, dst: Path) -> None:
    """优先 reflink 逐文件复制；不支持时回落 shutil.copy2。"""
    use_reflink = reflink_supported()
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.rglob("*"):
        relative = item.relative_to(src)
        target = dst / relative
        if item.is_symlink():
            # 工作区禁止符号链接（见 workspace_service.directory_size_bytes），
            # 快照里也不复制它，防止指到工作区外。
            raise SnapshotError(f"工作区含符号链接，拒绝快照：{item}")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            if use_reflink and _try_reflink(item, target):
                continue
            shutil.copy2(item, target)


def create_snapshot(workspace_root: str | Path) -> SnapshotInfo:
    """打一份快照。不包含 snapshots/ 自身（避免快照套快照指数增长）。"""
    root = Path(workspace_root)
    if not root.exists():
        raise SnapshotError(f"工作区目录不存在：{root}")

    now = datetime.now(UTC)
    snapshot_id = f"{now.strftime(_ID_TIME_FORMAT)}-{uuid.uuid4().hex[:8]}"
    target = _snapshots_dir(root) / snapshot_id
    target.parent.mkdir(parents=True, exist_ok=True)

    # 只复制工作区顶层内容，跳过 snapshots/。
    staging = target.with_name(f".{snapshot_id}.tmp")
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for item in root.iterdir():
            if item.name == _SNAPSHOTS_DIR:
                continue
            dest = staging / item.name
            if item.is_dir():
                _copy_tree(item, dest)
            elif item.is_file():
                if reflink_supported() and _try_reflink(item, dest):
                    continue
                shutil.copy2(item, dest)
            else:
                raise SnapshotError(f"工作区含非普通文件，拒绝快照：{item}")
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return SnapshotInfo(snapshot_id=snapshot_id, path=target, created_at=now)


def list_snapshots(workspace_root: str | Path) -> list[SnapshotInfo]:
    """列出快照，按创建时间升序（id 字典序即时间序）。"""
    base = _snapshots_dir(workspace_root)
    if not base.exists():
        return []

    result: list[SnapshotInfo] = []
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        created = _parse_id(entry.name)
        if created is None:
            # 无法识别的目录不纳入轮转，也不自动删——宁可留着让人看一眼。
            logger.warning(
                "跳过无法识别的快照目录。",
                extra={"event": "snapshot_unrecognized", "name": entry.name},
            )
            continue
        result.append(SnapshotInfo(snapshot_id=entry.name, path=entry, created_at=created))
    return sorted(result, key=lambda item: (item.created_at, item.snapshot_id))


def _retention_cutoff(now: datetime) -> datetime:
    """半小时桶的时间窗口下界：超出即不再按桶保留。"""
    max_buckets = settings.snapshot_max_buckets
    return now - timedelta(minutes=settings.snapshot_bucket_minutes * max_buckets)


def select_retained(snapshots: list[SnapshotInfo], *, now: datetime | None = None) -> set[str]:
    """按规则算出应保留的 snapshot_id 集合。

    两条规则取并集：
    1. 最近 N 次（按创建时间）——保证"刚打的几份"一定在；
    2. 每个半小时桶内最早的一份，且桶不早于窗口下界——保证长任务在
       每个时间片都有可回滚点，同时把总量压在 24 小时以内。
    """
    now = now or datetime.now(UTC)
    retained: set[str] = set()

    # 规则 1：最近 N 次。
    for item in snapshots[-settings.snapshot_keep_recent :]:
        retained.add(item.snapshot_id)

    # 规则 2：每桶最早一次，桶上限由 cutoff 控制。
    cutoff = _retention_cutoff(now)
    bucket_first: dict[datetime, SnapshotInfo] = {}
    for item in snapshots:
        if item.created_at < cutoff:
            continue
        bucket = item.bucket
        if bucket not in bucket_first:
            bucket_first[bucket] = item
    retained.update(item.snapshot_id for item in bucket_first.values())

    return retained


async def prune_snapshots(
    session: AsyncSession,
    *,
    workspace_root: str | Path,
    tenant_id: str,
    workspace_id: uuid.UUID,
    actor_id: str,
    now: datetime | None = None,
) -> list[str]:
    """应用保留规则，删除多余快照并写审计，返回被删的 snapshot_id。"""
    all_snapshots = list_snapshots(workspace_root)
    retained = select_retained(all_snapshots, now=now)
    removed: list[str] = []

    for item in all_snapshots:
        if item.snapshot_id in retained:
            continue
        shutil.rmtree(item.path, ignore_errors=True)
        removed.append(item.snapshot_id)

        await write_audit_event(
            session,
            event_type=AuditEventType.SNAPSHOT_DELETED,
            tenant_id=tenant_id,
            actor_id=actor_id,
            resource_type="snapshot",
            resource_id=item.snapshot_id,
            message="快照轮转清理。",
            metadata={
                "workspace_id": str(workspace_id),
                "bucket": item.bucket.isoformat(),
                "created_at": item.created_at.isoformat(),
            },
        )

    if removed:
        await session.flush()
        logger.info(
            "快照轮转完成。",
            extra={
                "event": "snapshot_pruned",
                "workspace_id": str(workspace_id),
                "removed": len(removed),
                "retained": len(retained),
            },
        )
    return removed
