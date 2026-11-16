# app/orchestration/engine.py
"""编排解释器：游标 + 黑板。

    run   = 冻结的图（revision 已定）
    bb    = 黑板：node_id -> {"text", "keys", "truncated", "missing"}
    cursor= 下一个要跑的节点

为什么不是一个动态 LangGraph 子图：现有审批恢复依赖「父图不中断、子图停在
`create_and_wait_approval`」，`interrupts.py` 与 `chat_service` 都按这个形状写的。
动态子图要把 `thread_id` 归属、中断载荷、`pending_delegated_approval` 三件事再推演
一遍，而收益只是省掉一个 `while`（`docs/ORCHESTRATION.md` §4 D1）。

两条不显眼但很关键的约定：

1. **`cursor` 在调用之前落库**（`persist`）。先记「我要跑第 3 个节点」再跑，
   进程崩了重启能知道崩在哪；反过来只会在日志里留下一句「不知道跑到哪了」。
2. **节点之间只传结论**。模板只能取黑板里的键，取不到就失败，**不**给空串——
   空串会被下一个节点当成「上游说没有问题」，然后给出一段看起来完整的结论。

本模块不碰数据库、不碰模型：`invoke` 与 `persist` 都是注入的，所以
`tests/unit/test_workflow_engine.py` 可以在一毫秒里跑完一条五节点流程。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.orchestration.definition import (
    KIND_COMPOSE,
    KIND_PARALLEL,
    DefinitionError,
    Node,
    WorkflowPlan,
    pick_edge,
    render,
)
from app.services.agent_invoker import (
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    Invocation,
)
from app.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# 并行分支的并发上限。不是拍脑袋：数据库连接池与供应商速率限制都在这一层，
# 四个分支同时打过去会让慢的那一个把整节点的 P95 拉成它的 4 倍。
PARALLEL_CONCURRENCY = 2

STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 节点结果的形态（不是 run 的状态）。
NODE_OK = "ok"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"
NODE_PAUSED = "paused"


class WorkflowAborted(Exception):
    """某一步失败，整条流程该停下。`message` 直接进 run 的失败原因。"""

    def __init__(self, code: str, message: str, *, node: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """一个节点跑完（或没跑完）之后，界面与审计要说的话。"""

    node_id: str
    kind: str
    status: str
    agent: str = ""
    output: str = ""
    truncated: bool = False
    keys: dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    error_code: str = ""
    message: str = ""
    missing: tuple[str, ...] = ()
    # 停审批时那张单子的号，以及它的原始载荷（preview / expires_at）。
    # 载荷不进 `as_dict`：它是给 chat 层拼确认卡用的，不是给运行页的。
    approval_id: str = ""
    approval_payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "status": self.status,
            "agent": self.agent,
            "truncated": self.truncated,
            "keys": self.keys,
            "depth": self.depth,
            "error_code": self.error_code,
            "message": self.message,
            "missing": list(self.missing),
            # 预览前 400 字：运行页要能一眼看出这一步到底产出了什么，
            # 但整段结论可能上千字，全塞进列表接口没有必要。
            "preview": (self.output or "")[:400],
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    """断点。`paused` 的 run 落库的就是这四样。"""

    cursor: str | None
    blackboard: dict[str, dict[str, Any]]
    nodes_used: int
    visited: list[str]


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    blackboard: dict[str, dict[str, Any]]
    nodes_used: int
    cursor: str | None
    answer: str = ""
    error_code: str = ""
    message: str = ""
    approval: dict[str, Any] | None = None
    trace: tuple[NodeOutcome, ...] = ()
    visited: tuple[str, ...] = ()

    def snapshot(self) -> Snapshot:
        return Snapshot(
            cursor=self.cursor,
            blackboard=self.blackboard,
            nodes_used=self.nodes_used,
            visited=list(self.visited),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "cursor": self.cursor,
            "nodes_used": self.nodes_used,
            "answer": self.answer,
            "error_code": self.error_code,
            "message": self.message,
            "nodes": [item.as_dict() for item in self.trace],
        }


# `invoke` 的形状：给定父上下文与目标，跑一次子调用。
NodeInvoker = Callable[..., Awaitable[Invocation]]
# `persist` 的形状：把「现在跑到哪、黑板上有什么」落库。
Persister = Callable[..., Awaitable[None]]


async def run(
    plan: WorkflowPlan,
    *,
    input_text: str,
    context: ToolExecutionContext,
    invoke: NodeInvoker,
    persist: Persister | None = None,
    max_nodes: int | None = None,
    snapshot: Snapshot | None = None,
    resumed: NodeOutcome | None = None,
) -> Outcome:
    """跑（或续跑）一张图。

    `resumed` 只在一个场景下传：某个节点停过审批、人已经点完了。此时那个节点的
    结论由审批恢复链路给回来，引擎把它当作「已完成」写进黑板，从它的出边继续——
    **不重跑该节点**。重跑会在外部系统上再写一次，那正是审批要防的事。
    """
    cap = max_nodes or plan.max_nodes

    if snapshot is None:
        blackboard: dict[str, dict[str, Any]] = {}
        visited: list[str] = []
        nodes_used = 0
        cursor: str | None = plan.entry
    else:
        blackboard = {k: dict(v) for k, v in snapshot.blackboard.items()}
        visited = list(snapshot.visited)
        nodes_used = snapshot.nodes_used
        cursor = snapshot.cursor

    trace: list[NodeOutcome] = []
    last_text = _latest_text(blackboard, visited)

    if resumed is not None:
        node = plan.nodes.get(resumed.node_id)
        if node is None:
            return Outcome(
                status=STATUS_FAILED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=cursor,
                error_code="WORKFLOW_UNKNOWN_NODE",
                message=f"续跑时找不到节点「{resumed.node_id}」，图可能已经被改过。",
                trace=tuple(trace),
                visited=tuple(visited),
            )

        _write(blackboard, resumed, node)
        visited.append(node.id)
        nodes_used += 1
        trace.append(resumed)
        last_text = resumed.output
        cursor = pick_edge(node, blackboard=blackboard, visited=visited)

        if cursor is None:
            return Outcome(
                status=STATUS_FAILED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=None,
                answer=_final_answer(blackboard, visited, last_text),
                error_code="WORKFLOW_NO_MATCHING_EDGE",
                message=f"节点「{node.id}」跑完了，但没有一条出边接通。",
                trace=tuple(trace),
                visited=tuple(visited),
            )

    while cursor is not None:
        if nodes_used >= cap:
            return Outcome(
                status=STATUS_FAILED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=cursor,
                answer=_final_answer(blackboard, visited, last_text),
                error_code="WORKFLOW_NODE_BUDGET_EXHAUSTED",
                message=(
                    f"这次执行已经跑了 {nodes_used} 个节点，达到这张图的上限（{cap}）。"
                    "如果流程确实需要更多步，请调大 max_nodes。"
                ),
                trace=tuple(trace),
                visited=tuple(visited),
            )

        node = plan.nodes.get(cursor)
        if node is None:
            return Outcome(
                status=STATUS_FAILED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=None,
                error_code="WORKFLOW_UNKNOWN_NODE",
                message=f"找不到节点「{cursor}」，图可能已经被改过。",
                trace=tuple(trace),
                visited=tuple(visited),
            )

        # 「我要跑第 N 个节点」先落库，再跑。崩在中间的 run 因此知道自己停在哪。
        if persist is not None:
            try:
                await persist(
                    cursor=node.id,
                    blackboard=blackboard,
                    nodes_used=nodes_used,
                    visited=visited,
                    status=STATUS_RUNNING,
                    nodes=[item.as_dict() for item in trace],
                )
            except Exception as exc:
                logger.exception(
                    "编排游标落库失败，已中止本次执行。",
                    extra={"event": "workflow_persist_failed", "node": node.id},
                )
                return Outcome(
                    status=STATUS_FAILED,
                    blackboard=blackboard,
                    nodes_used=nodes_used,
                    cursor=node.id,
                    error_code="WORKFLOW_PERSIST_FAILED",
                    message=f"执行状态无法保存（{exc}），为避免重复执行已中止。",
                    trace=tuple(trace),
                    visited=tuple(visited),
                )

        try:
            outcome, advance, next_context = await _run_node(
                node,
                plan=plan,
                input_text=input_text,
                context=context,
                invoke=invoke,
                blackboard=blackboard,
                visited=visited,
            )
        except WorkflowAborted as exc:
            return Outcome(
                status=STATUS_FAILED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=node.id,
                answer=_final_answer(blackboard, visited, last_text),
                error_code=exc.code,
                message=exc.message,
                trace=tuple(trace),
                visited=tuple(visited),
            )

        if outcome.status == NODE_PAUSED:
            # 停在这里等人确认。游标**不动**：恢复时要靠它知道停在哪。
            # 不落 nodes_used：这个节点还没有产出，算进去会让上限提前耗掉一格。
            trace.append(outcome)
            if persist is not None:
                await _safe_persist(
                    persist,
                    cursor=node.id,
                    blackboard=blackboard,
                    nodes_used=nodes_used,
                    visited=visited,
                    status=STATUS_PAUSED,
                    approval_id=outcome.approval_id,
                    nodes=[item.as_dict() for item in trace],
                )
            return Outcome(
                status=STATUS_PAUSED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=node.id,
                answer=last_text,
                message=outcome.message,
                approval=outcome.approval_payload,
                trace=tuple(trace),
                visited=tuple(visited),
            )

        _write(blackboard, outcome, node)
        visited.append(node.id)
        nodes_used += 1
        trace.append(outcome)
        last_text = outcome.output or last_text

        # 下一跳仍然以**发起方**为父，不把刚跑完那个 Agent 的子上下文接下去。
        #
        # 编排不是「A 把任务转交给 B」，而是「人一声令下，图依次调度 A、B」。
        # 按逐跳下传会得到两个错误结论：
        #   1) 「只读 Agent 查规程 → 可写 Agent 落单」这种正常分工会被判成
        #      边界放宽（`DELEGATE_WIDENS`），而这条分工恰恰是编排存在的理由；
        #   2) 审计里 `from_agent` 会记成上一个节点，而真正发起这一跳的是人。
        # 所以每个节点都跟发起方重新比一次边界（收窄仍然立即生效，见 `A2A.md` §9），
        # 深度恒为 1 跳；「一张图能跑多长」交给 `max_nodes` 管——两件事本来就该分开。
        #
        # `next_context` 仍然从 `_run_node` 返回：委派路径（`delegate_to_agent`）
        # 自己要用它做逐跳收窄，只是编排这里不接。
        _ = next_context
        cursor = advance
        if cursor is None:
            return Outcome(
                status=STATUS_COMPLETED,
                blackboard=blackboard,
                nodes_used=nodes_used,
                cursor=None,
                answer=_final_answer(blackboard, visited, last_text),
                trace=tuple(trace),
                visited=tuple(visited),
            )

    return Outcome(
        status=STATUS_COMPLETED,
        blackboard=blackboard,
        nodes_used=nodes_used,
        cursor=None,
        answer=_final_answer(blackboard, visited, last_text),
        trace=tuple(trace),
        visited=tuple(visited),
    )


async def _safe_persist(persist: Persister, **kwargs: Any) -> None:
    try:
        await persist(**kwargs)
    except Exception:
        # 暂停态的落库失败不该把「已经在等审批」这件事变成报错：
        # 审批单本身是另一条链路写的，用户看到的卡还在。
        logger.exception(
            "编排暂停状态落库失败。",
            extra={"event": "workflow_pause_persist_failed"},
        )


async def _run_node(
    node: Node,
    *,
    plan: WorkflowPlan,
    input_text: str,
    context: ToolExecutionContext,
    invoke: NodeInvoker,
    blackboard: dict[str, dict[str, Any]],
    visited: list[str],
) -> tuple[NodeOutcome, str | None, ToolExecutionContext]:
    """跑一个节点，返回（结果, 下一个节点, 下一跳的父上下文）。

    `advance` 为 None 表示走到终点。抛 `WorkflowAborted` 表示整条流程该停。

    **父上下文必须逐跳往下传**：`child_context_of` 把这一跳的 Agent 边界、
    深度、上游链都折进去，下一节点拿它当父——逐跳收窄与深度累积才是自然发生的。
    每步都用根上下文的话，深度永远是 1，`max_depth` 形同不存在，而审计里的
    `from_agent` 也会指回根而不是直接上级。
    """
    if node.kind == KIND_COMPOSE:
        try:
            text = render(
                node.task,
                node=node,
                blackboard=blackboard,
                input_text=input_text,
                visited=visited,
            )
        except DefinitionError as exc:
            raise WorkflowAborted(exc.code, exc.message, node=node.id) from exc

        outcome = NodeOutcome(
            node_id=node.id,
            kind=node.kind,
            status=NODE_OK,
            output=text,
            keys=_emit_keys(node, text),
        )
        # 汇总节点不调 Agent，链路停在原地：下一跳的父仍然是进入时的那个。
        return outcome, _advance(node, blackboard, visited, outcome), context

    if node.kind == KIND_PARALLEL:
        return await _run_parallel(
            node,
            plan=plan,
            input_text=input_text,
            context=context,
            invoke=invoke,
            blackboard=blackboard,
            visited=visited,
        )

    try:
        task = render(
            node.task,
            node=node,
            blackboard=blackboard,
            input_text=input_text,
            visited=visited,
        )
    except DefinitionError as exc:
        raise WorkflowAborted(exc.code, exc.message, node=node.id) from exc

    invocation = await invoke(
        context,
        agent=node.agent,
        task=task,
        node_id=node.id,
        branch="",
    )

    if invocation.status == STATUS_AWAITING_APPROVAL:
        return (
            NodeOutcome(
                node_id=node.id,
                kind=node.kind,
                status=NODE_PAUSED,
                agent=node.agent,
                depth=invocation.depth,
                message=invocation.message,
                approval_id=str((invocation.approval or {}).get("approval_id") or ""),
                approval_payload=invocation.approval,
            ),
            None,
            context,
        )

    if invocation.status == STATUS_FAILED:
        if node.on_error == "skip":
            outcome = NodeOutcome(
                node_id=node.id,
                kind=node.kind,
                status=NODE_SKIPPED,
                agent=node.agent,
                output=f"（节点 {node.id} 未完成：{invocation.error_code}）",
                error_code=invocation.error_code or "",
                message=invocation.message,
                depth=invocation.depth,
            )
            # skip 掉的节点没有「下一跳的父」——它什么都没产出来。
            return outcome, _advance(node, blackboard, visited, outcome), context

        raise WorkflowAborted(
            invocation.error_code or "WORKFLOW_NODE_FAILED",
            invocation.message or f"节点「{node.id}」没有完成。",
            node=node.id,
        )

    outcome = NodeOutcome(
        node_id=node.id,
        kind=node.kind,
        status=NODE_OK,
        agent=node.agent,
        output=invocation.output,
        keys=_emit_keys(node, invocation.output),
        depth=invocation.depth,
    )
    return (
        outcome,
        _advance(node, blackboard, visited, outcome),
        invocation.child_context or context,
    )


async def _run_parallel(
    node: Node,
    *,
    plan: WorkflowPlan,
    input_text: str,
    context: ToolExecutionContext,
    invoke: NodeInvoker,
    blackboard: dict[str, dict[str, Any]],
    visited: list[str],
) -> tuple[NodeOutcome, str | None, ToolExecutionContext]:
    """并行收集。**只收集，不合成**（`A2A.md` §12 仍然成立）。

    合并规则是按分支 id 摊平，引擎不做任何语义合并——不投票、不选优、
    不「综合两方观点」。谁想要「各写一版再选好的那份」，那是评测问题。
    """
    semaphore = asyncio.Semaphore(PARALLEL_CONCURRENCY)

    async def one(branch) -> tuple[str, Invocation | None, str]:
        async with semaphore:
            try:
                task = render(
                    branch.task,
                    node=node,
                    blackboard=blackboard,
                    input_text=input_text,
                    visited=visited,
                )
            except DefinitionError as exc:
                return branch.id, None, f"{exc.code}: {exc.message}"
            invocation = await invoke(
                context,
                agent=branch.agent,
                task=task,
                node_id=node.id,
                branch=branch.id,
            )
            return branch.id, invocation, ""

    results = await asyncio.gather(*(one(b) for b in node.branches))

    blocks: list[str] = []
    keys: dict[str, Any] = {}
    missing: list[str] = []
    # 并行节点自己的 emit 字段先占位：它的值在下面统一从合并文本里取。
    emit_field = node.emit_field

    failures: list[tuple[str, str, str]] = []

    for branch_id, invocation, render_error in results:
        if invocation is None:
            failures.append((branch_id, "WORKFLOW_TEMPLATE_MISSING", render_error))
            continue
        if invocation.status == STATUS_FAILED:
            failures.append(
                (branch_id, invocation.error_code or "WORKFLOW_NODE_FAILED", invocation.message)
            )
            continue
        if invocation.status == STATUS_AWAITING_APPROVAL:
            failures.append(
                (branch_id, "WORKFLOW_PARALLEL_APPROVAL", invocation.message)
            )
            continue

        text = invocation.output or ""
        blocks.append(f"【{branch_id}】{text}" if text else f"【{branch_id}】（无结论）")
        for branch in node.branches:
            if branch.id == branch_id and branch.emit_field:
                keys[branch.emit_field] = _clip(text, node.emit_chars)[0]

    if failures and node.on_error != "skip":
        branch_id, code, message = failures[0]
        raise WorkflowAborted(
            code,
            f"并行节点「{node.id}」的分支「{branch_id}」没有完成：{message}",
            node=node.id,
        )

    if failures:
        # skip：允许缺项，但**必须留痕**。静默少一条会让下游以为只查到了两台设备。
        for branch_id, code, _message in failures:
            missing.append(branch_id)
            blocks.append(f"【{branch_id}】（未完成：{code}）")

    merged = "\n\n".join(blocks)
    if emit_field:
        # 合并文本上同样支持抽取：三台设备各报一句「正常/异常」，汇总后取「异常」
        # 就能让下游按「有没有异常」分流，而不是让条件去比一整段拼接文本。
        keys[emit_field] = extract_emit(node, merged)

    outcome = NodeOutcome(
        node_id=node.id,
        kind=node.kind,
        status=NODE_OK,
        output=merged,
        truncated=len(merged) > node.emit_chars,
        keys=keys,
        missing=tuple(missing),
    )
    # 并行的下一跳父上下文是**进入并行之前**的那一个：分支之间是并列的，
    # 谁都不该成为另一个的上级。
    return outcome, _advance(node, blackboard, visited, outcome), context


def _advance(
    node: Node,
    blackboard: dict[str, dict[str, Any]],
    visited: list[str],
    outcome: NodeOutcome,
) -> str | None:
    """选下一条边。此刻本节点的产出还没写进黑板，所以先用一份临时视图。

    不这么做的话，`next` 里那种「判断自己刚产出的字段」的边会永远取不到值，
    而它恰恰是最常见的写法（分类 → 按类别分流）。
    """
    if not node.next:
        return None

    view = dict(blackboard)
    view[node.id] = {
        "text": outcome.output,
        "keys": dict(outcome.keys),
        "truncated": outcome.truncated,
    }

    target = pick_edge(node, blackboard=view, visited=[*visited, node.id])
    if target is None:
        raise WorkflowAborted(
            "WORKFLOW_NO_MATCHING_EDGE",
            f"节点「{node.id}」跑完了，但条件都不满足且没有兜底边。",
            node=node.id,
        )
    return target


def extract_emit(node: Node, text: str) -> str:
    """从节点输出里取出要写回黑板的那段文本。

    三种模式（`values` 与 `pattern` 互斥，编译期已经挡住同时给）：

    - `values`：回答里**最先出现**的枚举值；
    - `pattern`：第一个捕获组，没有捕获组就取整个匹配；
    - 没配、或配了但**没抽到**：退回截断文本。

    抽不到时退回截断而不是留空或抛错：留空会让下游模板缺键，整条链跑到后半段
    才失败；退回截断至少「有东西」，条件不命中就走兜底边——那是显式声明过的路。
    代价是「抽不到」和「抽到了」在黑板里长得一样，所以只适合兜底边有意义的图。
    """
    raw = text or ""
    if node.emit_values:
        hit = _first_mentioned(raw, node.emit_values)
        if hit:
            return _clip(hit, node.emit_chars)[0]
    elif node.emit_pattern:
        match = re.search(node.emit_pattern, raw)
        if match:
            taken = match.group(1) if match.groups() else match.group(0)
            if taken:
                return _clip(taken, node.emit_chars)[0]
    return _clip(raw, node.emit_chars)[0]


def _first_mentioned(text: str, values: tuple[str, ...]) -> str:
    """回答里最先出现的那个枚举值。

    位置相同时取更长的（「超压」优先于「压」），再相同则按声明顺序——
    否则短标签会把长标签吃掉，而那种错只有在下游走了错分支时才会被发现。
    """
    best_rank: tuple[int, int, int] | None = None
    best = ""
    for index, value in enumerate(values):
        pos = text.find(value)
        if pos < 0:
            continue
        rank = (pos, -len(value), index)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = value
    return best


def _emit_keys(node: Node, text: str) -> dict[str, Any]:
    """节点声明的 `emit.field`：出结果时就把它算好。

    必须在**出结果时**就填好，不能等到写黑板时才算——选边要用它
    （「分类 → 按类别分流」是最常见的写法），而选边发生在写黑板之前。

    默认存的是**输出的前 N 字**；配了 `emit.values` / `emit.pattern` 才会抽取
    成一个值（见 `extract_emit`）。抽不到时仍退回截断文本。
    """
    if not node.emit_field:
        return {}
    return {node.emit_field: extract_emit(node, text)}


def _write(
    blackboard: dict[str, dict[str, Any]],
    outcome: NodeOutcome,
    node: Node,
) -> None:
    """把节点结论写进黑板。截断在这里发生，并且**必须留痕**。

    截断不标记等于骗下游：它拿到 2000 字会以为这就是全部。
    """
    text, clipped = _clip(outcome.output or "", node.emit_chars)
    keys = dict(outcome.keys)
    if node.emit_field:
        keys[node.emit_field] = extract_emit(node, outcome.output)

    entry: dict[str, Any] = {
        "text": text,
        "keys": keys,
        "truncated": bool(outcome.truncated or clipped),
    }
    if outcome.missing:
        entry["missing"] = list(outcome.missing)
    if outcome.status != NODE_OK:
        entry["status"] = outcome.status
        entry["error_code"] = outcome.error_code

    blackboard[node.id] = entry


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _latest_text(blackboard: dict[str, dict[str, Any]], visited: list[str]) -> str:
    for nid in reversed(visited):
        text = str((blackboard.get(nid) or {}).get("text") or "").strip()
        if text:
            return text
    return ""


def _final_answer(
    blackboard: dict[str, dict[str, Any]],
    visited: list[str],
    fallback: str,
) -> str:
    """给用户的那句话：最后跑完的节点的结论。

    兜底用 `fallback`：最后一个节点可能是 `skip` 掉的空节点，
    这时候说「什么都没有」比说上一段的结论更糟。
    """
    for nid in reversed(visited):
        text = str((blackboard.get(nid) or {}).get("text") or "").strip()
        if text:
            return text
    return fallback
