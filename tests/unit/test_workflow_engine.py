# tests/unit/test_workflow_engine.py
"""解释器与图的单测：不碰数据库、不碰模型。

`invoke` 是注入的，所以这里可以在毫秒级跑完一条五节点流程。守的是四件事：

1. 游标只前进、`nodes_used` 有硬上限；
2. 父上下文逐跳下传（深度与 `from_agent` 都靠它）；
3. 停审批时游标不动、恢复后**不重跑**停住的那个节点；
4. 截断、缺项、并行失败都要留痕——静默的空值比报错难查十倍。
"""

from __future__ import annotations

import pytest

from app.orchestration import engine
from app.orchestration.definition import compile_definition, pick_edge
from app.services.agent_invoker import (
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    Invocation,
)
from app.services.agent_service import AgentBoundary
from app.services.agent_invoker import child_context_of
from app.tools.types import ToolExecutionContext


def _context(depth_path=(), agent="cs") -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="t",
        user_id="u",
        thread_id="th",
        run_id="r",
        request_id="req",
        agent_name=agent,
        agent_revision=1,
        delegation_path=tuple(depth_path),
    )


def _invocation(agent: str, output: str, *, status: str = STATUS_COMPLETED, **kwargs):
    return Invocation(
        status=status,
        target=agent,
        display_name=agent,
        output=output,
        message=kwargs.pop("message", ""),
        error_code=kwargs.pop("error_code", None),
        child_context=child_context_of(
            kwargs.pop("parent"),
            boundary=AgentBoundary(agent_name=agent, revision=1),
            limits=kwargs.pop("limits"),
            counts_against_budget=False,
        ),
        **kwargs,
    )


class Recorder:
    """记录每次调用：谁、任务、以及「当时父上下文的深度」。"""

    def __init__(self, outputs: dict[str, str], *, fail: set[str] | None = None):
        self.outputs = outputs
        self.fail = fail or set()
        self.calls: list[tuple[str, str, str, int]] = []

    async def __call__(self, parent, *, agent, task, node_id, branch):
        self.calls.append((node_id, agent, task, len(parent.delegation_path)))
        if node_id in self.fail or agent in self.fail:
            return Invocation(
                status=STATUS_FAILED,
                target=agent,
                output="",
                message=f"{agent} 没做完",
                error_code="DELEGATE_FAILED",
                depth=len(parent.delegation_path) + 1,
                child_context=parent,
            )
        output = self.outputs.get(node_id, self.outputs.get(agent, ""))
        if output == "__PAUSE__":
            return Invocation(
                status=STATUS_AWAITING_APPROVAL,
                target=agent,
                output="",
                message="需要确认一项写操作",
                depth=len(parent.delegation_path) + 1,
                approval={"approval_id": "ap-1", "status": STATUS_AWAITING_APPROVAL},
                child_context=parent,
            )
        return _invocation(
            agent,
            output,
            parent=parent,
            limits=parent.delegation_limits,
        )


