# tests/integration/test_context_compaction.py
"""长对话上下文：注入、压缩、降级、开关。

走 HTTP 而不是直接调 service：需要验证的是「模型这一轮到底看到了什么」。
断言落在假规划器收到的 payload 上——那是模型真实的输入，比查库更接近事实。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.agents.planner import reset_planner, set_planner_for_tests
from app.agents.schemas import ActionPlan
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.context import ConversationSummary
from app.models.conversation import Conversation
from app.services import context_service

SUMMARY_TEXT = (
    "【当前目标】跟进 3 号工单的关闭进度\n"
    "【已定结论】用户要求先不改状态（用户）\n"
    "【未完成】等用户确认站点 id\n"
    "【指代绑定】「那个」→ 3 号工单\n"
    "【被否决】无"
)


class _CapturingPlanner:
    """记下每次送给模型的 payload（含历史注入），始终回答一句闲聊。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def ainvoke(self, messages):  # noqa: ANN001
        for message in messages:
            if isinstance(message, tuple) and message[0] == "human":
                self.payloads.append(json.loads(message[1]))
        return ActionPlan(action="chat", response="好的。")


class _FailingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, messages):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("摘要模型超时")


class _RecordingSummarizer:
    def __init__(self, text: str = SUMMARY_TEXT) -> None:
        self.text = text
        self.calls: list[list[tuple[str, str]]] = []

    async def __call__(self, messages):  # noqa: ANN001
        self.calls.append(messages)
        return self.text, "fake-summary-model"

    @property
    def transcripts(self) -> list[str]:
        return [item[1] for call in self.calls for item in call if item[0] == "human"]


@pytest.fixture
def capturing_planner():
    planner = _CapturingPlanner()
    set_planner_for_tests(planner)
    yield planner
    reset_planner()


@pytest.fixture
def summarizer():
    recorder = _RecordingSummarizer()
    context_service.set_summarizer_for_tests(recorder)
    yield recorder
    context_service.set_summarizer_for_tests(None)


@pytest.fixture
def small_window(monkeypatch):
    """把保留预算压到 600 token、淘汰阈值压到 150 token。

    真实预算是 6000 / 800——要触发淘汰得写几万字对话，测试会慢到没人愿意跑。
    切边界与压缩逻辑与数值无关，这里只验证「该淘汰时淘汰」。
    """
    monkeypatch.setattr(context_service, "KEEP_TOKENS", 600)
    monkeypatch.setattr(context_service, "MIN_EVICT_TOKENS", 150)


async def _say(client, thread_id: str, message: str) -> dict:
    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": message},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _conversation_id(client, thread_id: str) -> uuid.UUID:
    listing = await client.get("/api/v1/conversations?limit=100")
    for item in listing.json()["items"]:
        if item["thread_id"] == thread_id:
            return uuid.UUID(item["id"])
    raise AssertionError(f"会话 {thread_id} 不在列表里")


async def _set_measured(conversation_id: uuid.UUID, tokens: int) -> None:
    """直接写「上一轮实测 token」：假规划器不产生真实用量，只能从这里造压力。"""
    async with async_session_factory() as session:
        async with session.begin():
            conversation = await session.get(Conversation, conversation_id)
            conversation.last_prompt_tokens = tokens


async def _summaries(conversation_id: uuid.UUID) -> list[ConversationSummary]:
    from sqlalchemy import select  # 局部导入：只有这个文件需要

    async with async_session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(ConversationSummary).where(
                        ConversationSummary.conversation_id == conversation_id
                    )
                )
            ).all()
        )


async def test_history_is_injected_with_roles(client, capturing_planner):
    thread_id = f"ctx-history-{uuid.uuid4().hex[:8]}"

    await _say(client, thread_id, "把 3 号工单状态改成已关闭")
    await _say(client, thread_id, "刚才那个是哪张工单？")

    payload = capturing_planner.payloads[-1]
    turns = payload["recent_turns"]

    assert turns == [
        {"role": "user", "text": "把 3 号工单状态改成已关闭"},
        {"role": "assistant", "text": "好的。"},
    ], "历史必须逐条带角色，拼成一段会让助手的话被当成用户说的"
    assert "conversation_summary" not in payload, "还没压过，不该凭空给一份摘要"
    assert payload["user_message"] == "刚才那个是哪张工单？"


