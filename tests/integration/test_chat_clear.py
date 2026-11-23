# tests/integration/test_chat_clear.py
"""`/clear` 的行为：历史留在原地，上下文归零。

两个断言方向必须都在：
- 消息表**没有少**（历史是审计，删了就再也答不出「他当时问过什么」）；
- 注入内容里**没有旧轮次**（这是 `/clear` 唯一真正的效果）。

只测前者会漏掉「什么都没做」这种实现——消息本来就没被删。
只测后者会漏掉「把历史删了」这种实现——注入当然也干净了。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.agents.schemas import ActionPlan
from app.db.session import async_session_factory
from app.models.context import ConversationSummary
from app.models.conversation import Conversation
from app.services import context_service
from app.tenancy.context import tenant_scope
from tests.conftest import TEST_TENANT


async def _conversation_id(client, thread_id: str) -> str:
    listing = (await client.get("/api/v1/conversations")).json()
    return next(i["id"] for i in listing["items"] if i["thread_id"] == thread_id)


async def test_clear_keeps_messages_and_records_watermark(client, planner):
    planner([ActionPlan(action="chat", response="第一轮回答。")])

    await client.post(
        "/api/v1/chat", json={"thread_id": "t-clear", "message": "第一轮问题"}
    )

    response = await client.post(
        "/api/v1/chat", json={"thread_id": "t-clear", "message": "/clear"}
    )
    body = response.json()

    assert body["status"] == "completed"
    # 不是一次运行：建了 run 会让观测页多出一条没有模型调用的轮次。
    assert body["run_id"] == ""
    assert body["metadata"]["context_cleared"] is True
    assert body["metadata"]["cleared_turns"] == 1
    assert "已清空" in body["answer"]

    detail = (
        await client.get(f"/api/v1/conversations/{await _conversation_id(client, 't-clear')}")
    ).json()

    # 一问一答 + `/clear` 与它的回复，一条都不能少。
    assert len(detail["messages"]) == 4
    assert detail["messages"][2]["content"] == "/clear"
    # 清空点指向最后那条回复：前端要插在它之后。
    assert detail["context_cleared"]["through_message_id"] == detail["messages"][3]["id"]


async def test_clear_stops_history_from_being_injected(client, planner):
    planner([ActionPlan(action="chat", response="第一轮回答。")])

    await client.post(
        "/api/v1/chat", json={"thread_id": "t-clear-inject", "message": "第一轮问题"}
    )

    conversation_id = uuid.UUID(await _conversation_id(client, "t-clear-inject"))

    # 对照组：清空之前这一轮必须看得见。少了这一步，「清空之后是空的」
    # 也可能只是因为准备流程根本没取到历史，用例证明不了任何事。
    with tenant_scope(TEST_TENANT):
        before = await context_service.prepare(
            conversation_id=conversation_id,
            user_message="新问题",
            measured_tokens=0,
        )
    assert before.turns
    assert "第一轮问题" in before.turns[0]["text"]

    await client.post(
        "/api/v1/chat", json={"thread_id": "t-clear-inject", "message": "/clear"}
    )

    async with async_session_factory() as session:
        row = await session.get(Conversation, conversation_id)
        cleared_at = row.context_cleared_at

    assert cleared_at is not None

    with tenant_scope(TEST_TENANT):
        # 不传水位：`prepare` 自己回库读。漏传过水位一次，旧轮次就原样回到了
        # 上下文里，而且不报错——这个静默分支必须堵在服务里，不能指望每个
        # 调用点都记得传。
        block = await context_service.prepare(
            conversation_id=conversation_id,
            user_message="新问题",
            measured_tokens=0,
        )

    assert block.turns == []
    assert block.summary == ""

    with tenant_scope(TEST_TENANT):
        cleared = await context_service.prepare(
            conversation_id=conversation_id,
            user_message="新问题",
            measured_tokens=0,
            cleared_at=cleared_at,
        )

    assert cleared.turns == []
    assert cleared.summary == ""


async def test_clear_invalidates_summaries_written_before_it(client, planner):
    """清空前的摘要不能再被注入。

    摘要表是 append-only 的（留着才能复盘「清空前它记得什么」），所以只能按
    生成时刻过滤。这一条一旦漏掉，表现是「消息不注入了，但模型还是知道
    上周那件事」——因为它从摘要里读到了。
    """
    planner([ActionPlan(action="chat", response="第一轮回答。")])

    await client.post(
        "/api/v1/chat", json={"thread_id": "t-clear-summary", "message": "第一轮问题"}
    )
    await client.post(
        "/api/v1/chat", json={"thread_id": "t-clear-summary", "message": "/clear"}
    )

    conversation_id = uuid.UUID(await _conversation_id(client, "t-clear-summary"))

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        cleared_at = conversation.context_cleared_at
        assert cleared_at is not None

        # 清空**之前**生成的摘要：模拟「早就压过一次」的会话。
        stale_at = cleared_at - timedelta(minutes=5)
        session.add(
            ConversationSummary(
                tenant_id=TEST_TENANT,
                conversation_id=conversation_id,
                through_message_id=uuid.uuid4(),
                through_created_at=stale_at,
                summary="清空前它记得：cs-smoke 改职责。",
                created_at=stale_at,
            )
        )
        await session.commit()

    with tenant_scope(TEST_TENANT):
        block = await context_service.prepare(
            conversation_id=conversation_id,
            user_message="新问题",
            measured_tokens=0,
            cleared_at=cleared_at,
        )

    assert block.summary == ""

    # 清空**之后**生成的摘要照常生效：过滤条件是时刻，不是「摘要一律作废」。
    async with async_session_factory() as session:
        session.add(
            ConversationSummary(
                tenant_id=TEST_TENANT,
                conversation_id=conversation_id,
                through_message_id=uuid.uuid4(),
                through_created_at=datetime.now(UTC),
                summary="清空后它记得：PartKeeper 凭据已换。",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()

    with tenant_scope(TEST_TENANT):
        fresh = await context_service.prepare(
            conversation_id=conversation_id,
            user_message="新问题",
            measured_tokens=0,
            cleared_at=cleared_at,
        )

    assert "PartKeeper" in fresh.summary


async def test_clear_twice_only_counts_what_is_visible(client, planner):
    """二次清空只数上一次清空之后的轮次。

    从头数的话，文案里的数字越清越大，而用户眼里那段早就看不见了——
    与压缩分隔条同一个坑：界面的「几轮」和库里的计数不是一回事。
    """
    planner(
        [
            ActionPlan(action="chat", response="一。"),
            ActionPlan(action="chat", response="二。"),
        ]
    )

    await client.post("/api/v1/chat", json={"thread_id": "t-clear-twice", "message": "问题一"})
    await client.post("/api/v1/chat", json={"thread_id": "t-clear-twice", "message": "/clear"})
    await client.post("/api/v1/chat", json={"thread_id": "t-clear-twice", "message": "问题二"})

    second = (
        await client.post(
            "/api/v1/chat", json={"thread_id": "t-clear-twice", "message": "/clear"}
        )
    ).json()

    # 上一次清空之后只有「问题二」这一轮；`/clear` 自己还没落库时不算。
    assert second["metadata"]["cleared_turns"] == 1


async def test_clear_on_a_fresh_thread_says_there_was_nothing(client):
    """空会话发 `/clear`：不报错，也不编一个轮数出来。

    「0 轮」与「清掉了 N 轮」是两件事。写成同一句会让用户以为刚才那几轮
    被清了——而它们其实从来没进过这个会话。
    """
    body = (
        await client.post(
            "/api/v1/chat", json={"thread_id": "t-clear-fresh", "message": "/clear"}
        )
    ).json()

    assert body["status"] == "completed"
    assert body["metadata"]["cleared_turns"] == 0
    assert "没有可清的内容" in body["answer"]
    assert "此前的 0 轮" not in body["answer"]


async def test_clear_command_only_matches_a_standalone_message(client):
    """`/clear` 必须独独占一条消息。

    前缀匹配会让 `/clearall`、`/cleared` 也清空会话——用户只是打错一个词，
    代价是整段上下文没了，而且没有任何提示。
    """
    from app.services.chat_service import is_clear_command

    assert is_clear_command("/clear")
    assert is_clear_command("  /clear  ")
    assert is_clear_command("/CLEAR")

    assert not is_clear_command("/clearall")
    assert not is_clear_command("/clear 一下这个会话")
    assert not is_clear_command("clear")
    assert not is_clear_command("")
    assert not is_clear_command("/workflow morning-patrol 巡检")
