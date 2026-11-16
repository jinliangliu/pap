# tests/unit/test_memory_dream.py
"""梦境：解析、证据选择、红线、应用语义与幂等。

四条最该守住的东西：

1. 用户确认过或手写的记忆，既不生成建议、也不能被应用——红线只有这一条；
2. 证据只取 user 消息，助手自己的话不能证明用户说过什么；
3. 模型输出不合法一律降级 keep，不能让一条坏输出中断整批；
4. 应用建议不等于确认记忆：改写后仍是未确认候选，要人再点确认。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.conversation import Conversation, Message, MessageRole
from app.models.memory import (
    Memory,
    MemoryReview,
    MemoryReviewAction,
    MemoryReviewStatus,
    MemoryStatus,
    MemoryType,
)
from app.services import memory_dream, memory_service
from app.services.model_service import TextModelConfig
from app.tenancy.context import tenant_scope

TENANT = "default"
USER = "dream-user"
OTHER_USER = "dream-other"


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


def _use_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(model_override: str | None = None) -> TextModelConfig:
        return _config()

    monkeypatch.setattr(memory_dream, "resolve_text_model", fake_resolve)


def _fake_reply(raw: str) -> list[list[str]]:
    calls: list[list[str]] = []

    async def caller(system: str, human: str) -> str:
        calls.append([system, human])
        return raw

    memory_dream.set_caller_for_tests(caller)
    return calls


def _proposal(**overrides: object) -> str:
    payload = {
        "action": "keep",
        "reason": "证据不足",
        "title": "",
        "content": "",
        "merge_target_id": "",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
async def _clean_db(session):
    await session.execute(delete(MemoryReview))
    await session.execute(delete(Message))
    await session.execute(delete(Conversation))
    await session.execute(delete(Memory))
    await session.commit()
    yield
    memory_dream.set_caller_for_tests(None)
    await session.execute(delete(MemoryReview))
    await session.execute(delete(Message))
    await session.execute(delete(Conversation))
    await session.execute(delete(Memory))
    await session.commit()


async def _seed_memory(
    *,
    title: str = "回答风格",
    content: str = "用户偏好简洁的回答",
    source: str = "auto_extract",
    confirmed: bool = False,
    thread_id: str | None = "thread-1",
    user: str = USER,
) -> uuid.UUID:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                row, _ = await memory_service.create_memory(
                    session,
                    user_id=user,
                    memory_type=MemoryType.PREFERENCE,
                    title=title,
                    content=content,
                    source=source,
                    confidence=0.8,
                    is_user_confirmed=confirmed,
                    source_thread_id=thread_id,
                )
                return row.id


async def _seed_message(
    text: str,
    *,
    thread_id: str = "thread-1",
    role: MessageRole = MessageRole.USER,
    user: str = USER,
    days_ago: int = 0,
) -> None:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.scalar(
                    select(Conversation).where(Conversation.thread_id == thread_id)
                )
                if conversation is None:
                    conversation = Conversation(user_id=user, thread_id=thread_id, title=thread_id)
                    session.add(conversation)
                    await session.flush()
                session.add(
                    Message(
                        conversation_id=conversation.id,
                        role=role,
                        content=text,
                        created_at=datetime.now(UTC) - timedelta(days=days_ago),
                    )
                )


async def _load_memory(memory_id: uuid.UUID) -> Memory:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            row = await session.get(Memory, memory_id)
            assert row is not None
            return row


async def _reviews() -> list[MemoryReview]:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            return list((await session.scalars(select(MemoryReview))).all())


# ---------------------------------------------------------------- 解析


def test_parse_correct_requires_title_and_content():
    proposal = memory_dream.parse_proposal(
        _proposal(action="correct", title="回答风格", content=""),
        allowed_merge_ids=set(),
    )
    # 缺内容时不能应用半条建议，降级 keep 比让人看到「改写成空」强。
    assert proposal.action == MemoryReviewAction.KEEP


def test_parse_merge_rejects_invented_target():
    proposal = memory_dream.parse_proposal(
        _proposal(action="merge", merge_target_id=str(uuid.uuid4())),
        allowed_merge_ids=set(),
    )
    # 模型编一个不存在的 id 是常见幻觉，直接应用会把两条无关记忆合并掉。
    assert proposal.action == MemoryReviewAction.KEEP


def test_parse_merge_accepts_known_target():
    target = uuid.uuid4()
    proposal = memory_dream.parse_proposal(
        _proposal(action="merge", merge_target_id=str(target)),
        allowed_merge_ids={target},
    )
    assert proposal.action == MemoryReviewAction.MERGE
    assert proposal.merge_target_id == target


def test_parse_low_confidence_degrades_to_keep():
    proposal = memory_dream.parse_proposal(
        _proposal(action="retire", confidence=0.3),
        allowed_merge_ids=set(),
    )
    assert proposal.action == MemoryReviewAction.KEEP


def test_parse_fenced_and_chinese_action():
    raw = "```json\n" + _proposal(action="下架", reason="用户已改口") + "\n```"
    proposal = memory_dream.parse_proposal(raw, allowed_merge_ids=set())
    assert proposal.action == MemoryReviewAction.RETIRE


def test_parse_garbage_keeps_without_raising():
    for raw in ("模型今天不想说话", '{"action":', "", "null"):
        proposal = memory_dream.parse_proposal(raw, allowed_merge_ids=set())
        assert proposal.action == MemoryReviewAction.KEEP
        assert proposal.confidence == 0.0


# ---------------------------------------------------------------- 证据


async def test_collect_evidence_only_uses_user_messages():
    memory_id = await _seed_memory()
    await _seed_message("我更喜欢简洁的回答，别写太长")
    # 助手自己的话不能当证据：拿模型输出证明用户说过什么等于自证。
    await _seed_message("用户偏好简洁的回答，这一点已经确认。", role=MessageRole.ASSISTANT)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            memory = await session.get(Memory, memory_id)
            assert memory is not None
            evidence = await memory_dream.collect_evidence(session, memory)

    assert evidence
    assert all("已经确认" not in item.excerpt for item in evidence)


async def test_collect_evidence_prefers_source_thread():
    memory_id = await _seed_memory(thread_id="thread-source")
    await _seed_message("回答别太长，简洁一点", thread_id="thread-source", days_ago=10)
    await _seed_message("简洁的回答更好", thread_id="thread-later", days_ago=1)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            memory = await session.get(Memory, memory_id)
            assert memory is not None
            evidence = await memory_dream.collect_evidence(session, memory)

    assert evidence[0].excerpt.startswith("回答别太长")


async def test_collect_evidence_marks_unmatched_context():
    memory_id = await _seed_memory()
    await _seed_message("今天上海天气怎么样")

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            memory = await session.get(Memory, memory_id)
            assert memory is not None
            evidence = await memory_dream.collect_evidence(session, memory)

    # 没有任何词面交集时仍给背景，但必须标 matched=false，prompt 里禁止当依据。
    assert evidence
    assert all(item.matched is False for item in evidence)


# ---------------------------------------------------------------- 红线


async def test_can_review_blocks_confirmed_and_user_written():
    user_memory = await _load_memory(await _seed_memory(source="user", confirmed=True))
    confirmed_auto = await _load_memory(
        await _seed_memory(title="称呼", source="auto_extract", confirmed=True)
    )
    candidate = await _load_memory(await _seed_memory(title="工作节奏"))

    assert memory_dream.can_review(user_memory) is False
    assert memory_dream.can_review(confirmed_auto) is False
    assert memory_dream.can_review(candidate) is True


# ---------------------------------------------------------------- 批量梦境


async def test_run_dreaming_creates_pending_review(monkeypatch):
    _use_model(monkeypatch)
    memory_id = await _seed_memory()
    await _seed_message("我不喜欢太长的回答，简洁一点")
    _fake_reply(
        _proposal(
            action="correct",
            reason="用户原话是简洁",
            title="回答风格",
            content="偏好简洁的回答",
            confidence=0.9,
        )
    )

    report = await memory_dream.run_dreaming(tenant_id=TENANT, user_id=USER, days=30, limit=5)

    assert report.scanned == 1
    assert report.suggested == 1
    assert report.failed == 0

    rows = await _reviews()
    assert len(rows) == 1
    assert rows[0].memory_id == memory_id
    assert rows[0].action == MemoryReviewAction.CORRECT
    assert rows[0].status == MemoryReviewStatus.PENDING
    assert rows[0].model == "test-model"
    assert rows[0].evidence_json


async def test_run_dreaming_is_idempotent_and_skips_pending(monkeypatch):
    _use_model(monkeypatch)
    await _seed_memory()
    await _seed_message("回答简洁一点")
    _fake_reply(_proposal(action="retire", reason="已改口", confidence=0.9))

    first = await memory_dream.run_dreaming(tenant_id=TENANT, user_id=USER, days=30, limit=5)
    second = await memory_dream.run_dreaming(tenant_id=TENANT, user_id=USER, days=30, limit=5)

    assert first.suggested == 1
    assert second.skipped_pending == 1
    assert len(await _reviews()) == 1


async def test_run_dreaming_keep_does_not_write(monkeypatch):
    _use_model(monkeypatch)
    await _seed_memory()
    await _seed_message("回答简洁一点")
    _fake_reply(_proposal(action="keep", reason="证据不足"))

    report = await memory_dream.run_dreaming(tenant_id=TENANT, user_id=USER, days=30, limit=5)

    assert report.kept == 1
    assert report.suggested == 0
    assert await _reviews() == []


async def test_run_dreaming_swallows_model_failure(monkeypatch):
    _use_model(monkeypatch)
    await _seed_memory()
    await _seed_message("回答简洁一点")

    async def broken(system: str, human: str) -> str:
        raise RuntimeError("模型炸了")

    memory_dream.set_caller_for_tests(broken)

    report = await memory_dream.run_dreaming(tenant_id=TENANT, user_id=USER, days=30, limit=5)

    assert report.failed == 1
    assert await _reviews() == []


async def test_run_dreaming_never_touches_other_users(monkeypatch):
    _use_model(monkeypatch)
    await _seed_memory(user=OTHER_USER, title="别人的偏好")
    await _seed_message("别人的回答要详细", user=OTHER_USER)

    report = await memory_dream.run_dreaming(tenant_id=TENANT, user_id=USER, days=30, limit=5)

    assert report.scanned == 0
    assert await _reviews() == []


# ---------------------------------------------------------------- 应用语义


async def _make_review(
    memory_id: uuid.UUID,
    *,
    action: MemoryReviewAction,
    title: str | None = None,
    content: str | None = None,
    target: uuid.UUID | None = None,
) -> uuid.UUID:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                memory = await session.get(Memory, memory_id)
                assert memory is not None
                proposal = memory_dream.ReviewProposal(
                    action=action,
                    reason="测试",
                    confidence=0.9,
                    proposed_title=title,
                    proposed_content=content,
                    merge_target_id=target,
                    evidence=[],
                )
                review = await memory_dream.create_review(
                    session, memory=memory, proposal=proposal, model="test-model"
                )
                return review.id


async def _apply(review_id: uuid.UUID) -> Memory:
    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                review = await session.get(MemoryReview, review_id)
                assert review is not None
                return await memory_dream.apply_review(session, review, actor_user_id=USER)


async def test_apply_correct_keeps_candidate_unconfirmed():
    memory_id = await _seed_memory()
    review_id = await _make_review(
        memory_id,
        action=MemoryReviewAction.CORRECT,
        title="回答风格",
        content="偏好简洁的回答",
    )

    memory = await _apply(review_id)

    assert memory.content == "偏好简洁的回答"
    # 模型纠错不等于用户确认：确认仍然要人点。
    assert memory.is_user_confirmed is False


async def test_apply_retire_supersedes_without_deleting():
    memory_id = await _seed_memory()
    review_id = await _make_review(memory_id, action=MemoryReviewAction.RETIRE)

    memory = await _apply(review_id)

    assert memory.status == MemoryStatus.SUPERSEDED


async def test_apply_merge_supersedes_source():
    source_id = await _seed_memory(title="回答风格")
    target_id = await _seed_memory(title="表达偏好")
    review_id = await _make_review(
        source_id,
        action=MemoryReviewAction.MERGE,
        content="偏好简洁的表达",
        target=target_id,
    )

    source = await _apply(review_id)
    target = await _load_memory(target_id)

    assert source.status == MemoryStatus.SUPERSEDED
    assert source.superseded_by_id == target_id
    assert target.content == "偏好简洁的表达"


async def test_apply_rejects_confirmed_memory():
    memory_id = await _seed_memory(source="user", confirmed=True)
    review_id = await _make_review(memory_id, action=MemoryReviewAction.RETIRE)

    with pytest.raises(memory_dream.ReviewConflict):
        await _apply(review_id)

    memory = await _load_memory(memory_id)
    assert memory.status == MemoryStatus.ACTIVE


async def test_apply_is_not_repeatable():
    memory_id = await _seed_memory()
    review_id = await _make_review(memory_id, action=MemoryReviewAction.RETIRE)
    await _apply(review_id)

    with pytest.raises(memory_dream.ReviewConflict):
        await _apply(review_id)


async def test_dismiss_keeps_memory_untouched():
    memory_id = await _seed_memory()
    review_id = await _make_review(memory_id, action=MemoryReviewAction.RETIRE)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            async with session.begin():
                review = await session.get(MemoryReview, review_id)
                assert review is not None
                await memory_dream.dismiss_review(session, review)

    memory = await _load_memory(memory_id)
    assert memory.status == MemoryStatus.ACTIVE
    rows = await _reviews()
    assert rows[0].status == MemoryReviewStatus.DISMISSED


# ---------------------------------------------------------------- 指标


async def test_extraction_stats_counts_only_auto_candidates():
    await _seed_memory(title="已确认", confirmed=True)
    await _seed_memory(title="待确认")
    await _seed_memory(title="手写的", source="user", confirmed=True)

    with tenant_scope(TENANT):
        async with async_session_factory() as session:
            stats = await memory_dream.extraction_stats(session, user_id=USER, days=7)

    assert stats.auto_total == 2
    assert stats.auto_confirmed == 1
    assert stats.auto_pending == 1
    assert stats.confirmation_rate == pytest.approx(0.5)
