# tests/unit/test_workflow_internal_agent.py
"""内置助手不能当编排节点。

运行时早就拦住了：`agent_invoker.invoke` 对 `is_builtin` 的目标返回
「不能承接转交任务（内置智能体）」。但**保存期不判**，图照样存得进去，
一直到来人 `/workflow` 触发才失败——而那时配图的人早就不在屏幕前了。
这正是 `_ensure_agents_available` 存在的理由（见它的 docstring）。

两条必须成对：
- 保存侧（`workflow_service._ensure_agents_available`）要拒，且理由带节点；
- 清单侧（`studio_tools._node_agents_line`）要把它排除——列进去只会让模型
  把它填进图，然后保存被拒，白跑一轮。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.agent import AgentStatus
from app.orchestration.definition import compile_definition
from app.services import workflow_service
from app.tools.builtins import studio_tools

ONE_STEP = {
    "entry": "solo",
    "nodes": [
        {"id": "solo", "agent": "studio", "task": "查一下 {{input.text}}", "next": []}
    ],
}


def _row(
    name: str,
    display: str,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    builtin: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        display_name=display,
        status=status,
        is_builtin=builtin,
    )


def test_save_rejects_the_builtin_assistant_as_a_node():
    plan = compile_definition(ONE_STEP)

    with pytest.raises(workflow_service.WorkflowError) as exc_info:
        workflow_service._ensure_agents_available(
            plan, [_row("studio", "构建助手", builtin=True)]
        )

    error = exc_info.value
    assert error.code == "WORKFLOW_INTERNAL_AGENT"
    assert error.node == "solo"
    # 说清楚「它是什么、该换成什么」，否则模型只会换个名字再试一次。
    assert "内置" in error.message and "业务智能体" in error.message


def test_save_still_accepts_a_business_agent():
    """正例必须成对：只测拒绝，一个「什么都拒」的实现也能全绿。"""

    plan = compile_definition(
        {
            "entry": "solo",
            "nodes": [
                {"id": "solo", "agent": "ops", "task": "查 {{input.text}}", "next": []}
            ],
        }
    )

    workflow_service._ensure_agents_available(plan, [_row("ops", "运维")])


def test_available_line_excludes_the_builtin_assistant():
    text = studio_tools._node_agents_line(
        [_row("studio", "构建助手", builtin=True), _row("ops", "运维")]
    )

    assert "能放进编排图的（启用中的业务智能体）：ops（运维）" in text
    assert "studio" not in text
    assert "构建助手" not in text


def test_available_line_says_so_when_only_the_builtin_is_ready():
    """只剩内置助手时不能给一句「没有」了事——那会让人以为智能体全停用了。"""

    text = studio_tools._node_agents_line([_row("studio", "构建助手", builtin=True)])

    assert "没有启用中的业务智能体" in text
    assert "studio" not in text