async def test_compaction_happens_between_turns_and_skips_current_message(
    client, capturing_planner, summarizer, small_window
):
    thread_id = f"ctx-compact-{uuid.uuid4().hex[:8]}"

    for index in range(8):
        await _say(client, thread_id, f"第{index}轮：继续跟进 3 号工单的关闭进度" + "细节" * 60)

    conversation_id = await _conversation_id(client, thread_id)
    await _set_measured(conversation_id, 20_000)  # 0.625 × 32000 → 触发压缩

    current = "第 9 轮：现在几点了？"
    await _say(client, thread_id, current)

    rows = await _summaries(conversation_id)
    assert len(rows) == 1, "一轮只允许压一次"
    row = rows[0]
    assert row.summary == SUMMARY_TEXT
    assert row.degraded is False
    assert row.tokens_after < row.tokens_before
    assert row.turns_covered >= 2

    transcript = summarizer.transcripts[0]
    assert "第0轮" in transcript, "被淘汰的必须是最旧的"
    assert "第7轮" not in transcript, "最近的轮次永远原样保留，不进摘要"
    assert current not in transcript, "当前这条用户消息永不该进摘要"

    payload = capturing_planner.payloads[-1]
    assert payload["conversation_summary"] == SUMMARY_TEXT
    assert len(payload["recent_turns"]) == 12, "保留最近 6 轮，逐条展开"
    assert payload["recent_turns"][-2]["text"] == "第7轮：继续跟进 3 号工单的关闭进度" + "细节" * 60
    assert payload["user_message"] == current


async def test_summary_failure_keeps_the_turn_alive(client, capturing_planner, small_window):
    failing = _FailingSummarizer()
    context_service.set_summarizer_for_tests(failing)
    try:
        thread_id = f"ctx-degrade-{uuid.uuid4().hex[:8]}"
        for index in range(8):
            await _say(client, thread_id, f"第{index}轮：" + "细节" * 60)

        conversation_id = await _conversation_id(client, thread_id)
        await _set_measured(conversation_id, 20_000)

        answer = await _say(client, thread_id, "还在吗？")
        assert answer["answer"] == "好的。", "摘要写不出来不该让用户收不到答案"

        assert await _summaries(conversation_id) == [], "失败了不能落一行假摘要把水位推走"

        payload = capturing_planner.payloads[-1]
        assert "conversation_summary" not in payload
        # 窗口缩到硬截断预算：比正常保留的 6 轮少，但不能少于 2 轮。
        assert 0 < len(payload["recent_turns"]) < 12
        assert failing.calls == 1, "同一轮只试一次，不许重试到超时"
    finally:
        context_service.set_summarizer_for_tests(None)


async def test_disabled_context_injects_nothing(client, capturing_planner, monkeypatch):
    monkeypatch.setattr(settings, "context_enabled", False)
    thread_id = f"ctx-off-{uuid.uuid4().hex[:8]}"

    await _say(client, thread_id, "第一句")
    await _say(client, thread_id, "第二句")

    payload = capturing_planner.payloads[-1]
    assert "recent_turns" not in payload
    assert "conversation_summary" not in payload
    assert payload["user_message"] == "第二句"


async def test_stale_summary_is_not_written(client, capturing_planner, session):
    """两轮并发压缩时，后到的那个必须放手：水位已被推走，再写就是把同一段压两遍。"""
    thread_id = f"ctx-race-{uuid.uuid4().hex[:8]}"
    await _say(client, thread_id, "第一句")
    conversation_id = await _conversation_id(client, thread_id)

    draft = context_service.SummaryDraft(
        body="【当前目标】测试",
        model="fake",
        tokens_before=10_000,
        tokens_after=100,
        turns_covered=2,
        messages_covered=4,
        through_message_id=uuid.uuid4(),
        through_created_at=datetime.now(UTC),
        rebuilt=False,
        gain=0.99,
    )

    first = await context_service._persist(
        draft=draft, conversation_id=conversation_id, base_summary_id=None
    )
    assert first is not None

    second = await context_service._persist(
        draft=draft, conversation_id=conversation_id, base_summary_id=None
    )
    assert second is None, "基线已经变了，第二个必须放弃写入"


async def test_prompt_tokens_are_persisted_for_next_turn(client, capturing_planner):
    thread_id = f"ctx-tokens-{uuid.uuid4().hex[:8]}"
    await _say(client, thread_id, "随便一句")
    conversation_id = await _conversation_id(client, thread_id)

    await context_service.remember_prompt_tokens(
        conversation_id=conversation_id, prompt_tokens=12_345
    )

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.last_prompt_tokens == 12_345


async def test_eviction_alone_triggers_compaction(client, capturing_planner, summarizer, small_window):
    """没有「接近上限」的压力，但这一轮要挤掉不少旧轮次时，也要压缩。

    线上真实情形：基线 prompt 约 12.4k（工具目录占大头），窗口只装 6000 的历史，
    两者相加到不了 19200 的压缩线。若只按总占比触发，旧轮次会每轮被悄悄挤掉，
    摘要永远不生成——模型不是「忘得慢」，是「忘得无声」。
    """
    thread_id = f"ctx-evict-{uuid.uuid4().hex[:8]}"

    for index in range(8):
        await _say(client, thread_id, f"第{index}轮：继续跟进 3 号工单" + "细节" * 60)

    conversation_id = await _conversation_id(client, thread_id)
    # 故意不设 last_prompt_tokens：模拟「总占比没到线」的真实情形。
    await _say(client, thread_id, "接着上次的说")

    rows = await _summaries(conversation_id)
    assert len(rows) == 1, "挤掉够多旧轮次就该压，不该等总占比到线"
    assert rows[0].turns_covered >= 1
    assert summarizer.transcripts, "不仅要落库，还要真的问过模型"
