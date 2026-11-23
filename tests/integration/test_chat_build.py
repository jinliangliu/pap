# tests/integration/test_chat_build.py
"""`/build` 打到接口上会被拒，而且拒绝要「什么都没发生」。

为什么不能放给模型：它不是 `/workflow`，会当成普通消息走完全程——建会话、
耗配额、调模型，最后得到一句「已帮你把流程建好」。它没有那个工具，那句话是编的。

所以这里断言两件事：409 带明确的 code，以及库里没有为这条消息留下会话或消息行。
只断言前者的话，一个「先落库再拒绝」的实现也能通过。
"""

from __future__ import annotations


async def _threads(client) -> list[str]:
    listing = (await client.get("/api/v1/conversations")).json()
    return [item["thread_id"] for item in listing["items"]]


async def test_build_is_rejected_with_a_handoff_hint(client):
    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "t-build", "message": "/build 压力高了先查规程，再决定关不关阀"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "CHAT_BUILD_HANDOFF"
    # 说清「这条消息没有发给任何人」——否则用户以为它已经转过去了。
    assert "构建助手" in detail["message"]
    assert "没有" in detail["message"]

    # 会话是惰性创建的：没有这一条就说明连会话都没建。
    assert "t-build" not in await _threads(client)


async def test_build_is_rejected_before_the_agent_is_resolved(client):
    # 即使指定了一个不存在的智能体，仍然是 409 而不是 404：这条消息在解析边界
    # 之前就被拦下了。顺序反了的话，一次 `/build` 会先去建/查智能体，再拒绝。
    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "t-build-2",
            "message": "/build 建一个巡检流程",
            "agent": "no-such-agent",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CHAT_BUILD_HANDOFF"
    assert "t-build-2" not in await _threads(client)
