# tests/unit/test_context_service.py
"""上下文切边界与摘要输入的纯函数测试。

这里不碰数据库也不碰模型：窗口怎么切、摘要怎么写入口、估值多保守，
都是确定性的，能纯函数测的就别放到集成里，否则一条断言要跑八轮对话。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.services import context_service as ctx


def _turn(user: str, assistant: str, *, index: int = 0) -> ctx.Turn:
    at = datetime(2026, 11, 13, 12, 0, tzinfo=UTC) + timedelta(minutes=index)
    return ctx.Turn(
        user_text=user,
        assistant_text=assistant,
        user_at=at,
        last_message_id=uuid.uuid4(),
        last_message_at=at,
        messages=2,
    )


def test_estimate_tokens_is_conservative():
    """每个字符按更贵的比例算：估多了只是早压，估少了会撞上限。"""
    assert ctx.estimate_tokens("") == 0
    assert ctx.estimate_tokens("a" * 130) == 100
    # 取小反而估多：1.3 字符/token 比真实的 1.5-1.8 更保守。
    assert ctx.estimate_tokens("a" * 130) > len("a" * 130) / 1.8


def test_plan_window_keeps_contiguous_tail():
    turns = [_turn(f"问题{i}", "回答" * 10, index=i) for i in range(10)]

    kept, dropped = ctx.plan_window(turns, budget_tokens=200, min_turns=3)

    assert kept == turns[-len(kept) :], "保留的必须是连续尾段，不能中间挖洞"
    assert dropped == turns[: len(turns) - len(kept)]
    assert not set(id(t) for t in kept) & set(id(t) for t in dropped)


def test_plan_window_keeps_min_turns_even_over_budget():
    """预算不是硬约束：连最近几轮都看不到，模型会把上一问重答一遍。"""
    turns = [_turn("很长的问题" * 50, "很长的回答" * 50, index=i) for i in range(8)]

    kept, _ = ctx.plan_window(turns, budget_tokens=10, min_turns=6)

    assert len(kept) == 6


@pytest.mark.parametrize(
    ("measured", "expected"),
    [
        (0, "none"),
        (100, "none"),
        (19_200, "compact"),   # 0.6 × 32000
        (27_200, "hard"),      # 0.85 × 32000
        (40_000, "hard"),
    ],
)
def test_decide_action_uses_measured_tokens(measured: int, expected: str):
    assert ctx.decide_action(measured=measured, window=settings.context_window_tokens) == expected


def test_decide_action_without_measurement_never_compacts():
    """第一轮没有实测值。此时压缩只会把唯一一条消息压掉。"""
    assert ctx.decide_action(measured=0, window=32_000) == "none"
    assert ctx.decide_action(measured=-1, window=32_000) == "none"


def test_decide_action_rejects_inverted_thresholds(monkeypatch):
    """压缩线高于硬截断线时，压缩永远不会发生，只剩无声的截断。

    配置层已经拦了这种写法（config._validate_auth_mode），这里再确认一次
    判定函数本身也自洽：先判 hard 再判 compact。
    """
    monkeypatch.setattr(settings, "context_compact_ratio", 0.5)
    monkeypatch.setattr(settings, "context_hard_ratio", 0.4)

    assert ctx.decide_action(measured=16_000, window=32_000) == "hard"


def test_transcript_drops_oldest_turns_and_says_so():
    turns = [_turn(f"第{i}轮的问题" + "细节" * 40, "回答" * 40, index=i) for i in range(6)]

    text = ctx.transcript_of(turns, limit=400)

    assert "第5轮的问题" in text, "最新的轮次必须在"
    assert "第0轮的问题" not in text, "超上限时丢的是最旧的"
    assert "未纳入本次压缩" in text, "丢了多少轮要写出来，否则模型以为对话就这么短"


def test_transcript_keeps_user_and_assistant_labeled():
    text = ctx.transcript_of([_turn("把工单 A 关掉", "已关闭工单 A")])

    assert text.startswith("用户：把工单 A 关掉")
    assert "助手：已关闭工单 A" in text


def test_clean_summary_drops_oldest_conclusions_not_whole_sections():
    """超限时丢【已定结论】里最旧的条目，其余四节一条不能少。

    线上旧实现是整体 `body[:1200]`：最后一句切在「…要求直」，
    【未完成】【被否决】整节被吃掉——而「用户说过别做什么」正是最不能丢的部分。
    """
    conclusions = "\n".join(f"- 第{index}条结论" + "细节" * 40 for index in range(40))
    summary = (
        "【当前目标】跟进 3 号工单\n"
        f"【已定结论】\n{conclusions}\n"
        "【未完成】等用户确认站点 id\n"
        "【指代绑定】「那个」→ 3 号工单\n"
        "【被否决】不改成已关闭"
    )

    body = ctx.clean_summary(summary)

    assert "【未完成】等用户确认站点 id" in body
    assert "【指代绑定】「那个」→ 3 号工单" in body
    assert "【被否决】不改成已关闭" in body
    assert "更早结论已省略" in body, "丢了多少条要写出来"
    # 不切半句：留下来的条目必须是完整的。
    for line in body.splitlines():
        if line.startswith("- 第"):
            assert line.endswith("细节")
    assert len(body) < len(summary)


def test_clean_summary_keeps_short_summary_verbatim():
    body = ctx.clean_summary("【当前目标】a\n\n\n【被否决】无")

    assert body == "【当前目标】a\n【被否决】无"


def test_clean_summary_hard_cuts_a_single_oversized_line():
    """没有小标题的旧摘要整整一行：只能硬切，但至少留标记。"""
    body = ctx.clean_summary("字" * (ctx.SUMMARY_MAX_CHARS + 50))

    assert body.endswith("…")
    assert len(body) == ctx.SUMMARY_MAX_CHARS + 1


def test_summary_prompt_incremental_carries_previous():
    messages = ctx.summary_messages(previous="【当前目标】关工单", transcript="用户：关掉 3 号", rebuilt=False)

    human = messages[-1][1]
    assert "【当前目标】关工单" in human
    assert "以原文为准" in human, "冲突时要明确以原文为准，否则旧摘要会压过事实"


def test_summary_prompt_rebuild_ignores_previous():
    """全量重建必须只看原文：带上旧摘要就等于「摘要再摘要」，偏差会复利。"""
    messages = ctx.summary_messages(previous="旧摘要内容", transcript="用户：原文", rebuilt=True)

    human = messages[-1][1]
    assert "旧摘要内容" not in human
    assert "全量重建" in human


def test_context_window_prefers_provider_declaration(monkeypatch):
    """供应商按模型声明了窗口就用它：换模型后还按 .env 的旧窗口压缩，等于静默丢历史。"""
    from types import SimpleNamespace

    async def fake(_override=None):
        return SimpleNamespace(context_window=128_000)

    monkeypatch.setattr(ctx, "resolve_text_model", fake)

    assert asyncio.run(ctx._context_window()) == (128_000, "provider")


def test_context_window_falls_back_to_settings_when_undeclared(monkeypatch):
    from types import SimpleNamespace

    async def fake(_override=None):
        return SimpleNamespace(context_window=None)

    monkeypatch.setattr(ctx, "resolve_text_model", fake)

    assert asyncio.run(ctx._context_window()) == (settings.context_window_tokens, "env")


def test_context_window_survives_config_read_failure(monkeypatch):
    """读配置失败只影响窗口，不能让这一轮对话起不来。"""

    async def boom(_override=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(ctx, "resolve_text_model", boom)

    assert asyncio.run(ctx._context_window()) == (settings.context_window_tokens, "env")
