# tests/unit/test_workflow_command.py
"""两个触发入口必须给出同一个 `Command`。

入口有两个：消息里敲 `/workflow <名字> <输入>`，和界面上直接选一条流程
（`ChatRequest.workflow`）。**名字校验只能有一份**：两边各写一遍，
迟早出现「手打的名字不合法，下拉选同一个名字却放行」，而排查时谁也
想不到是那两个分支的差别。

这个文件只盯这一件事，跑得比集成测试快得多。
"""

from __future__ import annotations

import pytest

from app.services import workflow_service


def test_typed_command_keeps_name_and_payload():
    command = workflow_service.parse_command("/workflow triage 3 号泵跳停")
    assert command is not None
    assert (command.name, command.input_text, command.invalid) == (
        "triage",
        "3 号泵跳停",
        False,
    )


def test_typed_command_without_input_is_not_invalid():
    """`/workflow triage` 是「缺一句输入」，不是「名字写错了」。

    这两种要回不同的话：前者补一句就能跑，后者改名字才能跑。
    """
    command = workflow_service.parse_command("/workflow triage")
    assert command is not None
    assert command.name == "triage"
    assert command.input_text == ""
    assert command.invalid is False


def test_typed_command_without_name_lists_flows():
    command = workflow_service.parse_command("/workflow")
    assert command is not None
    assert command.name == ""
    assert command.invalid is False


def test_typed_command_rejects_bad_name():
    command = workflow_service.parse_command("/workflow Bad Name 内容")
    assert command is not None
    assert command.invalid is True


@pytest.mark.parametrize(
    "name",
    ["triage", "alert_triage", "wf-1", "a", "x" * 64],
)
def test_selected_flow_builds_the_same_command(name: str):
    command = workflow_service.triggered_command(name, "3 号泵跳停")
    assert command.name == name
    assert command.input_text == "3 号泵跳停"
    assert command.invalid is False
    # 同一个名字走命令行解析也要被接受——两边口径一致才叫「同一种触发」。
    assert workflow_service.parse_command(f"/workflow {name} 3 号泵跳停").name == name


@pytest.mark.parametrize("name", ["", "   ", "Bad Name", "bad/name", "-leading"])
def test_selected_flow_rejects_the_same_names_as_the_command(name: str):
    assert workflow_service.triggered_command(name, "内容").invalid is True


def test_selected_flow_trims_input():
    command = workflow_service.triggered_command("triage", "  3 号泵跳停  ")
    assert command.input_text == "3 号泵跳停"
