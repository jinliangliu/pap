# tests/unit/test_studio_agent_readback.py
"""智能体写操作的回读核验。

四个写工具（保存 / 启停 / 删除 / 恢复）以前都在 handler 里写 `verified=True`：
那次读回与写入同一个事务，提交失败也照样读得到；而 `agents` 挂 RLS，
租户上下文没绑上时是空集、连报错都没有。审批卡上的 VERIFIED 因此并不成立。

现在四个 verifier 各管一件事：

- 保存：库里的**边界**与提交的一致（只核「行在」会漏掉并发改回去）；
- 启停：库里的状态就是请求的那个（「本来就是」也算对，但要说出来）；
- 删除：按 id 查不到 **且** 找得到删除审计（查不到本身什么都不说明）；
- 恢复：当前边界等于第 N 版快照（版本号每次都会变，不能拿它当证据）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.agent import Agent
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"


def _ctx(**kwargs) -> ToolExecutionContext:
    values = {
        "tenant_id": TENANT,
        "user_id": "actor-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "request_id": "req-1",
        "roles": ("dev",),
        "scopes": ("tools:read", "agent:write"),
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean_agents(session, seeded_identity):
    async def _purge() -> None:
        async with async_session_factory() as db:
            with tenant_scope(TENANT):
                await db.execute(delete(Agent))
            await db.commit()

    await _purge()
    yield
    await _purge()


async def _create(name: str = "smoke", *, can_write: bool = False) -> None:
    result = await studio_tools.studio_agent_save(
        _ctx(),
        {
            "mode": "create",
            "name": name,
            "display_name": "烟测",
            "can_write": can_write,
        },
    )
    assert result.success is True, result.message


async def _load(name: str = "smoke") -> Agent | None:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            return await db.scalar(select(Agent).where(Agent.name == name))


# ---------------- 保存 ----------------


async def test_saved_agent_reads_back_with_the_submitted_state():
    await _create()

    result = await studio_tools.verify_agent_saved(
        _ctx(external_operation_id="smoke"),
        {"mode": "create", "name": "smoke", "status": "draft"},
    )

    assert result.verified is True
    assert result.result["agent"]["name"] == "smoke"
    assert result.verification["source"] == "agents_table"


async def test_save_handout_is_the_name_not_a_claim():
    """handler 不再自证：它只交出标识，VERIFIED 由 verifier 给。"""
    result = await studio_tools.studio_agent_save(
        _ctx(),
        {"mode": "create", "name": "smoke", "display_name": "烟测"},
    )

    assert result.verified is False
    assert result.external_operation_id == "smoke"


async def test_a_boundary_that_did_not_stick_is_not_verified():
    """库里是 `None`（不额外收窄），提交的是 `["x"]`：批的和生效的不是一份。"""
    await _create()

    result = await studio_tools.verify_agent_saved(
        _ctx(external_operation_id="smoke"),
        {"name": "smoke", "allowed_tools": ["some_tool"]},
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_AGENT_MISMATCH"
    assert "allowed_tools" in result.verification["diff"]


async def test_missing_agent_is_not_verified():
    result = await studio_tools.verify_agent_saved(
        _ctx(external_operation_id="ghost"), {"name": "ghost"}
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_AGENT_MISSING"


# ---------------- 启停 ----------------


async def test_status_reads_back_as_requested():
    await _create()

    done = await studio_tools.studio_agent_set_status(
        _ctx(), {"name": "smoke", "status": "active"}
    )
    assert done.success is True
    assert done.verified is False

    result = await studio_tools.verify_agent_status(
        _ctx(external_operation_id="smoke"), {"name": "smoke", "status": "active"}
    )

    assert result.verified is True
    assert result.verification["status"] == "active"


async def test_a_status_that_is_not_the_requested_one_is_not_verified():
    await _create()

    result = await studio_tools.verify_agent_status(
        _ctx(external_operation_id="smoke"), {"name": "smoke", "status": "active"}
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_AGENT_STATE"
    assert result.verification["actual"] == "draft"


# ---------------- 删除 ----------------


async def test_deleted_agent_reads_back_as_gone_with_its_audit():
    await _create()
    await studio_tools.studio_agent_set_status(
        _ctx(), {"name": "smoke", "status": "disabled"}
    )

    done = await studio_tools.studio_agent_delete(
        _ctx(), {"name": "smoke", "confirm_name": "smoke"}
    )
    assert done.success is True
    assert done.verified is False
    assert done.external_operation_id

    result = await studio_tools.verify_agent_deleted(
        _ctx(external_operation_id=done.external_operation_id), {"name": "smoke"}
    )

    assert result.verified is True
    assert result.verification["check"] == "absent"


async def test_absence_without_an_audit_is_not_verified():
    """查不到 ≠ 删掉了：RLS 空集、名字不对、租户上下文没绑上都长这样。"""
    await _create()

    result = await studio_tools.verify_agent_deleted(
        _ctx(external_operation_id=str(uuid.uuid4())), {"name": "smoke"}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_evidence"


async def test_a_agent_that_is_still_there_is_not_deleted():
    await _create()
    row = await _load()

    result = await studio_tools.verify_agent_deleted(
        _ctx(external_operation_id=str(row.id)), {"name": "smoke"}
    )

    assert result.success is False
    assert result.error_code == "VERIFY_AGENT_PRESENT"


# ---------------- 恢复 ----------------


async def test_restore_reads_back_the_boundary_of_the_target_revision():
    await _create()
    # v2：把 can_write 打开（边界字段变了，版本号 +1，handler 会写审计）。
    updated = await studio_tools.studio_agent_save(
        _ctx(), {"mode": "update", "name": "smoke", "can_write": True}
    )
    assert updated.success is True

    restored = await studio_tools.studio_agent_restore(
        _ctx(), {"name": "smoke", "revision": 1}
    )
    assert restored.success is True, restored.message
    assert restored.verified is False

    result = await studio_tools.verify_agent_restored(
        _ctx(external_operation_id="smoke"), {"name": "smoke", "revision": 1}
    )

    assert result.verified is True
    assert result.result["restored_from"] == 1


async def test_restore_without_a_snapshot_is_not_verified():
    await _create()

    result = await studio_tools.verify_agent_restored(
        _ctx(external_operation_id="smoke"), {"name": "smoke", "revision": 9}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_snapshot"
