# tests/unit/test_chat_build_command.py
"""`/build` 的识别口径。

它是**界面快捷方式**，但服务端也认它：认错一次，正常提问会被 409 打断；
漏认一次，那句「/build 建个流程」会被当成普通消息发给模型，而模型会配合地
回一句「已帮你建好」——它没有那个工具，只是在说一件听起来对的话。
"""

from __future__ import annotations

import pytest

from app.services.chat_service import is_build_command


@pytest.mark.parametrize(
    "text",
    [
        "/build",
        "/build 压力高了先查规程再决定关不关阀",
        "/BUILD 大写也算",
        "  /build 前后空格  ",
        "/build\n换行后面接描述",
    ],
)
def test_build_command_is_recognized(text):
    assert is_build_command(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # 不是命令，是一个以 /build 开头的单词：不能因为前缀像就打断它。
        "/builder 是什么",
        "/buildall",
        # 位置不对：命令必须在开头。
        "请用 /build 建一个流程",
        # 没有斜杠，是正常提问。
        "build 一个流程",
        # `/workflow` 的第二个词是流程名，`build` 恰好是合法名字——这条走流程，不走这里。
        "/workflow build 处理一下",
        "",
    ],
)
def test_build_command_is_not_recognized(text):
    assert is_build_command(text) is False
