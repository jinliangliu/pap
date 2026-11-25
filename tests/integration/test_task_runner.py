# tests/integration/test_task_runner.py
"""任务执行器集成：用假沙箱跑通 DOCUMENT 全链路。

覆盖只有真连库才看得见的契约：
- 产物落 artifacts 且状态/输出指向同一个 artifact；
- 回读不一致时不落产物、状态 FAILED；
- stdout 截断时明确失败，不落半份 docx；
- 终态只写一次，重复认领被跳过。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.session import async_session_factory
from app.integrations.sandbox_client import SandboxResult, SandboxUnavailable
from app.models.artifact import Artifact
from app.models.audit import AuditEventType, AuditLog
from app.models.task import Task, TaskKind, TaskStatus, Workspace
from app.services import snapshot_service, workspace_service
from app.services import task_runner_service as runner
from app.services.task_service import create_task
from app.tenancy import tenant_scope

_TENANT = "ti-runner"
_USER = "alice"


@dataclass
class FakeClient:
    """最小沙箱替身：execute 与 get_execution 都按同一份结果回答。"""

    stdout: str
    status: str = "succeeded"
    cpu_ms: int | None = 17
    truncated: bool = False
    reread: SandboxResult | None = None
    execute_error: Exception | None = None
    calls: int = 0

    def _result(self) -> SandboxResult:
        return SandboxResult(
            execution_id="exec-runner-1",
            status=self.status,
            exit_code=0 if self.status == "succeeded" else 1,
            stdout=self.stdout,
            stderr="",
            cpu_ms=self.cpu_ms,
            truncated=self.truncated,
        )

    async def execute(self, **kwargs) -> SandboxResult:
        self.calls += 1
        if self.execute_error:
            raise self.execute_error
        return self._result()

    async def get_execution(self, execution_id: str, *, tenant_id: str) -> SandboxResult:
        return self.reread or self._result()


def _payload(text: str = "docx") -> str:
    content = b"PK\x03\x04" + (text * 300).encode("utf-8")
    return base64.b64encode(content).decode("ascii")


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workspace_service.settings, "workspace_storage_dir", str(tmp_path / "ws")
    )
    monkeypatch.setattr(
        snapshot_service.settings, "workspace_storage_dir", str(tmp_path / "ws")
    )
    return tmp_path / "ws"


@pytest.fixture
async def seeded_task(workspace_dir):
    """建工作区与一个 queued DOCUMENT 任务，返回 task id。"""
    key = f"runner-doc-{uuid.uuid4().hex}"
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id=_TENANT, user_id=_USER
                )
                task, _ = await create_task(
                    session,
                    tenant_id=_TENANT,
                    user_id=_USER,
                    workspace_id=ws.id,
                    kind=TaskKind.DOCUMENT,
                    idempotency_key=key,
                    input_json={"title": "周报", "markdown": "# 周报\n- 完成一件"},
                )
                return task.id


async def _load(task_id: uuid.UUID) -> tuple[Task, list[Artifact], list[AuditLog]]:
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            artifacts = list(
                await session.scalars(select(Artifact).where(Artifact.user_id == _USER))
            )
            audits = list(await session.scalars(select(AuditLog)))
            return task, artifacts, audits


async def test_document_task_succeeds_and_links_artifact(seeded_task):
    fake = FakeClient(stdout=f"ok\n{runner._ARTIFACT_MARK}{_payload()}\n")
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_task, client=fake)

    assert state == TaskStatus.SUCCEEDED.value
    task, artifacts, audits = await _load(seeded_task)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.cpu_ms == 17
    assert task.output_json["filename"] == "document.docx"
    assert any(
        item.object_key == task.output_json["object_key"] for item in artifacts
    )
    assert any(
        row.event_type == AuditEventType.TASK_SUCCEEDED
        and row.resource_id == str(seeded_task)
        for row in audits
    )

    # 重复认领不会跑第二次沙箱。
    with tenant_scope(_TENANT):
        assert await runner.run_task(task_id=seeded_task, client=fake) == "skipped"
    assert fake.calls == 1


async def test_readback_mismatch_fails_without_artifact(seeded_task):
    mismatched = replace(
        SandboxResult(
            execution_id="exec-runner-1",
            status="succeeded",
            exit_code=0,
            stdout="",
            stderr="",
            cpu_ms=17,
        ),
        cpu_ms=99,
    )
    fake = FakeClient(
        stdout=f"{runner._ARTIFACT_MARK}{_payload()}", reread=mismatched
    )
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_task, client=fake)

    assert state == TaskStatus.FAILED.value
    task, artifacts, audits = await _load(seeded_task)
    assert task.status == TaskStatus.FAILED
    assert task.output_json is None
    assert any(
        row.event_type == AuditEventType.TASK_FAILED
        and row.resource_id == str(seeded_task)
        and row.metadata_json.get("error_code") == "EXECUTION_MISMATCH"
        for row in audits
    )


async def test_truncated_output_fails_without_artifact(seeded_task):
    fake = FakeClient(
        stdout=f"{runner._ARTIFACT_MARK}{_payload()}",
        truncated=True,
    )
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_task, client=fake)

    assert state == TaskStatus.FAILED.value
    task, _, audits = await _load(seeded_task)
    assert task.output_json is None
    assert any(
        row.event_type == AuditEventType.TASK_FAILED
        and row.resource_id == str(seeded_task)
        and row.metadata_json.get("error_code") == "SANDBOX_OUTPUT_TRUNCATED"
        for row in audits
    )


async def test_sandbox_unavailable_marks_unknown(seeded_task):
    fake = FakeClient(stdout="", execute_error=SandboxUnavailable("连不上"))
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_task, client=fake)

    assert state == TaskStatus.UNKNOWN.value
    task, _, audits = await _load(seeded_task)
    assert task.status == TaskStatus.UNKNOWN
    assert task.output_json is None
    assert any(
        row.event_type == AuditEventType.TASK_UNKNOWN
        and row.resource_id == str(seeded_task)
        for row in audits
    )


# ---- 编程事务 -------------------------------------------------------------


def _patch_payload(text: str = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n") -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _code_stdout(passed: bool = True) -> str:
    result = {
        "ran": True,
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "output": "ok" if passed else "FAIL",
    }
    return (
        f"{runner._ARTIFACT_MARK}{_patch_payload()}\n"
        f"{runner._TEST_MARK}{json.dumps(result)}\n"
    )


@pytest.fixture
async def seeded_code_task(workspace_dir):
    key = f"runner-code-{uuid.uuid4().hex}"
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                ws, _ = await workspace_service.get_or_create(
                    session, tenant_id=_TENANT, user_id=_USER
                )
                task, _ = await create_task(
                    session,
                    tenant_id=_TENANT,
                    user_id=_USER,
                    workspace_id=ws.id,
                    kind=TaskKind.CODE,
                    idempotency_key=key,
                    input_json={
                        "files": {"a.py": "y\n"},
                        "base": {"a.py": "x\n"},
                        "test_command": ["python3", "-m", "pytest", "-q"],
                    },
                )
                return task.id


async def test_code_task_succeeds_with_patch_and_test_result(seeded_code_task, workspace_dir):
    fake = FakeClient(stdout="ok\n" + _code_stdout(passed=True))
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_code_task, client=fake)

    assert state == TaskStatus.SUCCEEDED.value
    task, artifacts, _ = await _load(seeded_code_task)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.output_json["filename"] == "changes.patch"
    assert task.output_json["test"]["passed"] is True
    assert any(item.object_key == task.output_json["object_key"] for item in artifacts)

    # 代码输入留档到工作区，可事后复现。路径以库里工作区的 root_path 为准：
    # 测试夹具共享同一用户工作区行，不能假设它落在本次的 workspace_dir。
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            ws = await session.get(Workspace, task.workspace_id)
            ws_root = ws.root_path
    draft = Path(ws_root) / "code" / seeded_code_task.hex / "a.py"
    assert draft.exists() and draft.read_text(encoding="utf-8") == "y\n"


async def test_code_task_failed_test_still_lands_patch(seeded_code_task):
    """测试未过不等于沙箱失败：diff 仍是有效产物，测试结论如实写进 output。"""
    fake = FakeClient(stdout=_code_stdout(passed=False))
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_code_task, client=fake)

    assert state == TaskStatus.SUCCEEDED.value
    task, artifacts, _ = await _load(seeded_code_task)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.output_json["test"]["passed"] is False
    assert task.output_json["test"]["exit_code"] == 1
    assert artifacts, "测试失败时 diff 仍应落库"


async def test_code_task_rejects_path_traversal(seeded_code_task):
    """输入校验在沙箱之前：路径穿越直接失败，不进沙箱、不留工作区文件。"""
    with tenant_scope(_TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                task = await session.get(Task, seeded_code_task)
                task.input_json = {"files": {"../evil.py": "x"}}

    fake = FakeClient(stdout=_code_stdout())
    with tenant_scope(_TENANT):
        state = await runner.run_task(task_id=seeded_code_task, client=fake)

    assert state == TaskStatus.FAILED.value
    assert fake.calls == 0
    task, _, audits = await _load(seeded_code_task)
    assert any(
        row.metadata_json.get("error_code") == "TASK_INPUT_INVALID" for row in audits
    )
