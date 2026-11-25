# app/services/task_runner_service.py
"""事务执行器：把 queued 任务送进断网沙箱，产出落 artifacts。

P1 已通三类（见 docs/TRANSACTION.md §5）：

- DOCUMENT：Markdown 走 stdin → 固定代码用 python-docx 排版 → docx 落 artifacts；
- CODE：代码基线/改动走 stdin → 固定代码在沙箱 tmpfs 写文件、生成 unified diff、
  可选跑白名单测试 → diff 落 artifacts，测试结论进 output.test；
- SCHEDULE：`input_json.compute` 声明上面两种计算，产物落库后再按
  `input_json.delivery` 经平台侧工具投递（授权范围校验 + 当前 RBAC 权限）。

三类都坚持「内容走数据通道、代码固定」，且**沙箱不挂宿主工作区**——
编程事务用的是一次性 tmpfs（容器销毁即丢），工作区只在平台侧留档与快照。

四条纪律（沿用 P0 与写工具回读的口径）：

1. **用户内容不进代码**。Markdown 以 JSON 走 stdin，代码是平台固定模板——
   否则一个标题里的换行就能改写执行逻辑。
2. **截断即失败**。stdout 被截断时绝不落半份 docx：能下载但打不开的产物
   比明确失败更难排查。产物上限单独配置，不复用对话的 8000 字符。
3. **终态以回读为准**。执行返回值不算证据；`get_execution()` 独立回读一致
   才推 SUCCEEDED，否则 FAILED + error_code。
4. **执行记录只有沙箱写**。`task_executions` 的 status / cpu_ms / task_id /
   snapshot_id 由沙箱直写 PG；平台只写 `tasks.output_json` 的产物引用。
   同一行不两边写，避免「同一事实存两处后对不上」。

沙箱异常（不可达 / 超时）落 UNKNOWN，不自动重发：代码可能已经跑了，
重发会产生第二份 docx 而第一份无人认领。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import uuid
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory
from app.integrations.sandbox_client import (
    SandboxClient,
    SandboxError,
    SandboxRejected,
    SandboxResult,
    SandboxTimeout,
    SandboxUnavailable,
)
from app.models.artifact import Artifact
from app.models.audit import AuditEventType
from app.models.task import Task, TaskKind, TaskStatus, Workspace
from app.services import snapshot_service
from app.services.audit_service import write_audit_event
from app.services.delivery_service import deliver_task_output
from app.services.task_service import record_execution_cpu_ms, transition
from app.storage.object_store import get_object_store, safe_filename, safe_suffix

logger = logging.getLogger(__name__)

_ARTIFACT_MARK = "PAP_ARTIFACT:"
_TEST_MARK = "PAP_TESTRESULT:"
_DOCX_NAME = "document.docx"
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PATCH_NAME = "changes.patch"
_PATCH_TYPE = "text/x-diff; charset=utf-8"

# 平台生成的代码：不读环境、不联网、不碰工作区外路径。
# python-docx 由镜像预装；-I 隔离模式下 site-packages 仍在 sys.path。
_DOCX_CODE = '''\
import base64
import json
import sys
from docx import Document

spec = json.loads(sys.stdin.read())
doc = Document()
doc.add_heading(spec["title"], level=0)

for line in spec["markdown"].splitlines():
    text = line.strip()
    if not text:
        continue
    if text.startswith("## "):
        doc.add_heading(text[3:], level=2)
    elif text.startswith("# "):
        doc.add_heading(text[2:], level=1)
    elif text.startswith("- "):
        doc.add_paragraph(text[2:], style="List Bullet")
    else:
        doc.add_paragraph(text)

out = "/work/document.docx"
doc.save(out)
with open(out, "rb") as handle:
    print("PAP_ARTIFACT:" + base64.b64encode(handle.read()).decode("ascii"))
'''

# 编程事务的模板代码。纪律与文档一致：文件内容走 stdin，代码固定。
# 沙箱内的工作目录 /work 是 tmpfs，容器销毁即丢——**这里从不挂载宿主工作区**。
# 流程：写基线 -> 应用改动 -> 生成 unified diff -> 可选跑测试 -> 两个标记输出。
# 测试命令由平台白名单校验后以 argv 列表给出，绝不让模板拼 shell 字符串。
_CODE_VERIFY_CODE = '''\
import base64
import difflib
import json
import pathlib
import subprocess
import sys

spec = json.loads(sys.stdin.read())
root = pathlib.Path("/work/code")


def write_tree(files, base):
    for name, content in files.items():
        target = (base / name).resolve()
        # 只接受 base 目录内的相对路径；斜杠是路径分隔符，不许 .. 或绝对路径。
        if base not in target.parents or not name or name.startswith("/"):
            raise ValueError("非法文件路径：" + str(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


before = spec.get("base") or {}
after = spec.get("files") or {}
write_tree(before, root / "base")
write_tree(after, root / "work")

# 逐文件生成 unified diff，只在内容不同时输出；改名视作删除+新增。
diff_lines = []
for name in sorted(set(before) | set(after)):
    old = before.get(name)
    new = after.get(name)
    if old == new:
        continue
    diff_lines.extend(
        difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile="a/" + name,
            tofile="b/" + name,
        )
    )
patch = "".join(diff_lines).encode("utf-8")

test_result = {"ran": False, "passed": None, "exit_code": None, "output": ""}
command = spec.get("test_command") or []
if command:
    try:
        proc = subprocess.run(
            command,
            cwd=str(root / "work"),
            capture_output=True,
            text=True,
            timeout=spec.get("test_timeout_seconds", 30),
        )
        test_result = {
            "ran": True,
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": (proc.stdout + proc.stderr)[:8000],
        }
    except subprocess.TimeoutExpired:
        test_result = {
            "ran": True,
            "passed": False,
            "exit_code": None,
            "output": "测试命令超时。",
        }

print("PAP_ARTIFACT:" + base64.b64encode(patch).decode("ascii"))
print("PAP_TESTRESULT:" + json.dumps(test_result, ensure_ascii=False))
'''


class TaskRunnerError(RuntimeError):
    """执行器无法给出可确认结论。"""


@lru_cache(maxsize=1)
def _task_client() -> SandboxClient:
    """任务产物比对话输出大，单独配上限；不复用全局客户端的 8000 字符。"""
    return SandboxClient(max_output_chars=settings.task_artifact_max_chars)


async def close_task_client() -> None:
    if _task_client.cache_info().currsize:
        await _task_client().aclose()
        _task_client.cache_clear()


def build_document_input(*, title: str, markdown: str) -> dict[str, Any]:
    """构造送进 stdin 的规格。代码固定，内容只走数据通道。"""
    return {
        "title": title[:255],
        "markdown": markdown[: settings.task_document_max_chars],
    }


# 测试命令白名单：只认 python3 启动的模块测试，argv 列表传入，不经 shell。
# 沙箱边界（断网/只读根/非 root/cap-drop）是主防线，这层是「不把任意 shell
# 字符串拼进 subprocess」的次防线——挡住 `; rm -rf` 这类参数注入。
_TEST_COMMAND_ALLOWLIST = frozenset({"pytest", "unittest"})
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:=@+-]{1,128}$")
_REL_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")


class CodeInputError(ValueError):
    """编程事务输入不合法。"""


def validate_code_files(files: Any) -> dict[str, str]:
    """校验代码文件映射：相对路径、字符上限、文件数上限，逐条拒绝不猜。"""
    if not isinstance(files, dict) or not files:
        raise CodeInputError("编程任务需要非空的文件映射。")
    if len(files) > settings.task_code_files_max:
        raise CodeInputError(f"文件数超过上限 {settings.task_code_files_max}。")

    clean: dict[str, str] = {}
    for name, content in files.items():
        if not isinstance(name, str) or not _REL_PATH.match(name):
            raise CodeInputError(f"文件路径不合法：{name!r}")
        parts = PurePosixPath(name).parts
        if ".." in parts or "." in parts or name.startswith("/"):
            raise CodeInputError(f"文件路径越界：{name!r}")
        if not isinstance(content, str):
            raise CodeInputError(f"文件内容必须是文本：{name!r}")
        if len(content) > settings.task_code_file_max_chars:
            raise CodeInputError(f"文件 {name!r} 超过字符上限。")
        clean[name] = content
    return clean


def validate_test_command(value: Any) -> list[str]:
    """只放行 python3 -m <白名单模块> [安全参数]；空表示只出 diff 不跑测试。"""
    if value in (None, "", []):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CodeInputError("测试命令必须是 argv 字符串列表。")

    argv = list(value)
    if len(argv) < 3 or argv[0] not in {"python3", "python"} or argv[1] != "-m":
        raise CodeInputError("测试命令必须以 python3 -m 启动。")
    if argv[2] not in _TEST_COMMAND_ALLOWLIST:
        raise CodeInputError(f"测试模块不在白名单：{argv[2]!r}")
    if len(argv) > 12 or not all(_SAFE_ARG.match(item) for item in argv):
        raise CodeInputError("测试命令参数含非法字符或过长。")
    return argv


def build_code_input(
    *,
    files: dict[str, str],
    base: dict[str, str] | None = None,
    test_command: list[str] | None = None,
) -> dict[str, Any]:
    """构造 CODE 送进 stdin 的规格。输入校验全在平台侧，沙箱只执行模板。"""
    clean_files = validate_code_files(files)
    clean_base = validate_code_files(base) if base else {}
    return {
        "files": clean_files,
        "base": clean_base,
        "test_command": validate_test_command(test_command),
        "test_timeout_seconds": settings.task_code_test_timeout_seconds,
    }


def extract_code_patch(stdout: str) -> bytes:
    """取 unified diff。空 diff 合法（无改动），但不许缺标记或出现多份。"""
    marks = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith(_ARTIFACT_MARK)
    ]
    if len(marks) != 1:
        raise TaskRunnerError(f"期望恰好一份 diff，实际收到 {len(marks)} 份。")
    encoded = marks[0][len(_ARTIFACT_MARK) :].strip()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TaskRunnerError("diff 不是合法的 base64。") from exc
    if len(data) > settings.artifact_max_bytes:
        raise TaskRunnerError("diff 超过大小上限。")
    # 不查 PK 头：这是文本 diff，不是 ZIP；空 diff 表示无改动，允许。
    return data


def extract_test_result(stdout: str) -> dict[str, Any]:
    """取测试结论。缺标记视为「未跑测试」，不当作失败或伪造一个结果。"""
    marks = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith(_TEST_MARK)
    ]
    if not marks:
        return {"ran": False, "passed": None, "exit_code": None, "output": ""}
    if len(marks) != 1:
        raise TaskRunnerError("测试结论出现多份，无法判定。")
    try:
        result = json.loads(marks[0][len(_TEST_MARK) :].strip())
    except ValueError as exc:
        raise TaskRunnerError("测试结论不是合法 JSON。") from exc
    if not isinstance(result, dict):
        raise TaskRunnerError("测试结论结构错误。")
    return result


def extract_artifact(stdout: str) -> bytes:
    """从 stdout 取唯一一份产物。多份或没有都拒绝，不「取第一份」猜。"""
    marks = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith(_ARTIFACT_MARK)
    ]
    if len(marks) != 1:
        raise TaskRunnerError(f"期望恰好一份产物，实际收到 {len(marks)} 份。")

    encoded = marks[0][len(_ARTIFACT_MARK) :].strip()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TaskRunnerError("产物不是合法的 base64。") from exc

    if not data:
        raise TaskRunnerError("产物为空。")
    if len(data) > settings.artifact_max_bytes:
        raise TaskRunnerError(
            f"产物 {len(data)} 字节超过上限 {settings.artifact_max_bytes}。"
        )
    # docx 是 ZIP：头两字节必须是 PK，挡住「标成 docx 的文本」。
    if data[:2] != b"PK":
        raise TaskRunnerError("产物不是 ZIP 容器，拒绝按 docx 落库。")

    # 一个空 docx 也有 KB 量级；几十字节只可能是截断或伪造的 PK 头。
    # stdout 截断 defense 的第二道（第一道是 run_task 里的 result.truncated）。
    if len(data) < settings.task_artifact_min_bytes:
        raise TaskRunnerError(
            f"产物仅 {len(data)} 字节，疑似截断，拒绝落库。"
        )

    return data


def readback_matches(result: SandboxResult, reread: SandboxResult) -> bool:
    """独立回读必须同 execution_id、同终态、同计量。"""
    if not reread.execution_id or reread.execution_id != result.execution_id:
        return False
    return (
        reread.status == result.status
        and reread.cpu_ms == result.cpu_ms
        and reread.exit_code == result.exit_code
    )


async def _write_draft(session: AsyncSession, *, task: Task, markdown: str) -> Path:
    """草稿落工作区：任务输入可事后复现，正文不进 tasks 行。"""
    workspace = await session.get(Workspace, task.workspace_id)
    if workspace is None:
        raise TaskRunnerError("任务的工作区不存在。")

    root = Path(workspace.root_path)
    drafts = root / "drafts"
    path = drafts / f"{task.id.hex}.md"

    def _write() -> None:
        drafts.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")

    await asyncio.to_thread(_write)
    return path


async def _store_artifact(
    session: AsyncSession,
    *,
    task: Task,
    data: bytes,
    display_name: str = _DOCX_NAME,
    content_type: str = _DOCX_TYPE,
) -> Artifact:
    """对象先落盘、元数据后落库；库失败则删对象，不留不可达文件。"""
    artifact_id = uuid.uuid4()
    display = safe_filename(display_name)
    key = get_object_store().build_key(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        artifact_id=artifact_id,
        suffix=safe_suffix(display),
    )
    size, digest = await get_object_store().put(key, data)
    artifact = Artifact(
        id=artifact_id,
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        object_key=key,
        filename=display,
        content_type=content_type,
        size_bytes=size,
        sha256=digest,
    )

    try:
        session.add(artifact)
        await session.flush()
        await write_audit_event(
            session,
            event_type=AuditEventType.ARTIFACT_CREATED,
            tenant_id=task.tenant_id,
            actor_id=task.user_id,
            resource_type="artifact",
            resource_id=str(artifact.id),
            message="任务产物入库。",
            metadata={"task_id": str(task.id), "kind": task.kind.value},
        )
    except Exception:
        await get_object_store().delete(key)
        raise

    return artifact


async def _finish(
    *,
    task_id: uuid.UUID,
    target: TaskStatus,
    note: str,
    error_code: str | None = None,
    cpu_ms: int | None = None,
    output: dict[str, Any] | None = None,
) -> None:
    """终态单独事务：前面任何一步失败都不会让任务卡在 running。"""
    async with async_session_factory() as session:
        async with session.begin():
            task = await session.get(Task, task_id)
            if task is None or task.status in (
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.UNKNOWN,
            ):
                return
            await transition(session, task, target, actor_id="runner", note=note)
            if cpu_ms is not None or target == TaskStatus.SUCCEEDED:
                await record_execution_cpu_ms(session, task=task, cpu_ms=cpu_ms)
            if output:
                task.output_json = output
            # CANCELLED / UNKNOWN 已由 transition() 各自记过审计，这里不重复；
            # 只有 SUCCEEDED / FAILED 是 transition 不记的那两条。
            if target in (TaskStatus.SUCCEEDED, TaskStatus.FAILED):
                await write_audit_event(
                    session,
                    event_type=AuditEventType.TASK_SUCCEEDED
                    if target == TaskStatus.SUCCEEDED
                    else AuditEventType.TASK_FAILED,
                    tenant_id=task.tenant_id,
                    actor_id="runner",
                    resource_type="task",
                    resource_id=str(task.id),
                    message=note,
                    metadata={"error_code": error_code, "status": target.value},
                )


async def _write_code_baseline(session: AsyncSession, *, task: Task, files: dict[str, str]) -> None:
    """把代码输入留档到工作区：任务输入可事后复现，正文不进 tasks 行。"""
    workspace = await session.get(Workspace, task.workspace_id)
    if workspace is None:
        raise TaskRunnerError("任务的工作区不存在。")

    base = Path(workspace.root_path) / "code" / task.id.hex

    def _write() -> None:
        for name, content in files.items():
            # 复用沙箱同一套相对路径纪律：.. / 绝对路径一律拒绝。
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or "." in parts:
                raise TaskRunnerError(f"非法文件路径：{name!r}")
            target = (base / name).resolve()
            if base.resolve() not in target.parents:
                raise TaskRunnerError(f"文件路径越界：{name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    await asyncio.to_thread(_write)


def _execution_plan(kind: TaskKind, input_json: dict[str, Any]) -> tuple[str, str, Any]:
    """按任务类型给出 (模板代码, stdin, 产物提取器)。平台校验全在构造 stdin 时完成。"""
    if kind == TaskKind.DOCUMENT:
        stdin = json.dumps(
            build_document_input(
                title=str(input_json.get("title") or "未命名文档"),
                markdown=str(input_json.get("markdown") or ""),
            ),
            ensure_ascii=False,
        )
        return _DOCX_CODE, stdin, extract_artifact

    if kind == TaskKind.CODE:
        spec = build_code_input(
            files=input_json.get("files"),
            base=input_json.get("base"),
            test_command=input_json.get("test_command"),
        )
        return _CODE_VERIFY_CODE, json.dumps(spec, ensure_ascii=False), extract_code_patch

    raise TaskRunnerError(f"暂不支持的任务类型：{kind.value}。")


async def run_task(
    *,
    task_id: uuid.UUID,
    client: SandboxClient | None = None,
    gateway: Any | None = None,
) -> str:
    """执行一个任务，返回终态 value。调用方负责租户上下文。"""
    # 1) 认领：同租户单实例下 queued 只会有一个执行者；撞上就跳过。
    async with async_session_factory() as session:
        async with session.begin():
            task = await session.get(Task, task_id)
            if task is None or task.status != TaskStatus.QUEUED:
                return "skipped"
            if task.kind not in (TaskKind.DOCUMENT, TaskKind.CODE, TaskKind.SCHEDULE):
                await transition(
                    session,
                    task,
                    TaskStatus.FAILED,
                    note=f"暂不支持的任务类型：{task.kind.value}。",
                )
                return TaskStatus.FAILED.value

            raw_input = dict(task.input_json or {})
            is_schedule = task.kind == TaskKind.SCHEDULE
            if is_schedule:
                # 定时任务的模板是 {compute: {...}, delivery: {...}}。
                # 只展开 compute 给现有的沙箱链路；delivery 留在原模板里，
                # 投递服务自己读——它是平台侧动作，不进沙箱 stdin。
                compute = raw_input.get("compute")
                compute_kind = compute.get("kind") if isinstance(compute, dict) else None
                if compute_kind not in ("document", "code"):
                    await transition(
                        session,
                        task,
                        TaskStatus.FAILED,
                        note="定时任务缺少合法的 compute 规格（TASK_INPUT_INVALID）。",
                    )
                    return TaskStatus.FAILED.value
                kind = TaskKind(compute_kind)
                input_json = dict(compute)
            else:
                kind = task.kind
                input_json = raw_input

            await transition(session, task, TaskStatus.RUNNING, actor_id="runner")
            tenant_id = task.tenant_id
            user_id = task.user_id
            root_path = ""
            workspace = await session.get(Workspace, task.workspace_id)
            if workspace is not None:
                root_path = workspace.root_path

    # 2) 构造执行计划：输入校验在这里完成，非法输入是「拒绝」，不是 Unknown。
    try:
        code, stdin, extractor = _execution_plan(kind, input_json)
    except (CodeInputError, TaskRunnerError) as exc:
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note=f"任务输入不合法：{exc}",
            error_code="TASK_INPUT_INVALID",
        )
        return TaskStatus.FAILED.value

    # 3) 准备：任务输入留档 + 任务前快照。快照 id 随执行请求交给沙箱写进
    # 执行记录，这样「这次执行用的是哪份快照」只有一个事实来源。
    snapshot_id = ""
    try:
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            if kind == TaskKind.DOCUMENT:
                await _write_draft(
                    session,
                    task=task,
                    markdown=str(input_json.get("markdown") or ""),
                )
            else:
                await _write_code_baseline(
                    session, task=task, files=input_json.get("files") or {}
                )
        if root_path:
            snap = await asyncio.to_thread(snapshot_service.create_snapshot, root_path)
            snapshot_id = snap.snapshot_id
    except Exception:
        logger.exception("任务准备失败。", extra={"event": "task_prepare_failed"})
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note="任务准备失败。",
            error_code="TASK_PREPARE_FAILED",
        )
        return TaskStatus.FAILED.value

    # 4) 执行：沙箱不联网、不挂宿主目录、不持凭据，输入全在 stdin。
    sandbox = client or _task_client()
    try:
        result = await sandbox.execute(
            code=code,
            language="python",
            stdin=stdin,
            timeout_seconds=settings.task_execution_timeout_seconds,
            idempotency_key=f"task:{task_id}",
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=str(task_id),
            snapshot_id=snapshot_id,
        )
    except (SandboxUnavailable, SandboxTimeout) as exc:
        # 结果未知：不重试、不声称失败，交人工确认。
        await _finish(
            task_id=task_id,
            target=TaskStatus.UNKNOWN,
            note=f"沙箱结果未知：{exc}",
            error_code="SANDBOX_UNCERTAIN",
        )
        return TaskStatus.UNKNOWN.value
    except (SandboxRejected, SandboxError) as exc:
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note=f"沙箱拒绝执行：{exc}",
            error_code="SANDBOX_REJECTED",
        )
        return TaskStatus.FAILED.value

    # 5) 独立回读：执行时那次响应不能自证。
    try:
        reread = await sandbox.get_execution(
            str(result.execution_id or ""), tenant_id=tenant_id
        )
    except (SandboxError, ValueError) as exc:
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note=f"回读执行记录失败：{exc}",
            error_code="EXECUTION_READBACK_FAILED",
        )
        return TaskStatus.FAILED.value

    if not readback_matches(result, reread):
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note="回读结果与执行响应不一致。",
            error_code="EXECUTION_MISMATCH",
            cpu_ms=result.cpu_ms,
        )
        return TaskStatus.FAILED.value

    if not result.succeeded or result.truncated:
        code_name = "SANDBOX_OUTPUT_TRUNCATED" if result.truncated else "SANDBOX_FAILED"
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note=f"沙箱未成功执行：{result.stderr[:200]}",
            error_code=code_name,
            cpu_ms=result.cpu_ms,
        )
        return TaskStatus.FAILED.value

    # 6) 产物落库。失败也是 FAILED——产物不完整时不能标成功。
    try:
        data = extractor(result.stdout)
        if kind == TaskKind.DOCUMENT:
            display_name, content_type = _DOCX_NAME, _DOCX_TYPE
        else:
            display_name, content_type = _PATCH_NAME, _PATCH_TYPE
        async with async_session_factory() as session:
            async with session.begin():
                task = await session.get(Task, task_id)
                artifact = await _store_artifact(
                    session, task=task, data=data,
                    display_name=display_name, content_type=content_type,
                )
                output = {
                    "artifact_id": str(artifact.id),
                    "object_key": artifact.object_key,
                    "filename": artifact.filename,
                    "sha256": artifact.sha256,
                    "execution_id": result.execution_id,
                }
                # 测试结果写入 output：测试未过不影响「diff 已产出」这个事实，
                # 但调用方必须能看出测试结论，不把失败测试伪装成全绿。
                if kind == TaskKind.CODE:
                    output["test"] = extract_test_result(result.stdout)
    except Exception as exc:
        logger.exception("任务产物落库失败。", extra={"event": "task_artifact_failed"})
        await _finish(
            task_id=task_id,
            target=TaskStatus.FAILED,
            note=f"产物落库失败：{exc}",
            error_code="TASK_ARTIFACT_FAILED",
            cpu_ms=result.cpu_ms,
        )
        return TaskStatus.FAILED.value

    # 7) 定时投递：计算已成功落库，投递结果独立记录，不影响产物。
    #    失败不把已生成的东西抹掉，但也不伪装成「已送达」——output.delivery
    #    与独立审计里都有明确结论。
    delivery_note = ""
    if is_schedule:
        try:
            outcome = await deliver_task_output(task_id=task_id, gateway=gateway)
            output["delivery"] = outcome.as_dict()
            delivery_note = f"；投递{outcome.status}"
        except Exception:
            # 投递服务自身异常（例如审计库故障）——不推翻已落库的产物。
            logger.exception(
                "定时投递记账失败。",
                extra={"event": "schedule_delivery_error", "task_id": str(task_id)},
            )
            output["delivery"] = {
                "status": "unknown",
                "tool_name": "",
                "error_code": "DELIVERY_BOOKKEEPING_FAILED",
                "message": "投递已尝试，但结论写入失败，需人工核查。",
                "external_operation_id": None,
            }
            delivery_note = "；投递结果未知"

    await _finish(
        task_id=task_id,
        target=TaskStatus.SUCCEEDED,
        note="产物已生成并通过回读核验。" + delivery_note,
        cpu_ms=result.cpu_ms,
        output=output,
    )
    return TaskStatus.SUCCEEDED.value


async def dispatch_queued_tasks(*, tenant_id: str, limit: int = 5) -> int:
    """扫描本租户的 queued 任务并逐个执行；返回实际收口的数量。

    调用方在 tenant_scope 内；单实例由 maintenance 的 advisory lock 保证。
    """
    async with async_session_factory() as session:
        ids = list(
            await session.scalars(
                select(Task.id)
                .where(Task.status == TaskStatus.QUEUED)
                .order_by(Task.created_at)
                .limit(limit)
            )
        )

    done = 0
    for task_id in ids:
        try:
            if await run_task(task_id=task_id) != "skipped":
                done += 1
        except Exception:
            logger.exception(
                "任务执行器异常。",
                extra={"event": "task_runner_error", "task_id": str(task_id)},
            )
            await _finish(
                task_id=task_id,
                target=TaskStatus.FAILED,
                note="执行器异常，任务失败。",
                error_code="TASK_RUNNER_ERROR",
            )
    return done
