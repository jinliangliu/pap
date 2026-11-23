# tests/unit/test_studio_tool_summaries.py
"""写工具的确认卡正文：只陈述参数，但要点必须都在。

`summarize` 与 `verifier` 是两条独立的线：前者决定「人批的是什么」，后者决定
「执行后核出什么」。没有摘要时，卡上只剩工具名与一份折叠参数——审批人批的是
他没看过的东西。这组用例盯三件容易写回去的事：

- `allowed_tools` 的三种写法（不传 / null / []）行为完全不同，摘要必须分开说；
- update 只改传入字段，摘要不能把没传的字段也写出来（人会以为整份被替换）；
- 凭据类工具必须明说「凭据不经过这次操作」，且摘要里不能出现凭据值。
"""

from __future__ import annotations

from app.tools.builtins import studio_admin_tools as admin
from app.tools.builtins import studio_tools as studio


async def _skill(**arguments) -> str:
    return await admin._summarize_skill_save(None, arguments)


async def test_skill_tool_whitelist_has_three_different_meanings():
    """不传 / null / [] 在技能里是「不限 / 不限 / 一个都调不动」三种行为。"""
    empty = await _skill(
        mode="create", name="valve", allowed_tools=[], trigger_keywords=["阀门"]
    )
    assert "新建技能「valve」（valve）" in empty
    assert "工具白名单为空（一个都调不动）" in empty

    unlimited = await _skill(mode="update", name="valve", allowed_tools=None)
    assert "工具不限（不额外收窄）" in unlimited

    listed = await _skill(mode="create", name="valve", allowed_tools=["query", "create"])
    assert "工具 2 个（query、create）" in listed


async def test_update_summary_only_lists_the_fields_that_were_sent():
    text = await _skill(mode="update", name="valve")
    assert "修改技能「valve」（valve）" in text
    assert "本次改动：无" in text
    assert "工具" not in text
    assert "指令" not in text

    only_status = await _skill(mode="update", name="valve", enabled=False)
    assert "本次改动：停用" in only_status
    assert "指令" not in only_status


async def test_http_tool_summary_shows_target_and_never_echoes_header_values():
    text = await admin._summarize_tool_save(
        None,
        {
            "kind": "http",
            "mode": "create",
            "name": "dispatch_order",
            "display_name": "派单",
            "method": "post",
            "url": "https://ops.example.com/orders",
            "risk_level": "write",
            "params": [
                {"name": "station_id", "required": True},
                {"name": "note"},
            ],
            "headers": {"X-Api-Key": "super-secret-value"},
            "timeout_seconds": 15,
        },
    )
    assert "新建自定义工具「派单」（dispatch_order）" in text
    assert "POST https://ops.example.com/orders" in text
    assert "风险 write" in text
    assert "参数 2 个（必填：station_id）" in text
    assert "自定义请求头 1 个（值不回显）" in text
    assert "super-secret-value" not in text
    assert "凭据不在这次操作里" in text


async def test_mcp_summary_says_local_name_points_at_remote_tool():
    text = await admin._summarize_tool_save(
        None,
        {
            "kind": "mcp",
            "mode": "create",
            "name": "fc_query",
            "remote_name": "query_device",
            "risk_level": "read",
        },
    )
    assert "本地名 fc_query ← 远端 query_device" in text
    assert "默认 MCP 端点" in text
    assert "凭据不经过这次操作" in text


async def test_python_tool_summary_says_it_will_be_refused():
    text = await admin._summarize_tool_save(
        None, {"kind": "python", "mode": "create", "name": "whatever"}
    )
    assert "会被直接拒绝" in text


async def test_delete_summaries_name_the_consequence_not_just_the_object():
    skill = await admin._summarize_skill_delete(None, {"name": "valve"})
    assert "删除技能「valve」" in skill
    assert "下次运行就没有这条技能" in skill

    tool = await admin._summarize_tool_delete(None, {"kind": "mcp", "name": "fc_query"})
    assert "远端 MCP 服务不受影响" in tool
    assert "失去这个工具" in tool

    connector = await admin._summarize_connector_delete(None, {"name": "fio"})
    assert "下面还挂着工具时会被拒绝" in connector

    workflow = await admin._summarize_workflow_delete(None, {"name": "dispatch"})
    assert "历史运行记录与未完结的运行都保留" in workflow


