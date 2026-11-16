# tests/integration/test_artifacts_api.py
"""产物列表的会话过滤（P3-c 的读路径）。

「按会话保留」有两半：写的时候绑会话（`save_artifact`），读的时候按会话收敛。
这个文件测的是后一半——而它最容易漏，因为不过滤时一切看起来都正常，
只是「上一场对话的文件也会混进来」，没人会为此报 bug。
"""

from __future__ import annotations

from sqlalchemy import delete

from app.models.artifact import Artifact


async def _upload(client, *, filename: str, conversation_id: str | None) -> str:
    params = {"filename": filename}
    if conversation_id is not None:
        params["conversation_id"] = conversation_id

    response = await client.put(
        "/api/v1/artifacts",
        params=params,
        content=f"{filename} 的内容".encode(),
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 200
    return response.json()["artifact_id"]


async def test_listing_filters_by_conversation(client, session, seeded_identity):
    await session.execute(delete(Artifact))
    await session.commit()

    a = await _upload(client, filename="本周.csv", conversation_id="conv-a")
    b = await _upload(client, filename="上周.csv", conversation_id="conv-b")
    # 历史上传的通用材料：上传时没绑会话。
    loose = await _upload(client, filename="规程.pdf", conversation_id=None)

    only_a = await client.get("/api/v1/artifacts", params={"conversation_id": "conv-a"})
    assert only_a.status_code == 200
    assert [item["artifact_id"] for item in only_a.json()] == [a]

    every = await client.get("/api/v1/artifacts")
    assert every.status_code == 200
    # 不传参数 = 「我手里有哪些文件」，包括没绑会话的那些。
    assert {item["artifact_id"] for item in every.json()} == {a, b, loose}


async def test_filtered_listing_is_empty_for_a_new_conversation(client, session, seeded_identity):
    """新会话看到的是空表，不是「上一场的东西」。

    这条是「换话题后仍能引用刚才那张表」的反面：跨会话不该串。
    """
    await session.execute(delete(Artifact))
    await session.commit()

    await _upload(client, filename="a.csv", conversation_id="conv-a")

    fresh = await client.get("/api/v1/artifacts", params={"conversation_id": "conv-new"})
    assert fresh.status_code == 200
    assert fresh.json() == []
