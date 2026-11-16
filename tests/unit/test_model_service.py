# tests/unit/test_model_service.py
"""模型来源解析：供应商优先、环境变量兜底，以及「不跨供应商复用密钥」。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models.model_provider import ModelProvider
from app.services import model_service


async def _insert(**overrides) -> int:
    payload = {
        "name": f"p-{uuid.uuid4().hex[:8]}",
        "protocol": "openai-compatible",
        "api_key": "sk-provider",
        "base_url": "https://example.invalid",
        "models": [{"id": "m-1", "kind": "text"}],
        "default_text_model": "m-1",
        "temperature": 0.0,
        "reasoning_mode": "auto",
        "is_active": False,
    }
    payload.update(overrides)

    async with async_session_factory() as session:
        async with session.begin():
            row = ModelProvider(**payload)
            session.add(row)
            await session.flush()
            provider_id = row.id

    model_service.invalidate_model_cache()
    return provider_id


@pytest.fixture(autouse=True)
async def _clean_providers():
    model_service.invalidate_model_cache()
    yield
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(ModelProvider))
    model_service.invalidate_model_cache()


async def test_falls_back_to_env_without_provider(monkeypatch):
    monkeypatch.setattr(model_service.env, "openai_api_key", "sk-env")
    model_service.invalidate_model_cache()

    config = await model_service.resolve_text_model()

    assert config.source == "env"
    assert config.api_key == "sk-env"
    assert config.model == model_service.env.model_name


async def test_active_provider_wins(monkeypatch):
    monkeypatch.setattr(model_service.env, "openai_api_key", "sk-env")
    await _insert(is_active=True, api_key="sk-provider", default_text_model="m-1")

    config = await model_service.resolve_text_model()

    assert config.source == "provider"
    assert config.model == "m-1"
    assert config.api_key == "sk-provider"


async def test_provider_without_key_does_not_borrow_env_key(monkeypatch):
    """给第三方 base_url 配空密钥时，绝不能把 .env 的密钥发过去。"""
    monkeypatch.setattr(model_service.env, "openai_api_key", "sk-env")
    await _insert(is_active=True, api_key=None)

    config = await model_service.resolve_text_model()

    assert config.source == "provider"
    assert config.api_key is None
    assert config.configured is False


async def test_model_override_applies_to_provider():
    await _insert(is_active=True, default_text_model="m-1")

    config = await model_service.resolve_text_model(model_override="vision-model")

    assert config.model == "vision-model"


async def test_context_window_comes_from_matching_model_entry():
    """按模型声明的窗口必须被读出来——否则换模型后仍按旧窗口压缩，静默丢历史。"""
    await _insert(
        is_active=True,
        models=[
            {"id": "m-1", "kind": "text", "context_window": 32_000},
            {"id": "m-2", "kind": "text", "context_window": 128_000},
        ],
        default_text_model="m-2",
    )

    config = await model_service.resolve_text_model()

    assert config.model == "m-2"
    assert config.context_window == 128_000


async def test_context_window_absent_stays_none():
    """没填就是没填：不能在这里补一个默认值，那会让「未声明」与「声明了 32k」同形。"""
    await _insert(is_active=True)

    config = await model_service.resolve_text_model()

    assert config.context_window is None


@pytest.mark.parametrize("bad", ["abc", 0, 32, -1, 10**9, True, {"v": 128000}])
async def test_context_window_ignores_dirty_and_out_of_range_values(bad):
    """库里 JSON 可能被手改。脏值按未声明处理，不能抛异常也不能当成 0。

    参数化而不是循环：每条用例都要跑一遍清库 fixture，循环里堆多个
    is_active=True 的供应商会让「当前生效的是哪一条」变成运气。
    """
    await _insert(
        is_active=True,
        models=[{"id": "m-1", "kind": "text", "context_window": bad}],
        default_text_model="m-1",
    )

    config = await model_service.resolve_text_model()

    assert config.context_window is None


async def test_context_window_for_overridden_model_not_default():
    """指定了另一个模型，窗口也要跟着那个模型走，不能沿用默认模型的。"""
    await _insert(
        is_active=True,
        models=[
            {"id": "m-1", "kind": "text", "context_window": 32_000},
            {"id": "vision", "kind": "text", "context_window": 200_000},
        ],
        default_text_model="m-1",
    )

    config = await model_service.resolve_text_model(model_override="vision")

    assert config.context_window == 200_000


async def test_reasoning_effort_only_sent_when_explicit():
    base = {
        "model": "m-1",
        "api_key": "sk-x",
        "base_url": None,
        "protocol": "openai-compatible",
        "temperature": 0.0,
    }

    auto = model_service.build_chat_model(
        model_service.TextModelConfig(source="provider", provider_name="p", reasoning_mode="auto", **base)
    )
    assert getattr(auto, "reasoning_effort", None) is None

    high = model_service.build_chat_model(
        model_service.TextModelConfig(source="provider", provider_name="p", reasoning_mode="high", **base)
    )
    assert high.reasoning_effort == "high"
