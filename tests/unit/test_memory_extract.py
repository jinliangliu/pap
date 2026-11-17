# tests/unit/test_memory_extract.py
"""记忆自动抽取：解析、写库规则、开关。

三条最该守住的东西：

1. 模型输出不合法时不能炸。这轮白跑可以，对话不能受影响；
2. 用户自己写或确认过的记忆不能被模型改写——改口是人的判断；
3. 关掉开关、或一句话太短时，不产生任何模型调用。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.memory import Memory, MemoryType
from app.services import memory_extract, memory_service
from app.tenancy.context import tenant_scope

TENANT = "default"
USER = "extract-user"


def _payload(*items: dict) -> str:
    return json.dumps({"memories": list(items)}, ensure_ascii=False)


def _item(
    *,
    type_: str = "profile",
    title: str = "称呼",
    content: str = "希望被称呼为 dylan",
    confidence: object = 0.9,
) -> dict:
    return {"type": type_, "title": title, "content": content, "confidence": confidence}


def _fake_reply(raw: str) -> list[list[str]]:
    """装上假的模型调用，并把每次请求记下来（用来断言「没被调用」）。"""
    calls: list[list[str]] = []

    async def caller(system: str, human: str) -> str:
        calls.append([system, human])
        return raw

    memory_extract.set_caller_for_tests(caller)
    return calls


@pytest.fixture(autouse=True)
async def _clean_db(session):
    await session.execute(delete(Memory))
    await session.commit()
    yield
    memory_extract.set_caller_for_tests(None)
    await session.execute(delete(Memory))
    await session.commit()


async def _seed_user_memory(
    *,
    title: str,
    content: str,
    source: str = "user",
    memory_type: MemoryType = MemoryType.PROFILE,
) -> Memory:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                row, _ = await memory_service.create_memory(
                    session,
                    user_id=USER,
                    memory_type=memory_type,
                    title=title,
                    content=content,
                    source=source,
                    is_user_confirmed=(source == "user"),
                )
                return row


# ---------------------------------------------------------------- 解析


def test_parse_plain_object():
    rows = memory_extract.parse_candidates(
        _payload(_item(confidence=0.95))
    )
    assert len(rows) == 1
    assert rows[0].memory_type is MemoryType.PROFILE
    assert rows[0].title == "称呼"
    assert rows[0].confidence == pytest.approx(0.95)


def test_parse_fenced_block():
    raw = "```json\n" + _payload(_item(type_="preference", title="回答风格")) + "\n```"
    rows = memory_extract.parse_candidates(raw)
    assert [r.title for r in rows] == ["回答风格"]


def test_parse_bare_list():
    rows = memory_extract.parse_candidates(json.dumps([_item()], ensure_ascii=False))
    assert len(rows) == 1


def test_parse_accepts_chinese_type_and_string_confidence():
    rows = memory_extract.parse_candidates(
        _payload(_item(type_="偏好", confidence="0.8"))
    )
    assert rows[0].memory_type is MemoryType.PREFERENCE
    assert rows[0].confidence == pytest.approx(0.8)


def test_parse_unknown_type_falls_back_to_fact():
    # 类型写错不值得丢掉一条真信息。
    rows = memory_extract.parse_candidates(_payload(_item(type_="随便写的")))
    assert rows[0].memory_type is MemoryType.FACT


def test_parse_drops_low_confidence_and_empty_fields():
    rows = memory_extract.parse_candidates(
        _payload(
            _item(title="猜测", confidence=0.3),
            _item(title="", content="没有标题"),
            _item(title="空内容", content="   "),
            _item(title="保留", confidence=0.7),
        )
    )
    assert [r.title for r in rows] == ["保留"]


def test_parse_caps_items_and_dedupes():
    rows = memory_extract.parse_candidates(
        _payload(
            _item(title="称呼"),
            _item(title="称呼"),          # 同名去重
            _item(title="回答风格"),
            _item(title="观测方案"),
            _item(title="第四条"),
        )
    )
    assert len(rows) == memory_extract.MAX_ITEMS


def test_parse_garbage_returns_empty():
    # 模型抽风、返回半截 JSON、返回自然语言，都只能算「这轮没抽到」。
    for raw in ("模型今天不太想说话", "{\"memories\": [", "", "null"):
        assert memory_extract.parse_candidates(raw) == []


# ---------------------------------------------------------------- 写库


async def test_extraction_writes_unconfirmed_candidate(monkeypatch):
    monkeypatch.setattr(settings, "memory_enabled", True)
    monkeypatch.setattr(settings, "memory_extract_enabled", True)
    _fake_reply(_payload(_item(content="希望被称呼为 dylan，不要用「您」。")))

    stats = await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-1",
        user_message="以后别用「您」，叫我 dylan 就行",
        answer="好的 dylan。",
    )

    assert stats == {"candidates": 1, "created": 1, "refreshed": 0, "skipped": 0}

    async with async_session_factory() as session:
        row = await session.scalar(select(Memory).where(Memory.user_id == USER))
    assert row is not None
    assert row.source == "auto_extract"
    # 抽出来的是候选，不是「用户的设定」：页面上要能看到待确认。
    assert row.is_user_confirmed is False
    assert row.source_thread_id == "thread-1"
    assert row.tenant_id == TENANT
    assert row.metadata_json.get("extracted") is True


async def test_extraction_never_overwrites_user_memory(monkeypatch):
    """模型推断出的话不能替换用户的原话。跳过，交给人在页面上改。"""
    monkeypatch.setattr(settings, "memory_extract_enabled", True)
    await _seed_user_memory(title="称呼", content="用户自己写的：叫我老张")

    _fake_reply(_payload(_item(content="模型认为他叫 dylan")))
    stats = await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-2",
        user_message="我们继续看观测那条链路吧",
        answer="好的。",
    )

    assert stats["skipped"] == 1
    async with async_session_factory() as session:
        rows = list((await session.scalars(select(Memory))).all())
    assert len(rows) == 1
    assert rows[0].content == "用户自己写的：叫我老张"
    assert rows[0].is_user_confirmed is True


async def test_extraction_refreshes_previous_candidate(monkeypatch):
    """同一条自动候选说了新说法：覆盖内容，而不是多出一条。"""
    monkeypatch.setattr(settings, "memory_extract_enabled", True)
    await _seed_user_memory(
        title="回答风格",
        content="简洁一点",
        source="auto_extract",
        memory_type=MemoryType.PREFERENCE,
    )

    _fake_reply(_payload(_item(type_="preference", title="回答风格", content="先结论后依据")))
    stats = await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-3",
        user_message="以后先给结论，再讲依据",
        answer="明白。",
    )

    assert stats["refreshed"] == 1
    async with async_session_factory() as session:
        rows = list((await session.scalars(select(Memory))).all())
    assert len(rows) == 1
    assert rows[0].content == "先结论后依据"
    assert rows[0].is_user_confirmed is False


async def test_working_note_gets_expiry(monkeypatch):
    """进展类最易过期：抽出来的 working_note 默认 30 天后不再注入。"""
    monkeypatch.setattr(settings, "memory_extract_enabled", True)
    _fake_reply(
        _payload(_item(type_="working_note", title="当前进度", content="V1.7 已上线"))
    )

    await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-4",
        user_message="V1.7 已经上线了，下一步做 M4",
        answer="收到。",
    )

    async with async_session_factory() as session:
        row = await session.scalar(select(Memory).where(Memory.user_id == USER))
    assert row is not None
    assert row.expires_at is not None


# ---------------------------------------------------------------- 开关


async def test_short_message_skips_model_call(monkeypatch):
    monkeypatch.setattr(settings, "memory_extract_enabled", True)
    calls = _fake_reply(_payload(_item()))

    stats = await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-5",
        user_message="嗯",
        answer="嗯。",
    )

    assert stats == {"skipped": "too_short"}
    assert calls == []


async def test_disabled_switch_skips_everything(monkeypatch):
    monkeypatch.setattr(settings, "memory_extract_enabled", False)
    calls = _fake_reply(_payload(_item()))

    stats = await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-6",
        user_message="记住我更喜欢先看结论",
        answer="好的。",
    )

    assert stats == {"skipped": "disabled"}
    assert calls == []


async def test_model_failure_is_swallowed(monkeypatch):
    """抽取挂了只留 warning：这一轮对话已经答完了，不能因为后台任务再翻车。"""
    monkeypatch.setattr(settings, "memory_extract_enabled", True)

    async def boom(system: str, human: str) -> str:
        raise RuntimeError("供应商 500")

    memory_extract.set_caller_for_tests(boom)

    stats = await memory_extract.run_extraction(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="thread-7",
        user_message="记住我更喜欢先看结论",
        answer="好的。",
    )

    assert stats == {"error": "RuntimeError"}


# ---------------------------------------------------------------- 注入门槛


async def _seed_memory(
    *,
    title: str,
    content: str,
    confidence: float,
    confirmed: bool,
    memory_type: MemoryType = MemoryType.PREFERENCE,
) -> None:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                await memory_service.create_memory(
                    session,
                    user_id=USER,
                    memory_type=memory_type,
                    title=title,
                    content=content,
                    source="user" if confirmed else "auto_extract",
                    confidence=confidence,
                    is_user_confirmed=confirmed,
                )


async def _prompt_titles() -> list[str]:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            rows = await memory_service.load_for_prompt(session, user_id=USER)
    return [row.title for row in rows]


async def test_explicit_candidate_enters_prompt_without_confirmation():
    """0.9 是「用户明确说过」这一档，模型只是记下来，可以直接进 prompt。"""
    await _seed_memory(
        title="回答风格", content="先结论后依据", confidence=0.9, confirmed=False
    )
    assert await _prompt_titles() == ["回答风格"]


async def test_inferred_candidate_waits_for_confirmation():
    """0.7 是「可合理推断」，留在记忆页等人判断，不能自己变成人格设定。"""
    await _seed_memory(
        title="回答风格", content="可能喜欢先看结论", confidence=0.7, confirmed=False
    )
    assert await _prompt_titles() == []


async def test_confirmed_memory_enters_prompt_at_low_confidence():
    """用户自己确认过的，置信度低也照注入——那是他自己的判断。"""
    await _seed_memory(
        title="称呼", content="叫我老张", confidence=0.5, confirmed=True
    )
    assert await _prompt_titles() == ["称呼"]


async def test_candidate_below_floor_never_reaches_prompt():
    """低于 0.6 的候选连列表都不该进，更不该进 prompt。"""
    await _seed_memory(
        title="随机猜测", content="大概是做运维的", confidence=0.4, confirmed=False
    )
    assert await _prompt_titles() == []
