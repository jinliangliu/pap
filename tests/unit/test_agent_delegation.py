# tests/unit/test_agent_delegation.py
"""委派：子集判定、纪律上限、子调用上下文。

这个模块最该守住的不是「能转交」，而是**不能转交**的那几条：
- 只读的 A 不能转交给可写的 B（`can_write` 是一整类风险，不是名单里的几个名字）；
- B 显式声明的工具不能超出 A 的清单；
- 环、深度、次数三条纪律各自有独立理由，混成一个「委派失败」就再也分不开。

其余用例守的是「传什么」：`user_id` 必须继承发起人（否则就是给 B 发凭据），
`agent_*` 必须是 B 自己的（否则根本没发生委派），附件不过桥。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.audit import AuditEventType, AuditLog
from app.services import agent_service
from app.services.agent_service import AgentBoundary
from app.tenancy.context import tenant_scope
from app.tools.builtins import delegate_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"


@pytest.fixture(autouse=True)
async def _clean_agents(session, seeded_identity):
    await session.execute(delete(Agent))
    await session.commit()
    yield
    await session.execute(delete(Agent))
    await session.commit()


def _boundary(**over) -> AgentBoundary:
    base = dict(agent_name="a", allowed_tools=None, can_write=True)
    base.update(over)
    return AgentBoundary(**base)


def _ctx(**over) -> ToolExecutionContext:
    base = dict(
        tenant_id=TENANT,
        user_id="u-1",
        thread_id="t-1",
        run_id="r-1",
        request_id="req-1",
        roles=("tester",),
        scopes=("tools:read",),
        agent_name="a",
        agent_revision=2,
        agent_allowed_tools=None,
        agent_can_write=True,
    )
    base.update(over)
    return ToolExecutionContext(**base)


async def _seed(name: str, **kwargs) -> str:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            row = await agent_service.create_agent(
                db,
                tenant_id=TENANT,
                name=name,
                display_name=kwargs.pop("display_name", name),
                status=kwargs.pop("status", AgentStatus.ACTIVE),
                **kwargs,
            )
            await db.commit()
            return str(row.id)


async def _audits(resource_id: str) -> list[AuditLog]:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = await db.scalars(
                select(AuditLog).where(
                    AuditLog.event_type == AuditEventType.AGENT_DELEGATED,
                    AuditLog.resource_id == resource_id,
                )
            )
            return list(rows.all())


# ---------------- 子集判定 ----------------


def test_subset_rejects_tool_outside_parent():
    ok, reason = agent_service.agent_subset(
        _boundary(allowed_tools=("t1", "t3")),
        _boundary(allowed_tools=("t1", "t2")),
    )
    assert ok is False
    # 理由必须点名是哪个工具，否则排障只能看到「委派失败」。
    assert reason == "allowed_tools:t3"


def test_subset_rejects_write_when_parent_is_readonly():
    ok, reason = agent_service.agent_subset(
        _boundary(can_write=True, allowed_tools=("t1",)),
        _boundary(can_write=False, allowed_tools=("t1",)),
    )
    assert ok is False
    assert reason == "can_write"


def test_subset_allows_any_child_when_parent_has_no_allowlist():
    ok, _ = agent_service.agent_subset(
        _boundary(allowed_tools=("t9",)),
        _boundary(allowed_tools=None),
    )
    assert ok is True


def test_subset_allows_child_without_own_allowlist():
    """B 没配清单不等于越权：它用的是自己的全量配置，不是从 A 借的能力。

    把它当成越权是常见误解，代价是每建一个 Agent 都要把父清单抄一遍——
    抄漏一个才是真的越权。
    """
    ok, _ = agent_service.agent_subset(
        _boundary(allowed_tools=None),
        _boundary(allowed_tools=("t1",)),
    )
    assert ok is True


def test_subset_checks_skills():
    ok, reason = agent_service.agent_subset(
        _boundary(allowed_skills=("s1", "s2")),
        _boundary(allowed_skills=("s1",)),
    )
    assert ok is False
    assert reason == "allowed_skills:s2"


def test_describe_violation_names_the_item():
    assert "t3" in agent_service.describe_violation("allowed_tools:t3")
    assert "只读" in agent_service.describe_violation("can_write")


# ---------------- 纪律上限 ----------------


async def test_delegate_rejects_cycle():
    await _seed("ops")
    result = await delegate_tools.delegate_to_agent(
        _ctx(delegation_path=("ops",)),
        {"target": "ops", "task": "看一下 3 号泵"},
    )
    assert result.success is False
    assert result.error_code == "DELEGATE_CYCLE"


async def test_delegate_rejects_self():
    result = await delegate_tools.delegate_to_agent(
        _ctx(agent_name="ops"),
        {"target": "ops", "task": "看一下 3 号泵"},
    )
    assert result.error_code == "DELEGATE_CYCLE"


async def test_delegate_rejects_depth():
    """链条长度 = 已经进入过的 Agent。3 个之后再转交就是第 4 跳，拒绝。"""
    await _seed("ops")
    result = await delegate_tools.delegate_to_agent(
        _ctx(delegation_path=("a", "b", "c")),
        {"target": "ops", "task": "看一眼"},
    )
    assert result.success is False
    assert result.error_code == "DELEGATE_DEPTH_EXCEEDED"


async def test_delegate_allows_up_to_two_hops(monkeypatch):
    """A→B→C 是允许的：第三跳才超限。上限写错会让正常链路一起被拒。

    子调用给一条工具痕迹：P1 起「毫无依据的结论」会被单独拦下
    （`DELEGATE_NO_EVIDENCE`），那属于另一条判定——混进来会让这条用例
    在边界调宽时也保持绿色，等于没测。
    """
    await _seed("ops")
    monkeypatch.setattr(
        "app.agents.graph.get_agent_graph",
        lambda: _FakeGraph(
            {
                "final_response": "好了。",
                "tool_observations": [{"tool_name": "get_platform_time", "success": True}],
            }
        ),
    )

    ok = await delegate_tools.delegate_to_agent(
        _ctx(delegation_path=("a", "b"), agent_name="b"),
        {"target": "ops", "task": "看一眼"},
    )
    assert ok.error_code != "DELEGATE_DEPTH_EXCEEDED"
    assert ok.success is True


async def test_delegate_rejects_budget():
    await _seed("ops")
    result = await delegate_tools.delegate_to_agent(
        _ctx(delegation_count=delegate_tools.MAX_PER_TURN),
        {"target": "ops", "task": "看一眼"},
    )
    assert result.error_code == "DELEGATE_BUDGET_EXHAUSTED"


async def test_caller_can_forbid_delegating_at_all():
    """`max_delegation_depth=0` = 这个智能体不转交。

    0 是配置，不是「没配」。实现里若用 `x or DEFAULT` 折配置，这个开关
    会静默变回默认的 2 跳，而界面上明明写着 0。
    """
    await _seed("ops")
    result = await delegate_tools.delegate_to_agent(
        _ctx(agent_max_delegation_depth=0),
        {"target": "ops", "task": "看一眼"},
    )
    assert result.error_code == "DELEGATE_DEPTH_EXCEEDED"
    assert "不转交" in result.message


async def test_caller_budget_zero_blocks_the_first_attempt():
    await _seed("ops")
    result = await delegate_tools.delegate_to_agent(
        _ctx(agent_delegation_budget=0),
        {"target": "ops", "task": "看一眼"},
    )
    assert result.error_code == "DELEGATE_BUDGET_EXHAUSTED"
    assert "不转交" in result.message


async def test_upstream_limit_cannot_be_widened_downstream():
    """上游已收到 1 跳，这一环配了 5 也不能放行。

    否则链中间的某个 Agent 单方面就把整条链拉长了——而发起方从来没有
    同意过这个深度。这条测的是「取 min」有没有真的落到判定上。
    """
    from app.services.delegation_limits import DelegationLimits

    await _seed("ops")
    result = await delegate_tools.delegate_to_agent(
        _ctx(
            delegation_path=("a", "b"),
            delegation_limits=DelegationLimits(max_depth=1, max_per_turn=3),
            agent_max_delegation_depth=5,
        ),
        {"target": "ops", "task": "看一眼"},
    )
    assert result.error_code == "DELEGATE_DEPTH_EXCEEDED"
    # 拒绝理由要说出生效的那个数，不是这一环配的那个数。
    assert "最多 1 跳" in result.message


async def test_depth_refusal_records_the_effective_limits():
    from app.services.delegation_limits import DelegationLimits

    await _seed("ops")
    await delegate_tools.delegate_to_agent(
        _ctx(
            delegation_path=("a", "b"),
            delegation_limits=DelegationLimits(max_depth=1, max_per_turn=3),
        ),
        {"target": "ops", "task": "看一眼"},
    )
    audits = await _audits("ops")
    metadata = audits[-1].metadata_json
    assert metadata["reject_reason"] == "depth"
    # 不记这一格的话，「我明明配了 2 跳为什么被拒」只能靠猜。
    assert metadata["limits"]["max_depth"] == 1


async def test_child_state_carries_the_narrowed_limits(monkeypatch):
    """子调用要拿到折过的上限与目标自己的配置，两样缺一不可。

    - 只有 `delegation_limits`：B 自己配的「不转交」进不来；
    - 只有 `agent_max_delegation_depth`：A 收窄到 1 这件事在 B 那一跳丢了。
    """
    await _seed("ops", max_delegation_depth=1, delegation_budget=2)
    fake = _FakeGraph(
        {
            "final_response": "好了。",
            "tool_observations": [{"tool_name": "get_platform_time", "success": True}],
        }
    )
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    await delegate_tools.delegate_to_agent(
        _ctx(agent_max_delegation_depth=1),
        {"target": "ops", "task": "看一眼"},
    )

    child, _ = fake.calls[0]
    assert child["delegation_limits"] == {"max_depth": 1, "max_per_turn": 3}
    assert child["agent_max_delegation_depth"] == 1
    assert child["agent_delegation_budget"] == 2


async def test_delegate_rejects_unknown_target():
    result = await delegate_tools.delegate_to_agent(
        _ctx(), {"target": "nobody", "task": "看一眼"}
    )
    assert result.error_code == "AGENT_UNAVAILABLE"


async def test_delegate_rejects_disabled_target():
    await _seed("ops", status=AgentStatus.DISABLED)
    result = await delegate_tools.delegate_to_agent(
        _ctx(), {"target": "ops", "task": "看一眼"}
    )
    assert result.error_code == "AGENT_UNAVAILABLE"
    audits = await _audits("ops")
    assert audits and audits[-1].success is False
    assert audits[-1].metadata_json["reject_reason"] == "未启用"


async def test_delegate_rejects_default_agent():
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            await agent_service.ensure_default(db, tenant_id=TENANT)
            await db.commit()

    result = await delegate_tools.delegate_to_agent(
        _ctx(), {"target": agent_service.DEFAULT_AGENT_NAME, "task": "看一眼"}
    )
    assert result.error_code == "AGENT_UNAVAILABLE"
    assert "默认" in (result.message or "")


async def test_delegate_rejects_wider_target_and_records_reason():
    """A 只给 t1，B 要 t1+t2 —— 必须拒，且审计里写清是哪一项。"""
    await _seed("ops", allowed_tools=["get_platform_time", "search_knowledge"])

    result = await delegate_tools.delegate_to_agent(
        _ctx(agent_allowed_tools=("get_platform_time",)),
        {"target": "ops", "task": "检索一下应急规程"},
    )

    assert result.success is False
    assert result.error_code == "DELEGATE_WIDENS"
    assert "search_knowledge" in (result.result or {}).get("violation", "")

    audits = await _audits("ops")
    assert audits, "越权尝试没有落审计"
    assert audits[-1].metadata_json["reject_reason"] == "allowed_tools:search_knowledge"


# ---------------- 子调用传了什么 ----------------


class _FakeGraph:
    """只记录子调用收到了什么，不跑真实执行图。"""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, state, config):  # noqa: ANN001
        self.calls.append((state, config))
        return self.state


async def test_delegate_child_inherits_user_but_uses_target_boundary(monkeypatch):
    await _seed("ops", allowed_tools=["get_platform_time"], can_write=True)

    fake = _FakeGraph(
        {
            "final_response": "3 号泵昨天 14:20 跳停。",
            "citations": [
                {
                    "document_id": "doc-1",
                    "title": "应急规程",
                    "section": "4.2",
                    "updated_at": "2026-11-01",
                }
            ],
            "tool_observations": [{"tool_name": "get_platform_time", "success": True}],
            "missing_fields": ["现场阀位反馈"],
            "error_category": None,
        }
    )
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(agent_allowed_tools=("get_platform_time",), agent_can_write=True),
        {"target": "ops", "task": "查 3 号泵昨天的跳停原因", "context": "用户问的是压力异常"},
    )

    assert result.success is True
    child, config = fake.calls[0]

    # 身份继承发起人，边界用目标自己的。
    assert child["user_id"] == "u-1"
    assert child["scopes"] == ["tools:read"]
    assert child["agent_name"] == "ops"
    assert child["agent_allowed_tools"] == ["get_platform_time"]
    # P2：子调用不再一律只读，跟随目标自己的 `can_write`。
    # 它不会变成越权通道——`can_write` 的子集校验已经保证「B 可写 ⇒ A 可写」，
    # 上面这条用例（`test_delegate_rejects_writable_target_from_readonly_parent`）
    # 守的正是另一半。真正执行写操作时仍然走 B 自己的审批。
    assert child["agent_can_write"] is True
    # 上游链带上发起方：B 开出的审批单要能回答「谁把它叫来的」。
    assert child["delegation_ancestors"] == [{"id": None, "name": "a"}]
    # 写操作的结论要回到用户看得见的会话，而不是这条一次性的子线程。
    assert child["delegation_origin_thread"] == "t-1"
    # 上下文不过桥：只带任务与背景，不带消息历史与附件。
    assert child["attachments"] == []
    assert child["run_id"] == ""
    assert child["delegation_path"] == ["a", "ops"]
    # 子调用必须是独立 thread，否则会写进父级 checkpoint。
    assert config["configurable"]["thread_id"] != "t-1"
    assert config["configurable"]["thread_id"].startswith("t-1:d1:ops:")

    envelope = result.result
    assert envelope["agent"] == "ops"
    assert envelope["status"] == "completed"
    assert envelope["readonly"] is False
    assert envelope["evidence"][0]["id"] == "doc-1"
    assert envelope["unknown"] == ["现场阀位反馈"]
    # 对方的话不是已核验的事实。
    assert result.verified is False


async def test_readonly_target_still_reports_readonly_to_caller(monkeypatch):
    """反向：B 只读时，信封里必须说清「它做不了是因为这一层限制」。

    这条和上一条是一对儿。只测一边的话，把 `readonly` 写死成 `False`
    （或写死成 `True`）两边都会有一半用例是绿的，而线上表现完全不同：
    说成可写，发起方会把「我的职责不允许」转述成「系统坏了」。
    """
    await _seed("ops", allowed_tools=["get_platform_time"], can_write=False)

    fake = _FakeGraph(
        {
            "final_response": "我不能改工单，只读。",
            "citations": [],
            "tool_observations": [{"tool_name": "get_platform_time", "success": True}],
            "missing_fields": [],
            "error_category": None,
        }
    )
    monkeypatch.setattr("app.agents.graph.get_agent_graph", lambda: fake)

    result = await delegate_tools.delegate_to_agent(
        _ctx(agent_allowed_tools=("get_platform_time",), agent_can_write=False),
        {"target": "ops", "task": "把工单关掉"},
    )

    assert result.success is True
    child, _ = fake.calls[0]
    assert child["agent_can_write"] is False
    assert result.result["readonly"] is True


async def test_delegate_records_audit_with_hash_not_body(monkeypatch):
    await _seed("ops")
    monkeypatch.setattr(
        "app.agents.graph.get_agent_graph",
        lambda: _FakeGraph(
            {
                "final_response": "好了。",
                "citations": [],
                # 有依据才走成功分支：否则落的是 `no_evidence` 那条审计，
                # 这条用例就变成在测「被拒也记账」，而不是「成功记账」。
                "tool_observations": [{"tool_name": "get_platform_time", "success": True}],
            }
        ),
    )

    await delegate_tools.delegate_to_agent(
        _ctx(), {"target": "ops", "task": "查一下 T002509008 昨天下午的压力异常"}
    )

    audits = await _audits("ops")
    assert audits
    metadata = audits[-1].metadata_json
    assert metadata["status"] == "completed"
    assert metadata["from_agent"] == "a"
    assert metadata["depth"] == 1
    assert metadata["task_chars"] == len("查一下 T002509008 昨天下午的压力异常")
    # 任务原文里有业务细节，审计只留哈希。
    assert "T002509008" not in str(metadata)
    assert len(metadata["task_sha256"]) == 16


# ---------------- 观测里的归属 ----------------


@pytest.fixture
def obs_bundles(monkeypatch):
    """打开观测并把 trace 拦在内存里。"""
    from app.core.config import settings
    from app.observability import context as obs_context

    boxes: list[dict] = []
    monkeypatch.setattr(obs_context, "_submit", lambda bundle: boxes.append(bundle))
    monkeypatch.setattr(settings, "obs_enabled", True)
    return boxes


async def test_delegated_span_is_attributed_to_the_callee(monkeypatch, obs_bundles):
    """委派那一层 span 必须归被委派方。

    这条测的是 `delegate_tools` 有没有真的把 `bind_agent` 套上去——
    只测 `bind_agent` 本身是不够的：把它从工具里删掉，那个用例照样绿，
    而拓扑图会把每一条边都指回发起方。
    """
    from app.observability.context import trace_run

    await _seed("ops", allowed_tools=["get_platform_time"], can_write=True)
    monkeypatch.setattr(
        "app.agents.graph.get_agent_graph",
        lambda: _FakeGraph(
            {
                "final_response": "好了。",
                "citations": [],
                "tool_observations": [{"tool_name": "get_platform_time", "success": True}],
            }
        ),
    )

    # 发起方是 a：trace 记它，但 agent.ops 这一层与它下面的节点都该归 ops。
    async with trace_run(tenant_id=TENANT, user_id="u-1", agent_name="a"):
        await delegate_tools.delegate_to_agent(
            _ctx(), {"target": "ops", "task": "查一下 T002509008 昨天下午的压力异常"}
        )

    bundle = obs_bundles[-1]
    agent_spans = [row for row in bundle["spans"] if row["kind"] == "agent"]
    assert agent_spans, "委派没有留下 agent span"
    assert agent_spans[0]["name"] == "agent.ops"
    assert agent_spans[0]["agent_name"] == "ops"

    # 发起方不能被一起改掉：trace 仍是 a 的，只有委派那一段换了归属。
    # 桩图不跑真实节点，所以这里只能查 trace 级；有真实子图时
    # `test_runtime_view` 覆盖了「子节点归 B、A 自己的节点仍归 A」。
    assert bundle["trace"]["agent_name"] == "a"
