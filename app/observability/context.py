# app/observability/context.py
"""观测的上下文与 span 语义。

一轮对话 = 一个 trace；节点/模型/工具 = span。父子关系靠 span 栈，
跨 async 边界靠 contextvar（LangGraph 节点都是协程，天然随 task 传播）。

三条设计约束（源自 docs/OBSERVABILITY.md §6.1）：

1. **关掉就是空转**：`OBS_ENABLED=false` 时 `span()` 不进栈、不分配 ID、
   不写队列，只产出一个哑对象。业务侧只多一次布尔判断。
2. **绝不改变控制流**：记录过程里的任何异常都被吞掉。观测挂了不能把对话带挂。
3. **整棵树一起提交**：span 先攒在内存里，trace 结束时一次交给写入器。
   这样既没有「逐 span 排队」的开销，也不需要回写 trace 汇总行。
   代价是进程中途被 kill 会丢掉那一条——观测数据，可以接受。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.services import runtime_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 敏感值打码

# 键名按 `_`/`-` 归一后比对，所以 apiKey / api-key / API_KEY 都能命中。
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|apikey|token|secret|passwd|password|authorization|cookie"
    r"|session|credential|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_REDACTED = "[redacted]"


def _normalize_key(key: str) -> str:
    return key.replace("-", "_").lower()


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(_normalize_key(key)))


def redact(value: Any, *, _depth: int = 0) -> Any:
    """递归打码。只替换命中黑名单的**值**，键名与层级原样保留。

    「完整原文」不等于「连密钥一起存」：prompt 泄露是开发者看到自己系统的数据，
    密钥泄露是系统被别人接管。前者是本工具存在的目的，后者与调试无关。
    """
    if _depth > 12:
        return "[too-deep]"

    if isinstance(value, dict):
        # 键名保留原样（包括大小写）：观测要还原「上游到底收到了什么」，
        # 顺手把 Authorization 改成 authorization 会让对照原始请求变难。
        return {
            str(k): (_REDACTED if is_sensitive_key(str(k)) else redact(v, _depth=_depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value


def redact_text(text: str) -> str:
    """文本里的 `key=value` / `"key": "value"` 也打一遍码。

    工具返回常见的形态是 JSON 串或 `?token=xxx`，不做这一步等于前面的
    结构化打码白做。
    """
    if not text:
        return text

    def _kv(match: re.Match[str]) -> str:
        # group(1) 始终是「键 + 分隔符」，值整体换掉。
        return f"{match.group(1)}{_REDACTED}"

    text = re.sub(
        r'((?:api[_-]?key|token|secret|password|passwd|authorization|cookie)"?\s*[:=]\s*")'
        r"([^\"\n]{1,200})",
        _kv,
        text,
        flags=re.IGNORECASE,
    )
    # URL query 里的敏感值
    text = re.sub(
        r"([?&](?:api_key|apikey|token|secret|password|access_token)=)([^&\s]{1,200})",
        _kv,
        text,
        flags=re.IGNORECASE,
    )
    # 请求头形式（不带引号）：`Authorization: Bearer xxx`
    text = re.sub(
        r"(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;\"'}]{1,300}",
        _kv,
        text,
        flags=re.IGNORECASE,
    )
    return text


# ---------------------------------------------------------------- 内容抓取


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _as_uuid(value: Any) -> uuid.UUID | None:
    """会话 ID 在业务侧一直是字符串，列类型却是 Uuid。

    不在这一层转，写库时会报 `'str' object has no attribute 'hex'`，
    而且异常发生在写入器的后台任务里——只留一条日志，对话照常返回，
    排查时几乎看不出是这里。
    """
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def capture(value: Any) -> str | None:
    """按 OBS_CAPTURE_CONTENT 决定记什么。

    full    → 完整原文（打码敏感键），仅受 obs_max_content_chars 限制防爆内存；
    summary → 前 200 字 + 长度 + sha256；
    off     → None。
    """
    if value is None:
        return None

    if not isinstance(value, str):
        try:
            value = json.dumps(redact(value), ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    else:
        value = redact_text(value)

    mode = settings.obs_capture_content
    if mode == "off":
        return None

    if mode == "summary":
        head = value[:200]
        return f"{head}…[len={len(value)}, sha256={_sha256(value)[:16]}]"

    limit = settings.obs_max_content_chars
    if limit and len(value) > limit:
        # 超限只截断，不静默丢弃：留个尾巴说明被截了。
        return f"{value[:limit]}…[truncated, total={len(value)}]"
    return value


def capture_payload(value: Any) -> Any:
    """同 `capture`，但结构化值保持结构，不压成一段 JSON 文本。

    `content` 最终整体会被 json.dumps 一次，所以这里若把 dict 先转成字符串，
    存下来就是「JSON 里套一个转义的 JSON 字符串」——读的时候要多解一层，
    界面上也没法折叠。全文模式直接嵌对象更合理。
    """
    if value is None or isinstance(value, str):
        return capture(value)

    mode = settings.obs_capture_content
    if mode == "off":
        return None

    redacted = redact(value)
    if mode == "summary":
        return capture(redacted)

    try:
        dumped = json.dumps(redacted, ensure_ascii=False, default=str)
    except Exception:
        return capture(str(redacted))

    limit = settings.obs_max_content_chars
    if limit and len(dumped) > limit:
        return f"{dumped[:limit]}…[truncated, total={len(dumped)}]"
    return redacted


# ---------------------------------------------------------------- 记录结构


@dataclass
class SpanRecord:
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    started_at: datetime
    seq: int
    ended_at: datetime | None = None
    duration_ms: int | None = None
    status: str = "ok"
    error_category: str | None = None
    content: str | None = None
    # 归属的智能体。委派子图里的节点要靠它认领，见 docs/RUNTIME_VIEW.md §4.1。
    agent_name: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceState:
    """一条 trace 的可变状态。整棵树的 span 攒在这里，结束时一起提交。"""

    trace_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    run_id: str | None
    thread_id: str | None
    started_at: datetime
    input_text: str | None
    status: str = "ok"
    error_category: str | None = None
    intent: str | None = None
    tool_name: str | None = None
    skill_name: str | None = None
    # 一次运行的分组键。没有值可继承时等于自己的 trace_id：
    # 这样「一次运行」= 同 operation_id 的 trace 集合，第一轮不必特殊处理。
    operation_id: str | None = None
    # 哪个智能体在替用户做事。第一等公民的观测落点：没有这一列，
    # 「哪个 Agent 值得留、哪个该停」只能靠翻日志猜。
    agent_name: str | None = None
    output_text: str | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    spans: list[SpanRecord] = field(default_factory=list)
    _started_perf: float = field(default_factory=time.perf_counter)
    # seq 必须在这里单独计数：span 是「退出时才 append 到 spans」的，
    # 用 len(spans) 当序号会让同时打开的同级 span 全拿到 0。
    _next_seq: int = 0

    def alloc_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    # ---- 收尾时的汇总 ----

    @property
    def span_count(self) -> int:
        return len(self.spans)

    @property
    def llm_calls(self) -> int:
        return sum(1 for s in self.spans if s.kind == "llm")

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.spans if s.kind in ("tool", "mcp"))

    def _sum_tokens(self) -> tuple[int | None, int | None]:
        prompt = completion = None
        for s in self.spans:
            if s.kind != "llm":
                continue
            pt = s.attrs.get("prompt_tokens")
            ct = s.attrs.get("completion_tokens")
            if isinstance(pt, int):
                prompt = (prompt or 0) + pt
            if isinstance(ct, int):
                completion = (completion or 0) + ct
        return prompt, completion

    def mark(
        self,
        *,
        status: str | None = None,
        error_category: str | None = None,
        intent: str | None = None,
        tool_name: str | None = None,
        skill_name: str | None = None,
        output_text: Any = None,
    ) -> None:
        """收尾前补结论。多次调用时后写的非 None 字段覆盖前面。"""
        if status:
            self.status = status
        if error_category:
            self.error_category = error_category
        if intent:
            self.intent = intent
        if tool_name:
            self.tool_name = tool_name
        if skill_name:
            self.skill_name = skill_name
        if output_text is not None:
            self.output_text = capture(output_text)

    def begin(self) -> dict[str, Any]:
        """进入时先落的那一行：`status="running"`，汇总全零。

        与 `finish()` 成对：一个 INSERT，一个 UPDATE。为什么要提前落库，
        见 docs/RUNTIME_VIEW.md §5.1——不落这一行，「现在谁在跑」永远答不上来，
        因为整棵树要到收尾才提交。

        `operation_id` 在这里就写死（没有可继承的值时等于自己的 trace_id），
        否则运行视图在收尾前无法分组。
        """
        return {
            "type": "start",
            "tenant_id": self.tenant_id,
            "trace": {
                "trace_id": self.trace_id,
                "operation_id": self.operation_id or self.trace_id,
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "conversation_id": _as_uuid(self.conversation_id),
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "started_at": self.started_at,
                "status": "running",
                "agent_name": self.agent_name,
                "span_count": 0,
                "llm_calls": 0,
                "tool_calls": 0,
                "input_text": self.input_text,
                "attrs": dict(self.attrs),
            },
            "spans": [],
        }

    def finish(self) -> dict[str, Any]:
        """冻结成写入器需要的字典。只调用一次（在 trace 的 finally 里）。"""
        self.ended_at = datetime.now(UTC)
        self.duration_ms = int((time.perf_counter() - self._started_perf) * 1000)
        prompt_tokens, completion_tokens = self._sum_tokens()
        return {
            "type": "finish",
            "tenant_id": self.tenant_id,
            "trace": {
                "trace_id": self.trace_id,
                "operation_id": self.operation_id or self.trace_id,
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "conversation_id": _as_uuid(self.conversation_id),
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "duration_ms": self.duration_ms,
                "status": self.status,
                "error_category": self.error_category,
                "intent": self.intent,
                "tool_name": self.tool_name,
                "skill_name": self.skill_name,
                "agent_name": self.agent_name,
                "span_count": self.span_count,
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_micros": None,
                "input_text": self.input_text,
                "output_text": self.output_text,
                "attrs": dict(self.attrs),
            },
            "spans": [
                {
                    "tenant_id": self.tenant_id,
                    "trace_id": self.trace_id,
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "seq": s.seq,
                    "name": s.name,
                    "kind": s.kind,
                    "agent_name": s.agent_name,
                    "started_at": s.started_at,
                    "ended_at": s.ended_at,
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                    "error_category": s.error_category,
                    "content": s.content,
                    "attrs": dict(s.attrs),
                }
                for s in self.spans
            ],
        }


# ---------------------------------------------------------------- contextvar

_trace_var: ContextVar[TraceState | None] = ContextVar("pap_obs_trace", default=None)
_stack_var: ContextVar[tuple[SpanRecord, ...]] = ContextVar("pap_obs_stack", default=())
# 当前是谁在干活。委派进入子图前由 `bind_agent()` 改写，退出时还原，
# 于是子图里所有 span（含嵌套委派的更深一层）自动归到正确的 Agent。
_agent_var: ContextVar[str | None] = ContextVar("pap_obs_agent", default=None)


def current_trace() -> TraceState | None:
    return _trace_var.get()


def current_operation_id() -> str | None:
    """当前 trace 所属的运行。观测关闭时是 None，调用方照常传下去即可。"""
    state = _trace_var.get()
    if state is None:
        return None
    return state.operation_id or state.trace_id


def current_agent() -> str | None:
    return _agent_var.get()


def current_span() -> SpanRecord | None:
    stack = _stack_var.get()
    return stack[-1] if stack else None


def enabled() -> bool:
    """开关 + 当前是否真在一条 trace 里。

    后者很重要：Studio 入口没有 trace 上下文，节点里的 `span()` 不能凭空开 trace，
    否则会写出一堆没有用户身份的孤儿记录。
    """
    return runtime_settings.obs_enabled() and _trace_var.get() is not None


# ---------------------------------------------------------------- span 句柄


class SpanHandle:
    """`span()` 的产出物。可以在 with 体内补属性、补内容、改状态。"""

    __slots__ = ("record", "_t0")

    def __init__(self, record: SpanRecord | None = None, t0: float | None = None) -> None:
        self.record = record
        self._t0 = t0

    def add(self, **attrs: Any) -> SpanHandle:
        if self.record is not None:
            for key, value in attrs.items():
                if value is not None:
                    self.record.attrs[key] = redact(value)
        return self

    def content(self, *, input: Any = None, output: Any = None) -> SpanHandle:  # noqa: A002
        """追加输入/输出。可以分两次调用（先记输入，跑完再补输出）。

        合并而不是覆盖：调用方常见的写法是「进 span 先记 input，拿到结果再记
        output」，覆盖式会让前者白记。
        """
        if self.record is None:
            return self

        payload: dict[str, Any] = {}
        if self.record.content:
            try:
                existing = json.loads(self.record.content)
                if isinstance(existing, dict):
                    payload.update(existing)
            except Exception:
                # 之前存的不是合法 JSON（理论上不会）：直接丢弃，别让新内容跟着坏。
                payload = {}

        captured_in = capture_payload(input)
        captured_out = capture_payload(output)
        if captured_in is not None:
            payload["input"] = captured_in
        if captured_out is not None:
            payload["output"] = captured_out

        if payload:
            try:
                self.record.content = json.dumps(payload, ensure_ascii=False)
            except Exception:
                self.record.content = None
        return self

    def fail(self, category: str) -> SpanHandle:
        if self.record is not None:
            self.record.status = "error"
            self.record.error_category = category
        return self


_NOOP = SpanHandle()


def classify_error(exc: BaseException | str) -> str:
    """给异常归一个稳定的类别。列表页按它分组，所以不能直接用异常类名。"""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, str):
        return exc

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "permission" in name or "denied" in name:
        return "permission_denied"
    if "connection" in name or "network" in name:
        return "connection_error"
    return f"error:{type(exc).__name__}"


# ---------------------------------------------------------------- 公开 API


@asynccontextmanager
async def trace_run(
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    agent_name: str | None = None,
    operation_id: str | None = None,
    input_text: Any = None,
) -> AsyncIterator[TraceState | None]:
    """整轮对话的根。关闭观测时 yield None，调用方自行判空。

    `operation_id` 只在一个场景下传：审批恢复的那一轮。它从审批单上继承发起方
    的运行键，于是「发起」与「确认后执行」两条 trace 在运行视图里是同一次运行。
    不传时等于自己的 trace_id——第一次发起就是一次运行的起点。
    """
    if not runtime_settings.obs_enabled():
        yield None
        return

    trace_id = secrets.token_hex(16)
    state = TraceState(
        trace_id=trace_id,
        operation_id=operation_id or trace_id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        run_id=run_id,
        thread_id=thread_id,
        agent_name=agent_name,
        started_at=datetime.now(UTC),
        input_text=capture(input_text),
    )

    token = _trace_var.set(state)
    agent_token = _agent_var.set(agent_name or None)
    # 先落「在跑」这一行，再进业务：不这么做，跑 40 秒的一轮前 40 秒在库里不存在，
    # 运行视图只能看到已经结束的事——那正是它没用的地方（RUNTIME_VIEW.md §5.1）。
    _submit_begin(state)
    try:
        yield state
    except BaseException as exc:  # 含 CancelledError：异常退出也要落一条
        state.status = "error"
        state.error_category = state.error_category or classify_error(exc)
        raise
    finally:
        _agent_var.reset(agent_token)
        _trace_var.reset(token)
        try:
            _submit(state.finish())
        except Exception:  # 提交失败也不能影响对话
            logger.warning(
                "观测 trace 提交失败。",
                extra={"event": "obs_submit_failed", "trace_id": state.trace_id},
                exc_info=True,
            )


def _submit_begin(state: TraceState) -> None:
    """把「在跑」这一行丢进队列。失败只记日志，不上抛。

    单独一个函数而不是在 `trace_run` 里包 try：起始行提交发生在 yield 之前，
    这时抛出去就是「观测把对话带挂」，而那是本模块第三条约束明令禁止的。
    """
    try:
        _submit(state.begin())
    except Exception:
        logger.warning(
            "观测 trace 起始行提交失败。",
            extra={"event": "obs_begin_failed", "trace_id": state.trace_id},
            exc_info=True,
        )


def _submit(bundle: dict[str, Any]) -> None:
    from app.observability.writer import get_writer

    get_writer().submit(bundle)


@asynccontextmanager
async def span(
    name: str,
    kind: str = "node",
    *,
    attrs: dict[str, Any] | None = None,
) -> AsyncIterator[SpanHandle]:
    """一个 span。无 trace 上下文或开关关闭时是空转的哑对象。"""
    state = _trace_var.get()

    if state is None or not runtime_settings.obs_enabled():
        yield _NOOP
        return

    stack = _stack_var.get()
    record = SpanRecord(
        span_id=secrets.token_hex(8),
        parent_span_id=stack[-1].span_id if stack else None,
        name=name[:64],
        kind=kind,
        started_at=datetime.now(UTC),
        seq=state.alloc_seq(),
        agent_name=_agent_var.get(),
        attrs=dict(attrs or {}),
    )
    handle = SpanHandle(record, time.perf_counter())

    token = _stack_var.set(stack + (record,))
    try:
        yield handle
    except BaseException as exc:
        record.status = "error"
        record.error_category = record.error_category or classify_error(exc)
        raise
    finally:
        _stack_var.reset(token)
        record.ended_at = datetime.now(UTC)
        record.duration_ms = (
            int((time.perf_counter() - handle._t0) * 1000) if handle._t0 is not None else 0
        )
        state.spans.append(record)


def record_llm_usage(message: Any) -> dict[str, Any]:
    """从 AIMessage 抽 token 与模型名。抽不到就返回空 dict，不猜。"""
    out: dict[str, Any] = {}
    if message is None:
        return out

    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        if isinstance(usage.get("input_tokens"), int):
            out["prompt_tokens"] = usage["input_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            out["completion_tokens"] = usage["output_tokens"]

    metadata = getattr(message, "response_metadata", None)
    if isinstance(metadata, dict):
        model = metadata.get("model_name") or metadata.get("model")
        if model:
            out["model"] = str(model)[:64]
        finish = metadata.get("finish_reason")
        if finish:
            out["finish_reason"] = str(finish)[:32]

    return out


@contextmanager
def bind_agent(name: str | None) -> Iterator[None]:
    """声明「接下来这段是某个智能体在干活」。

    委派进入子图前调用，退出时还原，所以嵌套 `A→B→C` 的归属天然正确：
    C 的 span 归 C，回到 B 时又归 B。观测关闭时什么都不做——
    这里不判 `enabled()`，因为 contextvar 本身没有开销，而判空会让
    「关掉之后又打开」的边界情况多一处分支。
    """
    token = _agent_var.set(name or None)
    try:
        yield
    finally:
        _agent_var.reset(token)


@contextmanager
def bind_trace_attrs(**attrs: Any) -> Iterator[None]:
    """给当前 trace 补静态属性（如 attachment 数量）。无 trace 时什么都不做。"""
    state = _trace_var.get()
    if state is not None:
        for key, value in attrs.items():
            if value is not None:
                state.attrs[key] = redact(value)
    yield
