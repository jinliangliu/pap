# tests/unit/test_context_approval_turns.py
"""审批卡体不能与执行结论拼进同一轮。

真实事故（2026-11-23，线上会话 48c60adc）：同一次导入落了三条助手消息——
卡体「待确认……该操作尚未执行」、结论「已导入知识库……58 片」，
折成一轮后模型看到的是「没执行」和「已导入」并存，随后就容易照这个样式
声称执行过。这里把折叠规则钉住：同一条 approval_id 上只有带
`approval_action` 的那条算结论，它之前的卡体丢弃。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.conversation import Message, MessageRole
from app.services import context_service as ctx

_CONV = uuid.uuid4()
_BASE = datetime(2026, 11, 23, 5, 19, tzinfo=UTC)


def _msg(role: MessageRole, content: str, *, index: int, meta: dict | None = None) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=_CONV,
        tenant_id="default",
        role=role,
        content=content,
        metadata_json=meta or {},
        created_at=_BASE + timedelta(seconds=index),
    )


class _Scalars:
    def __init__(self, rows: list[Message]) -> None:
        self._rows = rows

    def all(self) -> list[Message]:
        return list(self._rows)


class _Session:
    """只实现 `_load_turns` 用到的那一条路径：scalars(...).all()。"""

    def __init__(self, rows: list[Message]) -> None:
        self._rows = rows

    async def scalars(self, _stmt) -> _Scalars:
        return _Scalars(self._rows)


async def _turns(rows: list[Message]) -> list[ctx.Turn]:
    return await ctx._load_turns(_Session(rows), conversation_id=_CONV, exclude_message_id=None)


async def test_pending_card_is_dropped_when_result_lands():
    approval = "aec83c30-d26d-4ab7-9dab-d8d0dfe3f189"
    rows = [
        _msg(MessageRole.USER, "将这份标准入库", index=0),
        _msg(
            MessageRole.ASSISTANT,
            "待确认操作：从附件导入\n\n该操作尚未执行。请确认或取消本次操作。",
            index=1,
            meta={"intent": "write_tool", "tool_name": "studio_import", "approval_id": approval},
        ),
        _msg(
            MessageRole.ASSISTANT,
            "已导入知识库《CJ/T 335-2010》：58 片 · 分级 internal。",
            index=2,
            meta={"intent": "write_tool", "approval_id": approval, "approval_action": "confirm"},
        ),
    ]

    turns = await _turns(rows)

    assert len(turns) == 1
    assert "尚未执行" not in turns[0].assistant_text
    assert "已导入" in turns[0].assistant_text
    # 卡体被丢掉，剩下的只有结论这一条。
    assert turns[0].messages == 2


async def test_lone_pending_card_stays():
    """还没点确认：历史里就该停在这句，不能删。"""
    rows = [
        _msg(MessageRole.USER, "将这份标准入库", index=0),
        _msg(
            MessageRole.ASSISTANT,
            "待确认操作：从附件导入\n\n该操作尚未执行。请确认或取消本次操作。",
            index=1,
            meta={"intent": "write_tool", "tool_name": "studio_import", "approval_id": "a1"},
        ),
    ]

    turns = await _turns(rows)

    assert len(turns) == 1
    assert "尚未执行" in turns[0].assistant_text


async def test_workflow_tail_after_result_is_kept():
    """审批恢复后接着跑完的流程尾巴与结论同 approval_id，但它不是卡体。"""
    approval = "c770dc68-747e-4f27-9f38-817fea35425b"
    rows = [
        _msg(MessageRole.USER, "确认执行", index=0),
        _msg(
            MessageRole.ASSISTANT,
            "待确认操作：导入附件\n\n该操作尚未执行。请确认或取消本次操作。",
            index=1,
            meta={"intent": "write_tool", "tool_name": "studio_import", "approval_id": approval},
        ),
        _msg(
            MessageRole.ASSISTANT,
            "已导入：62 片。",
            index=2,
            meta={"intent": "write_tool", "approval_id": approval, "approval_action": "confirm"},
        ),
        _msg(
            MessageRole.ASSISTANT,
            "流程已继续：report 节点完成。",
            index=3,
            meta={"intent": "workflow", "approval_id": approval},
        ),
    ]

    turns = await _turns(rows)
    text = turns[0].assistant_text

    assert "尚未执行" not in text
    assert "已导入：62 片" in text
    assert "流程已继续" in text


async def test_plain_assistant_messages_still_concatenate():
    """回归：没有 approval_id 的多条助手消息照旧按顺序拼。"""
    rows = [
        _msg(MessageRole.USER, "查一下压力", index=0),
        _msg(MessageRole.ASSISTANT, "第一段", index=1, meta={"intent": "chat"}),
        _msg(MessageRole.ASSISTANT, "第二段", index=2, meta={"intent": "chat"}),
    ]

    turns = await _turns(rows)

    assert turns[0].assistant_text == "第一段\n第二段"
    assert turns[0].messages == 3


async def test_two_different_approvals_are_both_kept():
    """两张审批单各自收口，互不影响。"""
    rows = [
        _msg(MessageRole.USER, "导入两份", index=0),
        _msg(MessageRole.ASSISTANT, "待确认 A…尚未执行。", index=1, meta={"intent": "write_tool", "approval_id": "a"}),
        _msg(MessageRole.ASSISTANT, "A 已导入。", index=2, meta={"intent": "write_tool", "approval_id": "a", "approval_action": "confirm"}),
        _msg(MessageRole.ASSISTANT, "待确认 B…尚未执行。", index=3, meta={"intent": "write_tool", "approval_id": "b"}),
        _msg(MessageRole.ASSISTANT, "B 已导入。", index=4, meta={"intent": "write_tool", "approval_id": "b", "approval_action": "confirm"}),
    ]

    text = (await _turns(rows))[0].assistant_text

    assert "尚未执行" not in text
    assert "A 已导入。" in text
    assert "B 已导入。" in text
