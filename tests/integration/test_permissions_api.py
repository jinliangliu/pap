# tests/integration/test_permissions_api.py
"""权限接口：清单、单工具策略、平台级开关。

重点不是「能不能 200」，而是三件容易假绿的事：

1. 保存后**生效值**要跟着变——页面回显与执行链读的是同一份判定；
2. 「未配置」要真的把行删掉，否则平台默认动作一改，历史行会挡住它；
3. 写工具不能被标成「读取」——那是免审批直通，必须 400。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.approval import RiskLevel
from app.models.platform_setting import PlatformSetting
from app.models.tenant import TenantToolPolicy
from app.services import runtime_settings
from app.services.tenant_service import get_tenant_service
from app.tools.dependencies import get_tool_gateway
from app.tools.gateway import is_write_risk

pytestmark = pytest.mark.anyio

BASE = "/api/v1/admin/permissions"

# 平台管理员登录在 platform 租户；权限接口默认就改这个租户的策略。
TENANT = "platform"


def _read_tool():
    gateway = get_tool_gateway()
    tool = next(
        (t for t in gateway.list_tools() if not is_write_risk(t.risk_level)),
        None,
    )
    assert tool is not None, "测试环境应当注册了至少一个只读工具"
    return tool


def _write_tool():
    gateway = get_tool_gateway()
    tool = next((t for t in gateway.list_tools() if is_write_risk(t.risk_level)), None)
    assert tool is not None, "测试环境应当注册了至少一个写入类工具"
    return tool


@pytest.fixture
async def clean_permissions():
    """清策略与平台开关，避免用例之间互相影响。"""
    yield

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(TenantToolPolicy))
            await session.execute(
                delete(PlatformSetting).where(
                    PlatformSetting.key.in_(
                        (
                            runtime_settings.DEFAULT_TOOL_ACTION,
                            runtime_settings.MCP_INBOUND_ENABLED,
                        )
                    )
                )
            )

    get_tenant_service().invalidate()
    runtime_settings.reset_for_tests()


async def test_list_returns_effective_values(admin_client):
    data = (await admin_client.get(BASE)).json()

    assert data["tenant_id"] == TENANT
    assert data["default_action"] == "allow"
    assert data["tools"], "清单不该为空"
    assert data["summary"]["total"] == len(data["tools"])

    # 每个工具都要能回答「最终算几级风险、要不要审批」，
    # 只回配置值会逼着前端自己推一遍规则。
    for item in data["tools"]:
        assert item["effective_action"] in ("allow", "confirm", "deny")
        assert "requires_approval" in item


async def test_confirm_forces_approval_on_read_tool(admin_client, clean_permissions):
    tool = _read_tool()

    res = await admin_client.put(
        f"{BASE}/tools/{tool.name}",
        json={"action": "confirm"},
    )
    assert res.status_code == 200, res.text

    item = res.json()
    assert item["action"] == "confirm"
    assert item["effective_action"] == "confirm"
    assert item["requires_approval"] is True
    assert item["base_risk"] == tool.risk_level.value


async def test_write_tool_cannot_be_downgraded_to_read(admin_client, clean_permissions):
    tool = _write_tool()

    res = await admin_client.put(
        f"{BASE}/tools/{tool.name}",
        json={"action": "allow", "risk_override": "read"},
    )

    assert res.status_code == 400
    assert "只能上调" in res.json()["detail"]


async def test_read_tool_can_be_escalated_to_write(admin_client, clean_permissions):
    tool = _read_tool()

    res = await admin_client.put(
        f"{BASE}/tools/{tool.name}",
        json={"action": "allow", "risk_override": "write"},
    )
    assert res.status_code == 200, res.text

    item = res.json()
    assert item["effective_risk"] == RiskLevel.WRITE.value
    assert item["requires_approval"] is True


async def test_reset_deletes_the_row(admin_client, clean_permissions):
    tool = _read_tool()

    await admin_client.put(f"{BASE}/tools/{tool.name}", json={"action": "deny"})

    async with async_session_factory() as session:
        row = await session.get(
            TenantToolPolicy,
            {"tenant_id": TENANT, "tool_name": tool.name},
        )
        assert row is not None

    res = await admin_client.delete(f"{BASE}/tools/{tool.name}")
    assert res.status_code == 200, res.text
    assert res.json()["action"] is None

    async with async_session_factory() as session:
        row = await session.get(
            TenantToolPolicy,
            {"tenant_id": TENANT, "tool_name": tool.name},
        )
        # 「恢复未配置」必须是删行，不能留一行全空的记录：
        # 留空行会让「没配过」与「配过又清空」在库里长得不一样。
        assert row is None


async def test_platform_default_action_changes_visibility(admin_client, clean_permissions):
    """默认动作改成拒绝后，未配置的工具应当从目录里消失。"""
    tool = _read_tool()

    res = await admin_client.put(
        f"{BASE}/platform",
        json={"default_action": "deny", "mcp_inbound_enabled": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["default_action"] == "deny"

    settings_ = await get_tenant_service().settings_for(TENANT, force=True)
    assert settings_.allow_tool(tool.name, ("platform_admin",), default_action="deny") is False

    # 显式允许之后又可见。
    await admin_client.put(f"{BASE}/tools/{tool.name}", json={"action": "allow"})
    settings_ = await get_tenant_service().settings_for(TENANT, force=True)
    assert settings_.allow_tool(tool.name, ("platform_admin",), default_action="deny") is True


async def test_platform_can_disable_inbound_channel(admin_client, clean_permissions):
    res = await admin_client.put(
        f"{BASE}/platform",
        json={"default_action": "allow", "mcp_inbound_enabled": False},
    )
    assert res.status_code == 200, res.text

    assert runtime_settings.mcp_inbound_enabled() is False

    # 再打开，确认不是单向开关。
    await admin_client.put(
        f"{BASE}/platform",
        json={"default_action": "allow", "mcp_inbound_enabled": True},
    )
    assert runtime_settings.mcp_inbound_enabled() is True


async def test_unknown_tool_returns_404(admin_client):
    res = await admin_client.put(f"{BASE}/tools/not_a_tool", json={"action": "allow"})
    assert res.status_code == 404


async def test_non_admin_cannot_read_permissions(client):
    res = await client.get(BASE)
    assert res.status_code == 403


async def test_deny_removes_tool_from_tools_listing(admin_client, clean_permissions):
    """工具页与权限页必须说同一句话。

    目录过滤是同步路径，只读租户策略缓存；缓存没预热时它会跳过策略判定，
    于是权限页写着「拒绝」、工具页照样列出来。这条用例盯的就是那次预热。
    """
    tool = _read_tool()

    await admin_client.put(f"{BASE}/tools/{tool.name}", json={"action": "deny"})

    listed = (await admin_client.get("/api/v1/tools")).json()
    names = [item["name"] for item in listed]

    assert tool.name not in names