async def test_knowledge_summaries_show_size_and_scope():
    added = await admin._summarize_knowledge_add(
        None, {"title": "调压站巡检规程", "content": "一二三四五", "access_scope": "confidential"}
    )
    assert "「调压站巡检规程」" in added
    assert "正文 5 字" in added
    assert "机密（confidential）" in added

    deleted = await admin._summarize_knowledge_delete(
        None, {"document_id": "doc_0123456789ab", "confirm_title": "调压站巡检规程"}
    )
    assert "doc_0123456789ab" in deleted
    assert "软删除" in deleted


async def test_import_summary_warns_that_auto_will_not_guess():
    text = await admin._summarize_import(
        None, {"file_id": "doc_0123456789ab", "filename": "规程.md", "target": "auto"}
    )
    assert "导入附件 doc_0123456789ab（规程.md） → 自动识别" in text
    assert "判不出会拒绝，不会猜" in text


async def test_connector_summary_promises_nothing_about_credentials():
    bearer = await admin._summarize_connector_save(
        None,
        {
            "mode": "create",
            "name": "fio",
            "kind": "http",
            "base_url": "https://fio.example.com/",
            "auth_type": "bearer",
        },
    )
    assert "认证 Bearer 令牌" in bearer
    assert "请到控制台「连接器」页填写" in bearer
    assert "不要发在对话里" in bearer

    none = await admin._summarize_connector_save(
        None, {"mode": "create", "name": "public", "auth_type": "none"}
    )
    assert "不需要凭据" in none
    assert "控制台「连接器」页" not in none


async def test_connector_update_does_not_guess_the_untouched_auth_type():
    """update 只传 `base_url` 时，认证方式与类型都是「沿用」，不能按默认值说。"""
    text = await admin._summarize_connector_save(
        None,
        {"mode": "update", "name": "fio", "base_url": "https://fio.example.com/"},
    )
    assert "本次改动：端点 https://fio.example.com/" in text
    assert "类型 mcp" not in text  # 没传 kind 就不是 mcp
    assert "不需要凭据" not in text  # 没传 auth_type 不等于 none
    assert "认证方式与凭据沿用现有配置" in text


async def test_agent_summary_keeps_the_three_tool_modes_apart():
    unmetered = await studio._summarize_agent_save(
        None, {"mode": "update", "name": "patrol"}
    )
    assert "本次改动：无" in unmetered
    assert "工具" not in unmetered

    cleared = await studio._summarize_agent_save(
        None, {"mode": "update", "name": "patrol", "allowed_tools": None}
    )
    assert "工具：取消收窄（按平台基线可用）" in cleared

    silent = await studio._summarize_agent_save(
        None, {"mode": "update", "name": "patrol", "allowed_tools": []}
    )
    assert "工具：一个都不给（纯对话）" in silent

    listed = await studio._summarize_agent_save(
        None,
        {
            "mode": "create",
            "name": "patrol",
            "display_name": "巡检",
            "allowed_tools": ["query_device"],
            "can_write": False,
            "status": "active",
        },
    )
    assert "新建智能体「巡检」（patrol）" in listed
    assert "工具 1 个（query_device）" in listed
    assert "只读" in listed
    assert "状态 启用（active）" in listed
    # 已经显式给了状态，就不该再说「默认是草稿」。
    assert "默认是草稿" not in listed


async def test_agent_status_delete_and_restore_say_what_they_do_not_touch():
    status = await studio._summarize_agent_status(
        None, {"name": "patrol", "status": "disabled"}
    )
    assert "设为 停用（disabled）" in status
    assert "不动边界字段" in status

    deleted = await studio._summarize_agent_delete(None, {"name": "patrol"})
    assert "只有已停用的智能体能删" in deleted

    restored = await studio._summarize_agent_restore(
        None, {"name": "patrol", "revision": 3}
    )
    assert "恢复到第 3 版" in restored
    assert "会产生新版本号" in restored
