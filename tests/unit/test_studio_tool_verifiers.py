# tests/unit/test_studio_tool_verifiers.py
"""构建助手的写工具必须能回读核验，也必须说得清在批什么。

**为什么单独立一条**：`verification_hint` 读起来像已经核过了，实际只是给人事后
看的说明。真正让审批卡从 EXECUTED 走到 VERIFIED 的是 `verifier`——不声明它，
后台写成功了，界面上和「没核验」长得一模一样，而没人会追问一个看起来正常的
状态。这个洞已经在导入类、流程保存、智能体的四个写操作上各踩过一次。

`summarize` 是另一条线：`verifier` 管执行之后核什么，`summarize` 管点确认之前
人能不能看懂。卡上的正文只能由 `interrupt(message=…)` 给，同轮助手那段文字会被
整段顶掉——没有摘要，卡上就只剩工具名与一份折叠参数。曾经 15 个审批类工具里
只有「保存流程」写了摘要，其余全是盲批。

这两条用例盯的都是**新增**：以后再加一个写工具而不声明，断言立刻红。两个清单
都已清零，所以现在都是 `== set()`；真出现欠账时要**先把欠账写在这里**再改，
否则等于把「暂时没写」变成「以后也不必写」。
"""

from __future__ import annotations

from app.tools.builtins.studio_admin_tools import build_studio_admin_tools
from app.tools.builtins.studio_tools import build_studio_tools

# 已知欠账：声明了 approval，但 verifier 还没写。现在是空的。
STILL_WITHOUT_READBACK: set[str] = set()

# 同理：声明了 approval，但确认卡正文（`summarize`）还没写。现在是空的。
STILL_WITHOUT_SUMMARY: set[str] = set()


def test_every_approval_tool_declares_a_verifier():
    tools = [*build_studio_tools(), *build_studio_admin_tools()]

    missing = {
        tool.name
        for tool in tools
        if tool.requires_approval and getattr(tool, "verifier", None) is None
    }

    assert missing == STILL_WITHOUT_READBACK


def test_every_approval_tool_says_what_it_will_do():
    """审批卡的正文只能由 `interrupt(message=…)` 给。

    同轮助手那段文字会被整段顶掉，所以没有 `summarize` 的写工具，卡片上就只剩
    工具名与一份折叠参数——审批人批的是他没看过的东西。这条断言盯的同样是
    **新增**：以后补一个写工具，忘了写摘要立刻红。
    """
    tools = [*build_studio_tools(), *build_studio_admin_tools()]

    missing = {
        tool.name
        for tool in tools
        if tool.requires_approval and getattr(tool, "summarize", None) is None
    }

    assert missing == STILL_WITHOUT_SUMMARY


def test_the_verifiers_that_exist_are_reachable():
    """反向：清单里不该留着已经补上的名字（否则欠账表越看越像成绩单）。"""
    tools = {tool.name: tool for tool in [*build_studio_tools(), *build_studio_admin_tools()]}

    for name in (
        "studio_workflow_save",
        "studio_workflow_delete",
        "studio_import",
        "studio_agent_save",
        "studio_agent_set_status",
        "studio_agent_delete",
        "studio_agent_restore",
        "studio_skill_save",
        "studio_skill_delete",
        "studio_tool_save",
        "studio_tool_delete",
        "studio_connector_save",
        "studio_connector_delete",
    ):
        assert tools[name].verifier is not None, name