SEQUENTIAL = {
    "entry": "classify",
    "nodes": [
        {"id": "classify", "agent": "cs", "task": "分类：{{input.text}}",
         "emit": {"field": "category"}, "next": [{"when": None, "to": "diagnose"}]},
        {"id": "diagnose", "agent": "tech", "task": "按 {{nodes.classify}} 排查",
         "next": [{"when": None, "to": "answer"}]},
        {"id": "answer", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}


async def test_sequential_flow_keeps_every_node_at_one_hop():
    """每个节点都以**发起方**为父，不是上一个节点。

    逐跳下传会让「只读 Agent 查规程 → 可写 Agent 落单」被判成边界放宽
    （`DELEGATE_WIDENS`），而这条分工恰恰是编排最正当的用法。深度因此恒为 1，
    长度交给 `max_nodes` 管。
    """
    plan = compile_definition(SEQUENTIAL)
    recorder = Recorder({"classify": "电控类", "diagnose": "检查变频器"})

    outcome = await engine.run(
        plan,
        input_text="3 号泵跳停",
        context=_context(),
        invoke=recorder,
    )

    assert outcome.status == engine.STATUS_COMPLETED
    assert [c[0] for c in recorder.calls] == ["classify", "diagnose"]
    assert [c[3] for c in recorder.calls] == [0, 0]
    assert "检查变频器" in outcome.answer
    assert outcome.nodes_used == 3


async def test_compose_does_not_consume_depth():
    """汇总节点不调 Agent，所以它不占一跳。"""
    plan = compile_definition(SEQUENTIAL)
    assert plan.depth_hops == 2


async def test_branch_picks_edge_by_emitted_field():
    plan = compile_definition(
        {
            "entry": "classify",
            "nodes": [
                {"id": "classify", "agent": "cs", "task": "{{input.text}}",
                 "emit": {"field": "category"},
                 "next": [
                     {"when": {"key": "category", "op": "in", "value": ["电控", "液压"]},
                      "to": "diagnose"},
                     {"when": None, "to": "answer"},
                 ]},
                {"id": "diagnose", "agent": "tech", "task": "{{nodes.classify}}",
                 "next": [{"when": None, "to": "answer"}]},
                {"id": "answer", "kind": "compose", "task": "答：{{upstream}}", "next": []},
            ],
        }
    )

    hit = Recorder({"classify": "电控", "diagnose": "查 IGBT"})
    outcome = await engine.run(plan, input_text="x", context=_context(), invoke=hit)
    assert [c[0] for c in hit.calls] == ["classify", "diagnose"]
    assert outcome.status == engine.STATUS_COMPLETED

    miss = Recorder({"classify": "外观问题"})
    outcome = await engine.run(plan, input_text="x", context=_context(), invoke=miss)
    assert [c[0] for c in miss.calls] == ["classify"]


async def test_pause_keeps_cursor_and_resume_does_not_rerun_the_node():
    plan = compile_definition(
        {
            "entry": "check",
            "nodes": [
                {"id": "check", "agent": "ops", "task": "查状态",
                 "next": [{"when": None, "to": "write"}]},
                {"id": "write", "agent": "ops", "task": "开工单：{{nodes.check}}",
                 "next": [{"when": None, "to": "done"}]},
                {"id": "done", "kind": "compose", "task": "完成：{{nodes.write}}", "next": []},
            ],
        }
    )

    recorder = Recorder({"check": "泵已停", "write": "__PAUSE__"})
    paused = await engine.run(plan, input_text="x", context=_context(), invoke=recorder)

    assert paused.status == engine.STATUS_PAUSED
    assert paused.cursor == "write"
    # 暂停的节点没有产出，不该占掉一格预算。
    assert paused.nodes_used == 1
    assert paused.approval and paused.approval["approval_id"] == "ap-1"

    resume_recorder = Recorder({"done": "已开出工单 T-1"})
    resumed = await engine.run(
        plan,
        input_text="x",
        context=_context(),
        invoke=resume_recorder,
        snapshot=paused.snapshot(),
        resumed=engine.NodeOutcome(
            node_id="write",
            kind="agent",
            status=engine.NODE_OK,
            agent="ops",
            output="工单已建",
        ),
    )

    # **没有重跑 write**：只有汇总节点被调用。
    assert [c[0] for c in resume_recorder.calls] == []
    assert resumed.status == engine.STATUS_COMPLETED
    assert "工单已建" in resumed.answer


async def test_output_is_truncated_and_marked():
    plan = compile_definition(
        {
            "entry": "long",
            "nodes": [
                {"id": "long", "agent": "ops", "task": "产出长文",
                 "emit": {"field": "digest", "max_chars": 10},
                 "next": [{"when": None, "to": "tail"}]},
                {"id": "tail", "kind": "compose", "task": "{{nodes.long}}", "next": []},
            ],
        }
    )
    recorder = Recorder({"long": "一二三四五六七八九十十一十二"})
    outcome = await engine.run(plan, input_text="x", context=_context(), invoke=recorder)

    entry = outcome.blackboard["long"]
    assert len(entry["text"]) == 10
    assert entry["truncated"] is True
    # 下游拿到的是截断后的，不是完整长文。
    assert outcome.answer == "一二三四五六七八九十"


async def test_node_budget_stops_the_loop():
    nodes = [
        {"id": f"n{i}", "agent": "ops", "task": "步骤",
         "next": ([{"when": None, "to": f"n{i + 1}"}] if i < 4 else [])}
        for i in range(5)
    ]
    plan = compile_definition({"entry": "n0", "max_nodes": 5, "nodes": nodes})

    recorder = Recorder({f"n{i}": f"第{i}步" for i in range(5)})
    # 运行时再收一次：平台侧把上限压到 3（图自己配的 5 只是允许的最大值）。
    outcome = await engine.run(
        plan, input_text="x", context=_context(), invoke=recorder, max_nodes=3
    )

    assert outcome.status == engine.STATUS_FAILED
    assert outcome.error_code == "WORKFLOW_NODE_BUDGET_EXHAUSTED"
    assert outcome.nodes_used == 3


async def test_parallel_collects_and_marks_missing_when_skipped():
    definition = {
        "entry": "collect",
        "nodes": [
            {"id": "collect", "kind": "parallel", "task": "并行查三台",
             "on_error": "skip",
             "branches": [
                 {"id": "b1", "agent": "ops", "task": "查 1 号", "emit": {"field": "s1"}},
                 {"id": "b2", "agent": "ops", "task": "查 2 号", "emit": {"field": "s2"}},
             ],
             "next": [{"when": None, "to": "answer"}]},
            {"id": "answer", "kind": "compose", "task": "{{upstream}}", "next": []},
        ],
    }
    plan = compile_definition(definition)

    async def invoke(parent, *, agent, task, node_id, branch):
        if branch == "b2":
            return Invocation(
                status=STATUS_FAILED,
                target=agent,
                message="2 号离线",
                error_code="DELEGATE_FAILED",
                depth=1,
                child_context=parent,
            )
        return _invocation(agent, "1 号正常", parent=parent, limits=parent.delegation_limits)

    outcome = await engine.run(plan, input_text="x", context=_context(), invoke=invoke)

    assert outcome.status == engine.STATUS_COMPLETED
    entry = outcome.blackboard["collect"]
    assert entry["missing"] == ["b2"]
    assert "1 号正常" in entry["text"]
    # 缺项要留痕：不能静默只报一台。
    assert "未完成" in entry["text"]


async def test_parallel_fails_whole_node_without_skip():
    plan = compile_definition(
        {
            "entry": "collect",
            "nodes": [
                {"id": "collect", "kind": "parallel", "task": "并行",
                 "branches": [
                     {"id": "b1", "agent": "ops", "task": "a"},
                     {"id": "b2", "agent": "ops", "task": "b"},
                 ],
                 "next": [{"when": None, "to": "answer"}]},
                {"id": "answer", "kind": "compose", "task": "{{upstream}}", "next": []},
            ],
        }
    )

    async def invoke(parent, *, agent, task, node_id, branch):
        if branch == "b2":
            return Invocation(status=STATUS_FAILED, target=agent, message="挂了",
                              error_code="X", depth=1, child_context=parent)
        return _invocation(agent, "ok", parent=parent, limits=parent.delegation_limits)

    outcome = await engine.run(plan, input_text="x", context=_context(), invoke=invoke)

    assert outcome.status == engine.STATUS_FAILED
    # 黑板里不留半个键。
    assert "collect" not in outcome.blackboard


async def test_skip_node_is_recorded_not_silently_empty():
    plan = compile_definition(
        {
            "entry": "primary",
            "nodes": [
                {"id": "primary", "agent": "ops", "task": "主路径", "on_error": "skip",
                 "next": [{"when": None, "to": "tail"}]},
                {"id": "tail", "kind": "compose", "task": "{{upstream}}", "next": []},
            ],
        }
    )
    recorder = Recorder({}, fail={"primary"})
    outcome = await engine.run(plan, input_text="x", context=_context(), invoke=recorder)

    assert outcome.status == engine.STATUS_COMPLETED
    entry = outcome.blackboard["primary"]
    assert entry["status"] == engine.NODE_SKIPPED
    assert entry["error_code"] == "DELEGATE_FAILED"
    # 下游看到的是「这里没完成」，不是一段空白。
    assert "未完成" in outcome.answer


async def test_persist_is_called_before_each_node():
    """游标先落库再调用：崩在中间也能知道自己停在哪。"""
    plan = compile_definition(SEQUENTIAL)
    recorder = Recorder({"classify": "电控", "diagnose": "查"})
    saved: list[str] = []

    async def persist(*, cursor, blackboard, nodes_used, visited, status, **kwargs):
        saved.append(cursor)
        # 落库时该节点还没跑：黑板里不该已经有它的产出。
        assert cursor not in blackboard

    await engine.run(
        plan, input_text="x", context=_context(), invoke=recorder, persist=persist
    )

    # 汇总节点也要先落游标：它同样是一个会跑（并可能崩）的步骤。
    assert saved == ["classify", "diagnose", "answer"]


def test_parallel_node_needs_no_task_of_its_own():
    """并行节点自己不执行任何东西，不要求它写 task。

    它下面每个分支各带 task，引擎也只渲染分支的；要求一段永远不执行的文字，
    只会让画图的人多填一格，而这段文字在运行记录里也看不到。
    """
    plan = compile_definition(
        {
            "entry": "collect",
            "nodes": [
                {
                    "id": "collect",
                    "kind": "parallel",
                    "branches": [
                        {"id": "a", "agent": "ops", "task": "查一号泵", "emit": {"field": "a"}},
                        {"id": "b", "agent": "ops", "task": "查二号泵", "emit": {"field": "b"}},
                    ],
                    "next": [],
                }
            ],
        }
    )
    assert plan.node("collect").task == ""
    assert [b.id for b in plan.node("collect").branches] == ["a", "b"]


def test_missing_fallback_is_rejected_at_compile_time():
    """没有兜底边 → 保存时就拒。跑到那里才停的流程，在用户看来是「点了没反应」。"""
    from app.orchestration.definition import DefinitionError

    with pytest.raises(DefinitionError) as exc:
        compile_definition(
            {
                "entry": "a",
                "nodes": [
                    {"id": "a", "agent": "ops", "task": "t", "emit": {"field": "k"},
                     "next": [{"when": {"key": "k", "op": "eq", "value": "x"}, "to": "b"}]},
                    {"id": "b", "kind": "compose", "task": "done", "next": []},
                ],
            }
        )
    assert exc.value.code == "WORKFLOW_NO_FALLBACK"


def test_pick_edge_returns_none_when_no_condition_matches():
    """条件全不命中且没有兜底 → None。

    校验已经禁止了这种图，这里直接构造节点，守的是「有人手改数据库之后
    引擎不会把没走通当成跑完了」。
    """
    from app.orchestration.definition import Condition, Edge, Node

    node = Node(
        id="a",
        task="t",
        next=(Edge(when=Condition(key="k", op="eq", value="x"), to="b"),),
    )
    assert pick_edge(node, blackboard={"a": {"keys": {"k": "y"}}}, visited=["a"]) is None


@pytest.mark.parametrize(
    "raw, code",
    [
        ({"entry": "a", "nodes": [{"id": "a", "agent": "x", "task": "t"}]}, None),
        (
            {"entry": "a", "nodes": [{"id": "a", "agent": "x", "task": "t", "next": []},
                                     {"id": "b", "agent": "x", "task": "t"}]},
            "WORKFLOW_UNREACHABLE_NODE",
        ),
        (
            {"entry": "a", "nodes": [
                {"id": "a", "agent": "x", "task": "t", "next": [{"when": None, "to": "b"}]},
                {"id": "b", "agent": "x", "task": "t", "next": [{"when": None, "to": "a"}]},
            ]},
            "WORKFLOW_CYCLE",
        ),
        (
            {"entry": "a", "nodes": [
                {"id": "a", "agent": "x", "task": "t", "next": [{"when": None, "to": "b"}]},
                {"id": "b", "agent": "x", "task": "用 {{nodes.c}}",
                 "next": [{"when": None, "to": "c"}]},
                {"id": "c", "agent": "x", "task": "t", "next": []},
            ]},
            "WORKFLOW_UNREACHABLE_REFERENCE",
        ),
    ],
)
def test_definition_compiles_or_rejects(raw, code):
    from app.orchestration.definition import DefinitionError

    if code is None:
        assert compile_definition(raw).size == len(raw["nodes"])
        return

    with pytest.raises(DefinitionError) as exc:
        compile_definition(raw)
    assert exc.value.code == code


# ---------------- emit 的结构化抽取 ----------------
#
# 默认的 emit 只是把输出截断成一个键，于是 `eq "泄漏"` 永远比不上
# 「泄漏。建议立即关闭阀门」。`values` / `pattern` 就是为了把回答收敛成一个值。
# 这几条守的是：抽取规则本身、抽不到时的退路、以及「配错了要在保存时就拒」。


BRANCH_BY_VALUES = {
    "entry": "classify",
    "nodes": [
        {"id": "classify", "agent": "cs",
         "task": "判断类别：{{input.text}}",
         "emit": {"field": "category", "values": ["泄漏", "超压"]},
         "next": [
             {"when": {"key": "category", "op": "eq", "value": "泄漏"}, "to": "urgent"},
             {"when": None, "to": "answer"},
         ]},
        {"id": "urgent", "agent": "ops", "task": "按泄漏处置",
         "next": [{"when": None, "to": "answer"}]},
        {"id": "answer", "kind": "compose", "task": "结论：{{upstream}}", "next": []},
    ],
}


async def test_emit_values_routes_even_when_the_answer_is_a_paragraph():
    """这是 values 存在的理由：回答是一段话，条件仍然命中。"""
    plan = compile_definition(BRANCH_BY_VALUES)
    recorder = Recorder(
        {"classify": "泄漏。建议立即关闭上游阀门并疏散人员。", "urgent": "已按泄漏处置"}
    )

    outcome = await engine.run(
        plan, input_text="厨房有味道", context=_context(), invoke=recorder
    )

    assert [c[0] for c in recorder.calls] == ["classify", "urgent"], outcome.answer
    assert outcome.blackboard["classify"]["keys"]["category"] == "泄漏"


async def test_emit_values_falls_back_to_truncated_text_when_nothing_matches():
    """抽不到就退回截断文本——不走「留空」那条路，否则下游模板会缺键。"""
    plan = compile_definition(BRANCH_BY_VALUES)
    recorder = Recorder({"classify": "无法判断，需要现场确认"})

    outcome = await engine.run(
        plan, input_text="描述不清", context=_context(), invoke=recorder
    )

    # 兜底边生效：没有走 urgent。
    assert [c[0] for c in recorder.calls] == ["classify"]
    assert outcome.blackboard["classify"]["keys"]["category"] == "无法判断，需要现场确认"


def test_emit_values_prefers_the_longer_label_at_the_same_position():
    """「超压」不能被「压」吃掉：短标签在前时会选错分支，而且只在复盘时才发现。"""
    plan = compile_definition(
        {
            "entry": "a",
            "nodes": [
                {"id": "a", "agent": "cs", "task": "t",
                 "emit": {"field": "k", "values": ["超压", "压"]},
                 "next": [{"when": None, "to": "b"}]},
                {"id": "b", "kind": "compose", "task": "done", "next": []},
            ],
        }
    )
    assert engine.extract_emit(plan.node("a"), "判断：超压") == "超压"


def test_emit_pattern_takes_the_first_capture_group():
    plan = compile_definition(
        {
            "entry": "a",
            "nodes": [
                {"id": "a", "agent": "cs", "task": "t",
                 "emit": {"field": "level", "pattern": r"风险等级[:：]\s*([1-5])"},
                 "next": [{"when": None, "to": "b"}]},
                {"id": "b", "kind": "compose", "task": "done", "next": []},
            ],
        }
    )
    assert engine.extract_emit(plan.node("a"), "风险等级：4（较高）") == "4"
    # 没有捕获组就取整个匹配。
    plan2 = compile_definition(
        {
            "entry": "a",
            "nodes": [
                {"id": "a", "agent": "cs", "task": "t",
                 "emit": {"field": "level", "pattern": r"L[1-5]"},
                 "next": [{"when": None, "to": "b"}]},
                {"id": "b", "kind": "compose", "task": "done", "next": []},
            ],
        }
    )
    assert engine.extract_emit(plan2.node("a"), "定级 L3") == "L3"


@pytest.mark.parametrize(
    "emit, reason",
    [
        # 正则写错要在保存时炸，别等某个节点真跑完才发现——那时候得重跑整条链。
        ({"field": "k", "pattern": "([0-9"}, "正则"),
        # 同时给两个：实际存哪个取决于优先序，看图看不出来。
        ({"field": "k", "values": ["a"], "pattern": "a+"}, "不能同时"),
        # 配了抽取却没有字段：看起来该分流，实际什么都没发生。
        ({"values": ["a"]}, "没有 emit.field"),
        ({"field": "k", "values": "不是数组"}, "必须是数组"),
        ({"field": "k", "values": [1]}, "只能放字符串"),
    ],
)
def test_emit_misconfiguration_is_rejected_at_save(emit, reason):
    from app.orchestration.definition import DefinitionError

    raw = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agent": "cs", "task": "t", "emit": emit,
             "next": [{"when": None, "to": "b"}]},
            {"id": "b", "kind": "compose", "task": "done", "next": []},
        ],
    }
    with pytest.raises(DefinitionError) as exc:
        compile_definition(raw)
    assert exc.value.code == "WORKFLOW_BAD_EMIT"
    assert reason in exc.value.message
