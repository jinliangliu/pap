# tests/unit/test_studio_admin_readback.py
"""技能 / 自定义工具 / 连接器的写操作回读核验。

这六个以前都在 handler 里写 `verified=True`：读回与写入同一个会话，提交失败也
读得到；`skills` 还挂 RLS，上下文没绑上就是空集、连报错都没有。

现在每条核一件不同的事，没有一条只是「行在」：

- 技能保存：`instruction` / `allowed_tools` 与提交的一致（并发或归一化都可能改掉它）；
- 技能删除：按 id 查不到 **且** 找得到 `skill_deleted` 审计（查不到本身不说明什么）；
- 工具保存：库里定义一致 **且** 在网关目录里（两条对着两种不同的失败）；
- 工具删除：按 id 在对应的那张表里查不到（http / mcp 是两张表，不能互相顶替）；
- 连接器保存：端点与认证方式一致（这里最怕的是静默改指向）；
- 连接器删除：按 id 查不到，且有 `connector_deleted` 审计。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.audit import AuditLog
from app.models.connector import Connector
from app.models.custom_tool import CustomTool
from app.models.skill import Skill
from app.tenancy.context import tenant_scope
from app.tools.builtins import studio_admin_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"
PREFIX = "vc_"


def _ctx(**kwargs) -> ToolExecutionContext:
    values = {
        "tenant_id": TENANT,
        "user_id": "actor-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "request_id": "req-1",
        "roles": ("dev",),
        "scopes": ("skill:write", "tools:write", "connector:write"),
        "is_platform_admin": True,
    }
    values.update(kwargs)
    return ToolExecutionContext(**values)


@pytest.fixture(autouse=True)
async def _clean(session, seeded_identity):
    """只清本次前缀造出来的行：库里是 session 级共享的，别按表清空。"""

    async def _purge() -> None:
        async with async_session_factory() as db:
            with tenant_scope(TENANT):
                await db.execute(delete(Skill).where(Skill.name.like(f"{PREFIX}%")))
                await db.execute(delete(AuditLog).where(AuditLog.tenant_id == TENANT))
            await db.execute(delete(CustomTool).where(CustomTool.name.like(f"{PREFIX}%")))
            await db.execute(delete(Connector).where(Connector.name.like(f"{PREFIX}%")))
            await db.commit()

    await _purge()
    yield
    await _purge()


# ---------------- 技能 ----------------


async def _save_skill(name: str = f"{PREFIX}shift") -> None:
    result = await studio_admin_tools.studio_skill_save(
        _ctx(),
        {
            "mode": "create",
            "name": name,
            "display_name": "交接班",
            "description": "交班前核对",
            "instruction": "逐项核对压力、阀位，输出一张表。",
            "trigger_keywords": ["交接班", "交班"],
            "allowed_tools": [],
            "priority": 120,
        },
    )
    assert result.success is True, result.message


async def test_saved_skill_reads_back_with_the_submitted_fields():
    await _save_skill()

    result = await studio_admin_tools.verify_skill_saved(
        _ctx(external_operation_id=f"{PREFIX}shift"),
        {
            "mode": "create",
            "name": f"{PREFIX}shift",
            "display_name": "交接班",
            "instruction": "逐项核对压力、阀位，输出一张表。",
            "trigger_keywords": ["交接班", "交班"],
            "allowed_tools": [],
            "priority": 120,
        },
    )

    assert result.verified is True, result.message
    assert result.verification["source"] == "skills_table"


async def test_skill_verifier_catches_an_instruction_that_drifted():
    """保存成功之后有人改掉了正文：库里那份才是以后真会生效的。"""
    await _save_skill()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Skill).where(Skill.name == f"{PREFIX}shift"))
            row.instruction = "被人改过的正文"
        await db.commit()

    result = await studio_admin_tools.verify_skill_saved(
        _ctx(external_operation_id=f"{PREFIX}shift"),
        {"mode": "update", "name": f"{PREFIX}shift", "instruction": "逐项核对压力、阀位，输出一张表。"},
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_SKILL_MISMATCH"
    assert "instruction" in result.verification["diff"]


async def test_deleted_skill_reads_back_as_gone_with_its_record():
    await _save_skill()
    deleted = await studio_admin_tools.studio_skill_delete(
        _ctx(), {"name": f"{PREFIX}shift", "confirm_name": f"{PREFIX}shift"}
    )
    assert deleted.success is True
    skill_id = deleted.external_operation_id
    assert skill_id

    result = await studio_admin_tools.verify_skill_deleted(
        _ctx(external_operation_id=skill_id), {"name": f"{PREFIX}shift"}
    )

    assert result.verified is True, result.message
    assert result.verification["source"] == "skills_table+audit_logs"


async def test_a_missing_skill_row_alone_is_not_proof_of_deletion():
    """绕开 handler 直接删行：查不到，但没有删除审计——只能说核不出结论。"""
    await _save_skill()

    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Skill).where(Skill.name == f"{PREFIX}shift"))
            skill_id = str(row.id)
            await db.delete(row)
        await db.commit()

    result = await studio_admin_tools.verify_skill_deleted(
        _ctx(external_operation_id=skill_id), {"name": f"{PREFIX}shift"}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_evidence"


async def test_skill_verifier_says_present_when_the_row_is_still_there():
    await _save_skill()
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await db.scalar(select(Skill).where(Skill.name == f"{PREFIX}shift"))
            skill_id = str(row.id)

    result = await studio_admin_tools.verify_skill_deleted(
        _ctx(external_operation_id=skill_id), {"name": f"{PREFIX}shift"}
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_SKILL_PRESENT"


# ---------------- 自定义工具 ----------------


async def _save_tool(name: str = f"{PREFIX}hook") -> None:
    result = await studio_admin_tools.studio_tool_save(
        _ctx(),
        {
            "kind": "http",
            "mode": "create",
            "name": name,
            "display_name": "回调",
            "method": "POST",
            "url": "https://example.com/hook",
            "risk_level": "write",
        },
    )
    assert result.success is True, result.message


async def test_saved_tool_is_in_the_table_and_the_catalog():
    await _save_tool()

    result = await studio_admin_tools.verify_tool_saved(
        _ctx(external_operation_id=f"{PREFIX}hook"),
        {
            "kind": "http",
            "mode": "create",
            "name": f"{PREFIX}hook",
            "display_name": "回调",
            "method": "POST",
            "url": "https://example.com/hook",
            "risk_level": "write",
        },
    )

    assert result.verified is True, result.message
    assert result.verification["source"] == "tool_table+tool_registry"


async def test_tool_verifier_catches_one_that_is_in_the_table_but_not_the_catalog():
    """库里写进去了、目录里没有：这次调用拿不到它，不能算「已保存」。"""
    from app.tools.dependencies import get_tool_gateway, reset_tool_gateway

    await _save_tool()
    assert f"{PREFIX}hook" in {
        tool.name for tool in get_tool_gateway().list_tools(include_disabled=True)
    }

    try:
        reset_tool_gateway()  # 相当于进程重启：目录按代码重建，库里这条不在其中
        result = await studio_admin_tools.verify_tool_saved(
            _ctx(external_operation_id=f"{PREFIX}hook"),
            {"kind": "http", "mode": "create", "name": f"{PREFIX}hook"},
        )
    finally:
        reset_tool_gateway()

    assert result.verified is False
    assert result.error_code == "VERIFY_TOOL_NOT_REGISTERED"


async def test_removed_tool_reads_back_as_gone_with_its_record():
    await _save_tool()
    removed = await studio_admin_tools.studio_tool_delete(
        _ctx(),
        {"kind": "http", "name": f"{PREFIX}hook", "confirm_name": f"{PREFIX}hook"},
    )
    assert removed.success is True
    tool_id = removed.external_operation_id
    assert tool_id

    result = await studio_admin_tools.verify_tool_deleted(
        _ctx(external_operation_id=tool_id), {"kind": "http", "name": f"{PREFIX}hook"}
    )

    assert result.verified is True, result.message
    assert result.verification["source"] == "tool_table+audit_logs"


async def test_a_deleted_tool_without_its_record_is_not_called_verified():
    await _save_tool()

    async with async_session_factory() as db:
        row = await db.scalar(select(CustomTool).where(CustomTool.name == f"{PREFIX}hook"))
        tool_id = str(row.id)
        await db.delete(row)
        await db.commit()

    result = await studio_admin_tools.verify_tool_deleted(
        _ctx(external_operation_id=tool_id), {"kind": "http", "name": f"{PREFIX}hook"}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_evidence"


# ---------------- 连接器 ----------------


async def _save_connector(name: str = f"{PREFIX}xingyun") -> None:
    result = await studio_admin_tools.studio_connector_save(
        _ctx(),
        {
            "mode": "create",
            "name": name,
            "display_name": "星云",
            "kind": "http",
            "base_url": "https://example.com/api/",
            "auth_type": "none",
        },
    )
    assert result.success is True, result.message


async def test_saved_connector_reads_back_with_its_endpoint():
    await _save_connector()

    result = await studio_admin_tools.verify_connector_saved(
        _ctx(external_operation_id=f"{PREFIX}xingyun"),
        {
            "mode": "create",
            "name": f"{PREFIX}xingyun",
            "display_name": "星云",
            "kind": "http",
            # 尾斜杠会被服务层去掉：核验走同一个归一化，不该报不一致
            "base_url": "https://example.com/api/",
        },
    )

    assert result.verified is True, result.message
    assert result.verification["source"] == "connectors_table"


async def test_connector_verifier_catches_an_endpoint_that_drifted():
    """端点被改回旧地址：界面显示新的、模型调旧的，这种错最难查。"""
    await _save_connector()

    async with async_session_factory() as db:
        row = await db.scalar(select(Connector).where(Connector.name == f"{PREFIX}xingyun"))
        row.base_url = "https://old.example.com/api"
        await db.commit()

    result = await studio_admin_tools.verify_connector_saved(
        _ctx(external_operation_id=f"{PREFIX}xingyun"),
        {"mode": "update", "name": f"{PREFIX}xingyun", "base_url": "https://example.com/api"},
    )

    assert result.verified is False
    assert result.error_code == "VERIFY_CONNECTOR_MISMATCH"
    assert "base_url" in result.verification["diff"]


async def test_deleted_connector_reads_back_as_gone_with_its_record():
    await _save_connector()
    removed = await studio_admin_tools.studio_connector_delete(
        _ctx(), {"name": f"{PREFIX}xingyun", "confirm_name": f"{PREFIX}xingyun"}
    )
    assert removed.success is True, removed.message
    connector_id = removed.external_operation_id
    assert connector_id

    result = await studio_admin_tools.verify_connector_deleted(
        _ctx(external_operation_id=connector_id), {"name": f"{PREFIX}xingyun"}
    )

    assert result.verified is True, result.message
    assert result.verification["source"] == "connectors_table+audit_logs"


async def test_a_deleted_connector_without_its_record_is_not_called_verified():
    await _save_connector()

    async with async_session_factory() as db:
        row = await db.scalar(select(Connector).where(Connector.name == f"{PREFIX}xingyun"))
        connector_id = str(row.id)
        await db.delete(row)
        await db.commit()

    result = await studio_admin_tools.verify_connector_deleted(
        _ctx(external_operation_id=connector_id), {"name": f"{PREFIX}xingyun"}
    )

    assert result.verified is False
    assert result.verification["reason"] == "no_evidence"
