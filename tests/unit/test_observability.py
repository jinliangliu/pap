# tests/unit/test_observability.py
"""自研观测的单元测试。

覆盖四类必须成立的性质（docs/OBSERVABILITY.md §9）：

1. 采内容：full/summary/off 三种模式各自的语义，敏感键必须打码；
2. span 树：父子关系与 seq 正确，异常要落成 error + 类别；
3. 关闭即空转：`OBS_ENABLED=false` 时零写入、零副作用；
4. 观测永不反噬业务：写库失败、队列满都只影响观测自己。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.approval import RiskLevel
from app.models.observability import ObsSpan, ObsTrace
from app.observability import context as obs_context
from app.observability.context import (
    capture,
    current_span,
    current_trace,
    redact,
    redact_text,
    span,
    trace_run,
)
from app.observability.writer import ObsWriter, get_writer
from app.tools.gateway import ToolGateway
from app.tools.registry import ToolRegistry
from app.tools.types import ToolDefinition, ToolExecutionContext, ToolExecutionResult


@pytest.fixture
def collected(monkeypatch):
    """把 trace 提交拦在内存里，不碰数据库。"""
    boxes: list[dict] = []
    monkeypatch.setattr(obs_context, "_submit", lambda bundle: boxes.append(bundle))
    monkeypatch.setattr(settings, "obs_enabled", True)
    return boxes


@pytest.fixture
def spans_of():
    """从采集到的 bundle 里按名字取 span。"""

    def _get(boxes: list[dict], name: str) -> dict:
        bundle = boxes[-1]
        for row in bundle["spans"]:
            if row["name"] == name:
                return row
        raise AssertionError(f"没有记录名为 {name} 的 span：{[r['name'] for r in bundle['spans']]}")

    return _get


@pytest.fixture
def writer(monkeypatch):
    """换掉模块级单例。

    `trace_run` 是通过 `get_writer()` 拿实例的，测试里自己 new 一个不会
    被用到——这一点踩过：submit 静默丢弃（队列为 None），断言一片 0。
    """
    instance = ObsWriter()
    monkeypatch.setattr("app.observability.writer._writer", instance)
    monkeypatch.setattr(settings, "obs_enabled", True)
    return instance


# ---------------------------------------------------------------- 内容采集


def test_capture_full_keeps_original_but_redacts_secrets(monkeypatch):
    monkeypatch.setattr(settings, "obs_capture_content", "full")

    assert capture("普通文本 station-001 压力 1.2MPa") == "普通文本 station-001 压力 1.2MPa"

    # 完整原文 ≠ 连密钥一起存：prompt 里的 token 必须打码。
    out = capture({"query": "查站点", "api_key": "sk-live-123"})
    assert "查站点" in out
    assert "sk-live-123" not in out
    assert "[redacted]" in out


def test_capture_summary_keeps_head_and_fingerprint(monkeypatch):
    monkeypatch.setattr(settings, "obs_capture_content", "summary")

    out = capture("字" * 500)
    assert out.startswith("字" * 200)
    assert "len=500" in out
    assert "sha256=" in out
    assert len(out) < 500  # 摘要必须真的更短


def test_capture_off_records_nothing(monkeypatch):
    monkeypatch.setattr(settings, "obs_capture_content", "off")

    assert capture("随便什么") is None
    assert capture({"a": 1}) is None


def test_capture_truncates_at_configured_limit(monkeypatch):
    monkeypatch.setattr(settings, "obs_capture_content", "full")
    monkeypatch.setattr(settings, "obs_max_content_chars", 10)

    out = capture("12345678901234567890")
    # 截断要留痕：否则会误以为原文就这么长。
    assert out.startswith("1234567890")
    assert "total=20" in out


def test_redact_walks_nested_structures():
    out = redact(
        {
            "headers": {"Authorization": "Bearer abc", "Accept": "json"},
            "items": [{"token": "t-1", "station_id": "s-1"}],
            "station_id": "s-2",
        }
    )

    assert out["headers"]["Authorization"] == "[redacted]"
    # 键名大小写原样保留：观测要能对照上游真正收到的请求头。
    assert out["headers"]["Accept"] == "json"
    assert out["items"][0]["token"] == "[redacted]"
    # 站点号不是密钥，删了就没法排查「查的是哪个站」。
    assert out["items"][0]["station_id"] == "s-1"
    assert out["station_id"] == "s-2"


def test_redact_text_handles_common_leak_shapes():
    assert "abc123" not in redact_text("?token=abc123&x=1")
    assert "sk-live" not in redact_text('{"api_key": "sk-live-9"}')
    assert "Bearer" not in redact_text("Authorization: Bearer zzz")
    assert redact_text("station-001 压力 1.2MPa") == "station-001 压力 1.2MPa"


# ---------------------------------------------------------------- span 树


async def test_spans_nest_and_keep_order(collected, spans_of):
    async with trace_run(tenant_id="t1", user_id="u1", input_text="你好") as trace:
        assert current_trace() is trace

        async with span("node.plan", "node") as outer:
            outer.add(output_keys="intent")
            async with span("llm.call", "llm") as inner:
                assert current_span() is inner.record
                inner.content(input="prompt"), inner.content(output="plan")

    bundle = collected[-1]
    assert bundle["trace"]["span_count"] == 2
    assert bundle["trace"]["status"] == "ok"
    assert bundle["trace"]["input_text"] == "你好"
    assert bundle["trace"]["duration_ms"] >= 0

    root = spans_of(collected, "node.plan")
    child = spans_of(collected, "llm.call")

    assert root["parent_span_id"] is None
    assert child["parent_span_id"] == root["span_id"]
    # seq 是展示顺序，不能靠 started_at 排（可能完全相同）。
    assert root["seq"] < child["seq"]
    assert root["attrs"]["output_keys"] == "intent"

    # 二次写 content 要合并而不是覆盖，否则先记的 input 就白记了。
    payload = json.loads(child["content"])
    assert payload == {"input": "prompt", "output": "plan"}


async def test_span_error_is_recorded_and_reraised(collected, spans_of):
    with pytest.raises(RuntimeError):
        async with trace_run(tenant_id="t1", user_id="u1"):
            async with span("tool.call", "tool"):
                raise RuntimeError("boom")

    bundle = collected[-1]
    # 异常必须继续往上抛：观测只记录，不改变控制流。
    assert bundle["trace"]["status"] == "error"
    assert bundle["trace"]["error_category"] == "error:RuntimeError"

    row = spans_of(collected, "tool.call")
    assert row["status"] == "error"
    assert row["error_category"] == "error:RuntimeError"


async def test_error_category_prefers_explicit_marking(collected):
    async with trace_run(tenant_id="t1", user_id="u1") as trace:
        assert trace is not None
        trace.mark(status="timeout", error_category="chat_timeout")

    bundle = collected[-1]
    assert bundle["trace"]["status"] == "timeout"
    assert bundle["trace"]["error_category"] == "chat_timeout"


# ---------------------------------------------------------------- 关闭即空转


async def test_disabled_observability_is_pure_overhead(monkeypatch):
    monkeypatch.setattr(settings, "obs_enabled", False)

    boxes: list[dict] = []
    monkeypatch.setattr(obs_context, "_submit", lambda bundle: boxes.append(bundle))

    async with trace_run(tenant_id="t1", user_id="u1") as trace:
        # 关闭时 trace 是 None，调用方以此判空（chat_service 就是这么写的）。
        assert trace is None
        async with span("node.plan", "node") as handle:
            handle.add(output_keys="intent")
            handle.content(input="x", output="y")
            handle.fail("nope")
        assert current_span() is None

    assert boxes == []


async def test_span_without_trace_does_not_open_one(collected):
    """Studio 入口没有 trace 上下文，节点里的 span 不能凭空造出孤儿 trace。"""
    async with span("node.plan", "node") as handle:
        assert handle.record is None
        handle.add(anything=1)

    assert collected == []


# ---------------------------------------------------------------- 写入器


async def test_writer_persists_trace_and_spans(writer):
    await writer.start()
    try:
        async with trace_run(
            tenant_id="t1",
            user_id="u1",
            conversation_id=None,
            input_text="要写入的原文",
        ) as trace:
            assert trace is not None
            trace.mark(intent="query", output_text="写回的答案")
            async with span("node.respond", "node"):
                pass

        # 攒批最长 1 秒，给足等待时间但不无限等。
        # 只挑本用例租户的行：同一个进程里的库是全量共享的，
        # 别的用例已经写过 default 租户的 trace。
        for _ in range(40):
            async with async_session_factory() as session:
                rows = (
                    (await session.execute(select(ObsTrace).where(ObsTrace.tenant_id == "t1")))
                    .scalars()
                    .all()
                )
                if rows:
                    break
            await asyncio.sleep(0.1)
    finally:
        await writer.stop()

    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == "t1"
    assert row.input_text == "要写入的原文"
    assert row.output_text == "写回的答案"
    assert row.intent == "query"
    assert row.span_count == 1
    assert row.status == "ok"

    async with async_session_factory() as session:
        span_rows = (
            (await session.execute(select(ObsSpan).where(ObsSpan.trace_id == row.trace_id)))
            .scalars()
            .all()
        )
    assert [s.name for s in span_rows] == ["node.respond"]
    assert span_rows[0].trace_id == row.trace_id

    stats = writer.stats()
    assert stats["written"] == 1
    assert stats["failed"] == 0


async def test_writer_failure_never_propagates(monkeypatch, writer):
    """表被删 / 库挂了 / RLS 拒绝，都只能记日志，不能让对话 500。"""

    class _BrokenFactory:
        def __call__(self):
            raise RuntimeError("database is gone")

    monkeypatch.setattr("app.observability.writer.async_session_factory", _BrokenFactory())

    await writer.start()
    try:
        async with trace_run(tenant_id="t1", user_id="u1", input_text="x"):
            pass
        # 等消费者处理完这一批（失败也要走完 task_done）。
        for _ in range(40):
            if writer.stats()["failed"]:
                break
            await asyncio.sleep(0.1)
    finally:
        await writer.stop()

    # 关键：上面没有抛异常。对话侧看到的就是「观测什么都没写」。
    assert writer.stats()["failed"] == 1


async def test_writer_drops_oldest_when_queue_is_full(writer):
    writer._queue = asyncio.Queue(maxsize=2)

    for i in range(3):
        writer.submit({"tenant_id": "t1", "trace": {"input_text": str(i)}, "spans": []})

    assert writer.stats()["dropped"] == 1
    assert writer.stats()["queue_depth"] == 2
    # 丢的是最旧的：正在排查的那一轮更可能是新的。
    remaining = [writer._queue.get_nowait()["trace"]["input_text"] for _ in range(2)]
    assert remaining == ["1", "2"]


async def test_writer_sampling_keeps_errors(monkeypatch, writer):
    monkeypatch.setattr(settings, "obs_sample_rate", 0.0)

    writer._queue = asyncio.Queue(maxsize=10)

    writer.submit({"tenant_id": "t1", "trace": {"status": "ok"}, "spans": []})
    writer.submit({"tenant_id": "t1", "trace": {"status": "error"}, "spans": []})

    # 采样只丢成功的；错误 trace 排查价值最高，一律保留。
    assert writer.stats()["sampled_out"] == 1
    assert writer.stats()["queue_depth"] == 1


async def test_submit_does_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "obs_enabled", False)

    writer = get_writer()
    writer.submit({"tenant_id": "t1", "trace": {"status": "ok"}, "spans": []})
    assert writer.stats()["queue_depth"] == 0


# ---------------------------------------------------------------- 工具埋点


def _echo_tool(**overrides) -> ToolDefinition:
    async def handler(ctx, arguments):  # noqa: ANN001
        return ToolExecutionResult(success=True, result={"echo": arguments})

    defaults = {
        "name": "echo",
        "display_name": "回声",
        "description": "回显参数。",
        "kind": "python",
        "risk_level": RiskLevel.READ,
        "required_scopes": ("tools:read",),
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "handler": handler,
    }
    defaults.update(overrides)
    return ToolDefinition(**defaults)


async def test_gateway_records_tool_span(collected, spans_of):
    registry = ToolRegistry()
    gateway = ToolGateway(registry)
    gateway.register_tool(_echo_tool())

    context = ToolExecutionContext(
        tenant_id="t1",
        user_id="u1",
        thread_id="th1",
        run_id="r1",
        request_id="req1",
        scopes=("tools:read",),
        roles=(),
    )

    async with trace_run(tenant_id="t1", user_id="u1") as trace:
        assert trace is not None
        async with span("execute_read_tool", "node"):
            await gateway.execute_read_only(
                context=context, tool_name="echo", arguments={"value": "hi"}
            )

    row = spans_of(collected, "tool.call")
    assert row["kind"] == "tool"
    assert row["attrs"]["tool"] == "echo"
    assert row["attrs"]["ok"] is True
    assert row["attrs"]["args_keys"] == "value"
    # 入参与返回都要留在同一个 content 里（合并而不是覆盖）。
    payload = json.loads(row["content"])
    assert payload["input"] == {"value": "hi"}
    assert payload["output"] == {"echo": {"value": "hi"}}
    # 工具 span 必须挂在节点 span 下面，而不是挂在根上。
    node_span = spans_of(collected, "execute_read_tool")
    assert row["parent_span_id"] == node_span["span_id"]


async def test_gateway_marks_failed_tool_as_error(collected, spans_of):
    async def failing(ctx, arguments):  # noqa: ANN001
        return ToolExecutionResult(
            success=False, result={}, error_code="UPSTREAM_500", message="上游挂了"
        )

    registry = ToolRegistry()
    gateway = ToolGateway(registry)
    gateway.register_tool(_echo_tool(handler=failing))

    context = ToolExecutionContext(
        tenant_id="t1",
        user_id="u1",
        thread_id="th1",
        run_id="r1",
        request_id="req1",
        scopes=("tools:read",),
        roles=(),
    )

    async with trace_run(tenant_id="t1", user_id="u1"):
        await gateway.execute_read_only(
            context=context, tool_name="echo", arguments={"value": "hi"}
        )

    row = spans_of(collected, "tool.call")
    assert row["status"] == "error"
    assert row["error_category"] == "UPSTREAM_500"
    assert row["attrs"]["ok"] is False
