# tests/unit/test_mcp_server.py
"""入向 MCP 通道：鉴权门槛、状态折叠、返回体契约。

这里守的是三件不会抛异常、只会悄悄出错的事：

1. 没有令牌却放行。它不会报错，只会让整个平台变成匿名接口。
2. `needs_input` 被折成 `completed`。调用方会把一句反问当结论转述出去。
3. 超时之后拿不到 task_id。任务照跑，但调用方永远不知道去问谁要结果。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.mcp_server import auth, tools
from app.models.conversation import RunStatus

# ---------------- 令牌 ----------------


def test_bearer_value_parsing():
    assert auth._bearer_value("Bearer abc123") == "abc123"
    assert auth._bearer_value("bearer abc123") == "abc123"
    assert auth._bearer_value("Bearer  spaced ") == "spaced"
    # 非 Bearer 方案一律不认：Basic 凭证不能当成令牌用。
    assert auth._bearer_value("Basic abc123") is None
    assert auth._bearer_value("abc123") is None
    assert auth._bearer_value("Bearer ") is None
    assert auth._bearer_value(None) is None


class _Recorder:
    """最小的下游 ASGI 应用：只记录有没有被调用、看到了什么身份。"""

    def __init__(self) -> None:
        self.called = False
        self.identity = None

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.identity = (scope.get("state") or {}).get(auth.STATE_KEY)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


async def _call_gate(gate, path: str, token: str | None) -> int:
    scope = {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": (
            [(b"authorization", f"Bearer {token}".encode())] if token is not None else []
        ),
    }
    captured: dict[str, int] = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await gate(scope, receive, send)
    return captured["status"]


async def test_gate_rejects_without_token(monkeypatch):
    monkeypatch.setattr(auth.settings, "mcp_inbound_token", "")
    downstream = _Recorder()
    gate = auth.BearerGate(downstream)

    assert await _call_gate(gate, "/mcp", None) == 401
    assert downstream.called is False


async def test_gate_rejects_unknown_token(monkeypatch, seeded_identity):
    """两张表都查不到就 401。这条挡的是「静态令牌没配、于是放行」。"""
    monkeypatch.setattr(auth.settings, "mcp_inbound_token", "")
    downstream = _Recorder()
    gate = auth.BearerGate(downstream)

    assert await _call_gate(gate, "/mcp", "pap_not-a-real-token") == 401
    assert downstream.called is False


async def test_gate_passes_with_static_token_and_stashes_identity(
    monkeypatch, seeded_identity
):
    """静态兜底令牌仍可用，并且身份要在门槛里就解析好。

    身份放在 scope state 里而不是留给工具再查一次：无效令牌必须拿到 401，
    如果只在工具里校验，客户端看到的是「工具调用失败」。
    """
    monkeypatch.setattr(auth.settings, "mcp_inbound_token", "abc123")
    monkeypatch.setattr(auth.settings, "mcp_inbound_subject", seeded_identity["subject"])
    monkeypatch.setattr(auth.settings, "mcp_inbound_tenant", seeded_identity["tenant_id"])

    downstream = _Recorder()
    gate = auth.BearerGate(downstream)

    assert await _call_gate(gate, "/mcp", "abc123") == 200
    assert downstream.called is True
    assert downstream.identity is not None
    assert downstream.identity.subject == seeded_identity["subject"]


async def test_gate_404s_other_paths(monkeypatch):
    """挂在根路径上，所以非 MCP 路径必须在这里终止，不能转发给会话管理器。"""
    monkeypatch.setattr(auth.settings, "mcp_inbound_token", "abc123")
    downstream = _Recorder()
    gate = auth.BearerGate(downstream)

    assert await _call_gate(gate, "/api/v1/chat", "abc123") == 404
    assert downstream.called is False


# ---------------- 状态折叠 ----------------


def test_response_status_separates_clarify_from_completed():
    from app.services.chat_service import _response_status

    assert _response_status({"intent": "chat"}) == "completed"
    assert _response_status({"intent": "read_tool"}) == "completed"
    assert _response_status({"intent": "clarify"}) == "needs_input"
    assert _response_status({"intent": "error"}) == "error"


@pytest.mark.parametrize(
    ("run_status", "expected"),
    [
        (RunStatus.PENDING, "running"),
        (RunStatus.RUNNING, "running"),
        (RunStatus.WAITING_FOR_APPROVAL, "awaiting_approval"),
        (RunStatus.COMPLETED, "completed"),
        (RunStatus.FAILED, "failed"),
        (RunStatus.CANCELLED, "failed"),
    ],
)
def test_status_of(run_status, expected):
    assert tools._status_of(SimpleNamespace(status=run_status)) == expected


# ---------------- 返回体 ----------------


def test_shape_carries_hint_per_status():
    """每个非 completed 状态都必须带一句「接下来怎么办」。

    没有 hint 的话，调用方拿到 `status=running` 只会当成失败重试一次，
    而重试的代价是再跑一遍完整的一轮对话。
    """
    for status in ("completed", "needs_input", "awaiting_approval", "running", "failed"):
        payload = json.loads(
            tools._shape(task_id="t", status=status, conversation_id="c")
        )
        assert payload["hint"], status


def test_questions_from_extracts_labels_only():
    metadata = {
        "clarify": {
            "items": [
                {
                    "question": "涉及哪个站点？",
                    "options": [{"label": "station-001"}, {"label": ""}, "脏数据"],
                },
                {"question": "", "options": []},
                "不是字典",
            ]
        }
    }

    questions = tools._questions_from(metadata)

    assert questions == [{"question": "涉及哪个站点？", "options": ["station-001"]}]


def test_questions_from_tolerates_missing_metadata():
    assert tools._questions_from({}) == []
    assert tools._questions_from(None) == []


# ---------------- 委派 ----------------


def _identity():
    return SimpleNamespace(
        tenant_id="default",
        user_id="00000000-0000-0000-0000-000000000001",
        display_name="检查用",
        roles=(),
        scopes=(),
    )


async def test_delegate_rejects_empty_task_without_touching_services():
    """空任务必须在进图之前挡掉：跑一轮空对话会白扣配额、还会留下空会话。"""
    payload = json.loads(
        await tools.delegate(identity=_identity(), task="   ")
    )

    assert payload["status"] == "failed"
    assert "task" in payload["error"]


async def test_delegate_rejects_bad_conversation_id():
    payload = json.loads(
        await tools.delegate(
            identity=_identity(),
            task="查一下西环站",
            conversation_id="非法 id 带空格",
        )
    )

    assert payload["status"] == "failed"
    assert payload["conversation_id"] == "非法 id 带空格"


async def test_delegate_returns_needs_input_with_questions(monkeypatch):
    from app.schemas.chat import ChatResponse

    async def fake_boundary(_identity):
        return SimpleNamespace(agent_name="assistant")

    async def fake_turn(**_kwargs):
        return ChatResponse(
            thread_id="mcp-abc",
            run_id="11111111-1111-1111-1111-111111111111",
            status="needs_input",
            answer="还差一个站点编号。",
            metadata={
                "intent": "clarify",
                "clarify": {
                    "items": [
                        {
                            "question": "涉及哪个站点？",
                            "options": [{"label": "station-001"}],
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(tools, "_boundary", fake_boundary)
    monkeypatch.setattr(tools, "_run_turn", fake_turn)

    payload = json.loads(
        await tools.delegate(
            identity=_identity(),
            task="帮我看看那个站的情况",
            conversation_id="mcp-abc",
        )
    )

    assert payload["status"] == "needs_input"
    assert payload["questions"][0]["question"] == "涉及哪个站点？"
    assert payload["trace"]["intent"] == "clarify"
    assert payload["task_id"] == "11111111-1111-1111-1111-111111111111"


async def test_delegate_hands_off_task_id_when_slow(monkeypatch):
    """超时必须返回句柄，而不是把请求挂到客户端自己超时。

    这条是 30 秒上限的全部意义所在：客户端先死，用户看到的是断连；
    交出 task_id 之后，慢的那一轮仍在跑，结论会被 pap_task_status 取回。
    """
    started = asyncio.Event()

    async def fake_boundary(_identity):
        return SimpleNamespace(agent_name="assistant")

    async def slow_turn(**_kwargs):
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("不该跑完")

    row = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        thread_id="default:uid:mcp-slow",
        tenant_id="default",
        user_id="uid",
        started_at=None,
        conversation_id=None,
        status=RunStatus.RUNNING,
        intent=None,
        tool_name=None,
        approval_id=None,
        error_category=None,
        output_summary=None,
    )

    async def fake_lookup(**_kwargs):
        return row

    monkeypatch.setattr(tools, "_boundary", fake_boundary)
    monkeypatch.setattr(tools, "_run_turn", slow_turn)
    monkeypatch.setattr(tools, "_run_by_thread", fake_lookup)
    monkeypatch.setattr(tools.settings, "mcp_inbound_wait_seconds", 0.5)

    payload = json.loads(
        await tools.delegate(
            identity=_identity(),
            task="跑一份很慢的巡检报告",
            conversation_id="mcp-slow",
            wait_seconds=0.5,
        )
    )

    assert started.is_set()
    assert payload["status"] == "running"
    assert payload["task_id"] == "22222222-2222-2222-2222-222222222222"


async def test_delegate_folds_rejected_into_failed(monkeypatch):
    """`rejected` 是入口拒绝（附件不合格、配额用尽），对委派方与 failed 同义。

    工具契约只有五个状态；多透出一个取值，调用方就得写一个它永远走不到的分支。
    """
    from app.schemas.chat import ChatResponse

    async def fake_boundary(_identity):
        return SimpleNamespace(agent_name="assistant")

    async def fake_turn(**_kwargs):
        return ChatResponse(
            thread_id="mcp-x",
            run_id="",
            status="rejected",
            answer="附件无法接收：格式不支持。",
            error_category="invalid_attachment",
        )

    monkeypatch.setattr(tools, "_boundary", fake_boundary)
    monkeypatch.setattr(tools, "_run_turn", fake_turn)

    payload = json.loads(
        await tools.delegate(identity=_identity(), task="看下这个附件")
    )

    assert payload["status"] == "failed"
    assert payload["error"] == "invalid_attachment"
    assert "附件无法接收" in payload["answer"]


async def test_delegate_folds_error_into_failed(monkeypatch):
    """`ChatResponse.status="error"` 不在对外五个状态里，必须折成 failed。

    真实故障是 `_response_status` 会返回 "error"，而 `_shape` 只折了
    "rejected"，于是 MCP 客户端收到一个契约里根本没有的取值。
    """
    from app.schemas.chat import ChatResponse

    async def fake_boundary(_identity):
        return SimpleNamespace(agent_name="assistant")

    async def fake_turn(**_kwargs):
        return ChatResponse(
            thread_id="mcp-x",
            run_id="",
            status="error",
            answer="当前无法生成有效回复。",
            error_category="chat_timeout",
        )

    monkeypatch.setattr(tools, "_boundary", fake_boundary)
    monkeypatch.setattr(tools, "_run_turn", fake_turn)

    payload = json.loads(
        await tools.delegate(identity=_identity(), task="查下报警")
    )

    assert payload["status"] == "failed"
    assert payload["error"] == "chat_timeout"
    assert "tool_error" not in payload


async def test_delegate_demotes_tool_error_when_completed(monkeypatch):
    """办完了但工具失败：状态仍 completed，错误降级到 tool_error。

    第一次真实委派就撞上这个：`status=completed` 配 `error=MCP_TOOL_ERROR`，
    上层按 hint 说「PAP 已经办完」，实际一个字节都没查到。
    """
    from app.schemas.chat import ChatResponse

    async def fake_boundary(_identity):
        return SimpleNamespace(agent_name="assistant")

    async def fake_turn(**_kwargs):
        return ChatResponse(
            thread_id="mcp-x",
            run_id="7d4cd37c",
            status="completed",
            answer="远端拒绝了本次调用：授权目录中没有匹配的站点。",
            error_category="MCP_TOOL_ERROR",
        )

    monkeypatch.setattr(tools, "_boundary", fake_boundary)
    monkeypatch.setattr(tools, "_run_turn", fake_turn)

    payload = json.loads(
        await tools.delegate(identity=_identity(), task="查下 station-001 的报警")
    )

    assert payload["status"] == "completed"
    assert payload["tool_error"] == "MCP_TOOL_ERROR"
    # 顶层 error 只留给「任务没办成」，这里绝不能出现。
    assert "error" not in payload
    assert payload["hint"] == tools._HINT_COMPLETED_NO_DATA
    assert "没能拿到数据" in payload["hint"]


def test_outcome_routes_error_by_status():
    assert tools._outcome("failed", "chat_timeout") == {"error": "chat_timeout"}
    assert tools._outcome("completed", "MCP_TOOL_ERROR") == {
        "tool_error": "MCP_TOOL_ERROR"
    }
    assert tools._outcome("needs_input", "MCP_TOOL_ERROR") == {
        "tool_error": "MCP_TOOL_ERROR"
    }
    # 没有错误就没有字段，不要塞一个 None 进去。
    assert tools._outcome("completed", None) == {}


def test_status_aliases_stay_inside_the_declared_contract():
    declared = {"completed", "needs_input", "awaiting_approval", "running", "failed"}
    assert set(tools._STATUS_ALIASES.values()) <= declared
    assert set(tools._HINTS) == declared


async def test_task_status_rejects_bad_id():
    payload = json.loads(await tools.task_status(identity=_identity(), task_id="nope"))

    assert payload["status"] == "failed"
    assert "task_id" in payload["error"]


# ---------------- 能力声明 ----------------


def test_capability_boundary_only_narrows():
    from app.services.agent_service import AgentBoundary

    writable = AgentBoundary(agent_name="assistant", can_write=True)
    narrowed = tools._capability_boundary(writable, allow_write=False)

    assert narrowed.can_write is False
    # 只改这一位，别的什么都不动：收窄能力不是换一个智能体。
    assert narrowed.agent_name == "assistant"


def test_capability_boundary_never_widens():
    """**这是整个开关的安全边界。**

    调用方只能放弃写能力，不能凭参数取得它。若这里写反了（`allow_write=True`
    时把 can_write 改成 True），MCP 就成了一条绕过智能体权限配置的通道——
    而表面上一切正常：调用成功、有结论、只有审计里那一行写权限对不上。
    """
    from app.services.agent_service import AgentBoundary

    readonly = AgentBoundary(agent_name="field-ops", can_write=False)

    assert tools._capability_boundary(readonly, allow_write=True).can_write is False
    assert tools._capability_boundary(readonly, allow_write=False).can_write is False


def test_shape_marks_read_only_only_when_narrowed():
    plain = json.loads(
        tools._shape(task_id=None, status="running", conversation_id="mcp-abc")
    )
    # 没被收窄就不该出现这个字段：多一个恒为 false 的键，调用方还得判断它有没有意义。
    assert "read_only" not in plain

    narrowed = json.loads(
        tools._shape(
            task_id=None, status="running", conversation_id="mcp-abc", read_only=True
        )
    )
    assert narrowed["read_only"] is True


async def test_delegate_wires_allow_write_into_payload(monkeypatch):
    """开关必须真的走到返回体上，而不只是躺在函数签名里。"""
    from app.schemas.chat import ChatResponse
    from app.services.agent_service import AgentBoundary

    seen: dict[str, object] = {}

    async def fake_boundary(_identity):
        return AgentBoundary(agent_name="assistant", can_write=True)

    async def fake_turn(**kwargs):
        seen["boundary"] = kwargs.get("boundary")
        return ChatResponse(
            thread_id="mcp-abc",
            run_id="11111111-1111-1111-1111-111111111111",
            status="completed",
            answer="查完了。",
        )

    monkeypatch.setattr(tools, "_boundary", fake_boundary)
    monkeypatch.setattr(tools, "_run_turn", fake_turn)

    payload = json.loads(
        await tools.delegate(
            identity=_identity(), task="查一下西环站的状态", allow_write=False
        )
    )

    assert getattr(seen["boundary"], "can_write") is False
    assert payload["read_only"] is True
    assert payload["status"] == "completed"


async def test_tools_declare_capabilities():
    """注解是给宿主的：它据此决定要不要在自动执行前提醒用户。"""
    from app.mcp_server.server import get_server, reset_for_tests

    reset_for_tests()
    try:
        listed = {tool.name: tool for tool in await get_server().list_tools()}
    finally:
        reset_for_tests()

    delegate = listed["pap_delegate"]
    assert delegate.annotations is not None
    # PAP 侧可能建单，宿主必须知道这不是一次只读查询。
    assert delegate.annotations.read_only_hint is False
    assert delegate.annotations.destructive_hint is False
    assert delegate.annotations.idempotent_hint is False
    assert delegate.annotations.open_world_hint is True
    assert delegate.title

    status = listed["pap_task_status"]
    assert status.annotations is not None
    assert status.annotations.read_only_hint is True
    assert status.annotations.idempotent_hint is True
    assert status.title
