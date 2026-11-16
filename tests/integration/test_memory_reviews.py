# tests/integration/test_memory_reviews.py
"""梦境的端到端闭环：触发 → 列表 → 应用，以及跨用户隔离。

这一轮的核心风险不是「模型判得准不准」，而是「会不会改到不该改的记忆」。
所以最要紧的回归是 `test_confirmed_memory_is_never_reviewed` 与
`test_other_user_cannot_apply_review`。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.conversation import Conversation, Message, MessageRole
from app.models.memory import Memory, MemoryReview, MemoryType, TenantMemorySettings
from app.services import memory_dream, memory_service
from app.services.model_service import TextModelConfig
from app.tenancy.context import tenant_scope

TENANT = "default"


def _config() -> TextModelConfig:
    return TextModelConfig(
        source="env",
        provider_name="test",
        protocol="openai-compatible",
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=0.0,
        reasoning_mode="auto",
    )


@pytest.fixture(autouse=True)
def _stub_model(monkeypatch):
    async def fake_resolve(model_override: str | None = None) -> TextModelConfig:
        return _config()

    monkeypatch.setattr(memory_dream, "resolve_text_model", fake_resolve)
    yield
    memory_dream.set_caller_for_tests(None)


@pytest.fixture(autouse=True)
async def _clean_db(session):
    # 配置表也要清：它按租户存一行，跨用例留着会让「默认值」断言随机失败。
    await session.execute(delete(TenantMemorySettings))
    await session.execute(delete(MemoryReview))
    await session.execute(delete(Message))
    await session.execute(delete(Conversation))
    await session.execute(delete(Memory))
    await session.commit()
    yield
    await session.execute(delete(TenantMemorySettings))
    await session.execute(delete(MemoryReview))
    await session.execute(delete(Message))
    await session.execute(delete(Conversation))
    await session.execute(delete(Memory))
    await session.commit()


def _reply(**overrides: object) -> str:
    payload = {
        "action": "correct",
        "reason": "用户原话是简洁",
        "title": "回答风格",
        "content": "偏好简洁的回答",
        "merge_target_id": "",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


async def _seed(user_id: str, *, source: str = "auto_extract", confirmed: bool = False):
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                memory, _ = await memory_service.create_memory(
                    session,
                    user_id=user_id,
                    memory_type=MemoryType.PREFERENCE,
                    title="回答风格",
                    content="用户偏好啰嗦的回答",
                    source=source,
                    confidence=0.8,
                    is_user_confirmed=confirmed,
                    source_thread_id="thread-review",
                )
                conversation = Conversation(
                    user_id=user_id, thread_id="thread-review", title="review"
                )
                session.add(conversation)
                await session.flush()
                session.add(
                    Message(
                        conversation_id=conversation.id,
                        role=MessageRole.USER,
                        content="我不喜欢太长的回答，简洁一点",
                        created_at=datetime.now(UTC),
                    )
                )
                return memory.id


async def _memory(memory_id: uuid.UUID) -> Memory:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            row = await session.get(Memory, memory_id)
            assert row is not None
            return row


async def test_dream_list_apply_round_trip(client, seeded_identity, monkeypatch):
    memory_id = await _seed(seeded_identity["user_id"])

    async def caller(system: str, human: str) -> str:
        return _reply()

    memory_dream.set_caller_for_tests(caller)

    dream = await client.post("/api/v1/memories/dream", json={"days": 30, "limit": 5})
    assert dream.status_code == 200
    assert dream.json()["suggested"] == 1

    listing = await client.get("/api/v1/memories/reviews?status=pending")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["memory_id"] == str(memory_id)
    assert items[0]["action"] == "correct"
    assert items[0]["can_apply"] is True
    assert items[0]["evidence"]

    applied = await client.post(f"/api/v1/memories/reviews/{items[0]['id']}/apply")
    assert applied.status_code == 200
    assert applied.json()["content"] == "偏好简洁的回答"
    # 应用建议不等于确认记忆。
    assert applied.json()["is_user_confirmed"] is False

    memory = await _memory(memory_id)
    assert memory.content == "偏好简洁的回答"

    # 已处理的建议不再出现在梦境建议列表。
    listing = await client.get("/api/v1/memories/reviews?status=pending")
    assert listing.json()["items"] == []


async def test_confirmed_memory_is_never_reviewed(client, seeded_identity, monkeypatch):
    memory_id = await _seed(seeded_identity["user_id"], source="user", confirmed=True)

    async def caller(system: str, human: str) -> str:
        return _reply()

    memory_dream.set_caller_for_tests(caller)

    dream = await client.post("/api/v1/memories/dream", json={"limit": 5})
    assert dream.status_code == 200
    assert dream.json()["scanned"] == 0

    memory = await _memory(memory_id)
    # 确认过的记忆，模型连看都不该看到。
    assert memory.content == "用户偏好啰嗦的回答"


async def test_other_user_cannot_apply_review(client, other_client, seeded_identity):
    memory_id = await _seed(seeded_identity["user_id"])

    async def caller(system: str, human: str) -> str:
        return _reply()

    memory_dream.set_caller_for_tests(caller)

    dream = await client.post("/api/v1/memories/dream", json={"limit": 5})
    assert dream.json()["suggested"] == 1
    review_id = (await client.get("/api/v1/memories/reviews")).json()["items"][0]["id"]

    # 另一个租户的用户看不到、也应用不了：不存在和没权限返回同一个 404。
    listing = await other_client.get("/api/v1/memories/reviews")
    assert listing.json()["items"] == []

    applied = await other_client.post(f"/api/v1/memories/reviews/{review_id}/apply")
    assert applied.status_code == 404

    memory = await _memory(memory_id)
    assert memory.content == "用户偏好啰嗦的回答"


async def test_dream_disabled_returns_400(client, seeded_identity, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "memory_dream_enabled", False)
    response = await client.post("/api/v1/memories/dream", json={"limit": 5})
    assert response.status_code == 400


async def test_extraction_stats_shape(client, seeded_identity):
    await _seed(seeded_identity["user_id"])

    response = await client.get("/api/v1/memories/extraction-stats?days=7")
    assert response.status_code == 200
    body = response.json()
    assert body["auto_total"] == 1
    assert body["auto_pending"] == 1
    assert body["confirmation_rate"] == 0.0
    assert "分母" in body["note"]


# ---------------- 梦境配置 ----------------
#
# 注意租户归属：`client` 是 default 租户的普通成员，`admin_client` 是 platform 租户
# 的平台管理员。PUT 写的是**调用者自己所在租户**的配置，所以「管理员改配置」这条
# 只能验证权限与往返，不能拿它去改 default 租户的设置。
# 需要 default 租户有配置时，直接落库（`_set_config`），别硬凑身份。


async def _set_config(**overrides):
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            await memory_dream.save_dream_config(
                session,
                tenant_id=TENANT,
                enabled=overrides.get("enabled"),
                days=overrides.get("days", 0),
                max_per_run=overrides.get("max_per_run", 0),
                actor_user_id="seed-actor",
            )
            await session.commit()


async def test_member_can_read_config_but_not_edit(client, seeded_identity):
    """读得到，才解释得清「开始梦境」按钮为什么是灰的。"""
    read = await client.get("/api/v1/memories/dream/config")
    assert read.status_code == 200

    body = read.json()
    assert body["can_edit"] is False
    assert body["enabled"] is True
    assert body["tenant_enabled"] is None
    assert body["tenant_days"] == 0
    assert body["disabled_reason"] is None

    write = await client.put("/api/v1/memories/dream/config", json={"enabled": False})
    assert write.status_code == 403


async def test_admin_can_update_config_and_read_it_back(admin_client, seeded_identity):
    updated = await admin_client.put(
        "/api/v1/memories/dream/config",
        json={"enabled": True, "days": 7, "max_per_run": 3},
    )
    assert updated.status_code == 200

    body = updated.json()
    assert body["can_edit"] is True
    assert body["days"] == 7
    assert body["max_per_run"] == 3
    assert body["tenant_days"] == 7
    assert body["tenant_max_per_run"] == 3

    read = await admin_client.get("/api/v1/memories/dream/config")
    assert read.json()["days"] == 7


async def test_admin_can_reset_to_platform_default(admin_client, seeded_identity):
    """清空输入框 = 跟随平台。必须真的回到 0，而不是把当前默认值写死。"""
    from app.core.config import settings

    await admin_client.put("/api/v1/memories/dream/config", json={"days": 7, "max_per_run": 3})
    reset = await admin_client.put(
        "/api/v1/memories/dream/config", json={"days": 0, "max_per_run": 0}
    )

    body = reset.json()
    assert body["tenant_days"] == 0
    assert body["tenant_max_per_run"] == 0
    assert body["days"] == settings.memory_dream_days
    assert body["max_per_run"] == settings.memory_dream_max_per_run


async def test_config_out_of_range_is_rejected(admin_client, seeded_identity):
    response = await admin_client.put("/api/v1/memories/dream/config", json={"days": 999})
    assert response.status_code == 422


async def test_dream_uses_configured_days_and_clamps_limit(client, seeded_identity, monkeypatch):
    """配置要真的落到调用参数上，否则界面上那两个输入框就是装饰。"""
    await _set_config(days=7, max_per_run=2)

    captured: dict[str, object] = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return memory_dream.DreamReport()

    monkeypatch.setattr(memory_dream, "run_dreaming", fake_run)

    response = await client.post("/api/v1/memories/dream", json={"limit": 50})
    assert response.status_code == 200
    assert captured["days"] == 7
    assert captured["limit"] == 2


async def test_tenant_disabled_blocks_dream(client, seeded_identity):
    await _set_config(enabled=False)

    response = await client.post("/api/v1/memories/dream", json={"limit": 5})
    assert response.status_code == 400
    assert "本租户已关闭" in response.json()["detail"]
