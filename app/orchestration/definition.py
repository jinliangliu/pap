# app/orchestration/definition.py
"""编排图：解析、编译期校验、模板渲染、选边。

**为什么校验必须在保存时做完。** 运行时才发现的图错误有两种表现：一是流程跑到一半
静默死掉（没有兜底边），二是下游拿到一个空串然后一本正经地作答（引用了不存在的键）。
两种都不会抛异常，只会在某次复盘里被发现「那天它其实没干活」。所以本模块的每个
`raise` 都是为了让这类错误在**保存那一刻**变成一个可以直接看懂的拒绝理由。

校验分三类：

| 类 | 例子 | 界面上怎么说 |
|---|---|---|
| 结构 | 没有兜底边 / 有环 / 指向不存在的节点 | 图不完整，指出哪个节点 |
| 可达 | `{{nodes.diagnose}}` 出现在一条不经过 diagnose 的路径上 | 模板引用了走不到的节点 |
| 取值 | 条件算子不认识 / `in` 的值不是数组 | 条件写法不对 |

刻意不做的事（`docs/ORCHESTRATION.md` §8）：表达式语言、循环边、运行时改图、
节点内联提示词或临时授权。图能决定的只有「什么时候轮到谁」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 节点数与分支数的硬天花板。不是性能考虑：再高就等于「不设上限」，
# 而这张图存在的理由之一就是给「一次请求跑十分钟」踩刹车。
DEFAULT_MAX_NODES = 12
MAX_NODES_CEILING = 20
MAX_BRANCHES = 4

# 单节点输出的默认截断长度。设计稿 D2：默认 2000，顶 8000。
# 截断**必须留痕**（黑板里 `truncated: true`），否则下游会以为看到了全部。
DEFAULT_EMIT_CHARS = 2000
MAX_EMIT_CHARS = 8000

# `emit` 的结构化抽取。默认只是把输出截断成一个键；要「按类别分流」就得先把
# 模型的回答收敛成一个值，否则 `eq "泄漏"` 永远比不上「泄漏。建议立即关闭阀门」。
#
# 只给两个手段，不给表达式：
#   - `values`：枚举。回答里出现哪个词就存哪个（取最先出现的）。
#   - `pattern`：正则。存第一个捕获组，没有捕获组就存整个匹配。
# 二者互斥——同时给会让「实际存了什么」取决于优先序，而那是配图的人看不出来的。
MAX_EMIT_VALUES = 20
MAX_EMIT_VALUE_CHARS = 32
MAX_EMIT_PATTERN_CHARS = 200

KIND_AGENT = "agent"
KIND_COMPOSE = "compose"
KIND_PARALLEL = "parallel"
NODE_KINDS = (KIND_AGENT, KIND_COMPOSE, KIND_PARALLEL)

# 条件算子。没有 `contains` / `matches` / `starts_with`：一旦允许字符串运算，
# 下一个人就会写 `len(x) > 3 and y.startswith(...)`，然后图变成代码。
# 要复杂判断就再加一个 Agent 节点——那是能审计的做法。
OPS = ("eq", "neq", "in", "gt", "lt")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
# `inputs` 里引用黑板的写法：`$nodes.classify.category` 或 `$nodes.classify`。
_INPUT_REF_RE = re.compile(r"^\$nodes\.([a-z][a-z0-9_]{0,31})(?:\.([A-Za-z0-9_]{1,32}))?$")

MAX_TASK_CHARS = 4000


class DefinitionError(ValueError):
    """图不成立。`code` 供接口层与前端分支，`message` 直接给人看。"""

    def __init__(self, code: str, message: str, *, node: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


class TemplateMissing(DefinitionError):
    """运行时要取的键不在黑板上。

    单独一个类型是因为它的处理方式不同：其它 `DefinitionError` 都是「这张图不该
    被保存」，而它意味着「这张图在这条路径下跑不通」——引擎据此把 run 判失败，
    而不是把空串塞给下一个节点。
    """


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True, slots=True)
class Condition:
    key: str
    op: str
    value: Any

    def as_json(self) -> dict[str, Any]:
        return {"key": self.key, "op": self.op, "value": self.value}


@dataclass(frozen=True, slots=True)
class Edge:
    # None = 兜底边。它必须显式写出来：`when` 全不命中时流程静默死掉，
    # 是所有工作流引擎最经典的坑。
    when: Condition | None
    to: str

    def as_json(self) -> dict[str, Any]:
        return {"when": self.when.as_json() if self.when else None, "to": self.to}


@dataclass(frozen=True, slots=True)
class Branch:
    """并行节点的一个分支。它自己不能再有 `next`——收集不是合成。"""

    id: str
    agent: str
    task: str
    emit_field: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "task": self.task,
            **({"emit": {"field": self.emit_field}} if self.emit_field else {}),
        }


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    kind: str = KIND_AGENT
    agent: str = ""
    task: str = ""
    # 输出前 N 字写回黑板，作为下游条件判断与模板取值的键。
    emit_field: str = ""
    emit_chars: int = DEFAULT_EMIT_CHARS
    # 结构化抽取（可选）：`emit.values` / `emit.pattern`。都不给就是纯截断。
    # 抽不到时退回截断文本——宁可分支不命中走兜底边，也不要让下游缺键。
    emit_values: tuple[str, ...] = ()
    emit_pattern: str = ""
    # 显式声明的入参映射：`{"category": "$nodes.classify.category"}`。
    # 键必须在路径上必达，否则保存时就拒绝——运行时给空串最难查。
    inputs: dict[str, str] = field(default_factory=dict)
    on_error: str = "fail"
    next: tuple[Edge, ...] = ()
    branches: tuple[Branch, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return not self.next

    @property
    def invokes_agent(self) -> bool:
        """会不会真的调一次子 Agent。`compose` 不会。"""
        return self.kind in (KIND_AGENT, KIND_PARALLEL)

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "task": self.task}
        if self.kind != KIND_AGENT:
            payload["kind"] = self.kind
        if self.agent:
            payload["agent"] = self.agent
        if self.emit_field:
            emit: dict[str, Any] = {
                "field": self.emit_field,
                "max_chars": self.emit_chars,
            }
            if self.emit_values:
                emit["values"] = list(self.emit_values)
            if self.emit_pattern:
                emit["pattern"] = self.emit_pattern
            payload["emit"] = emit
        if self.inputs:
            payload["inputs"] = dict(self.inputs)
        if self.on_error != "fail":
            payload["on_error"] = self.on_error
        if self.branches:
            payload["branches"] = [b.as_json() for b in self.branches]
        payload["next"] = [e.as_json() for e in self.next]
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """编译过的图。跑起来之后不再读数据库里的 definition。

    `revision` 由服务层填（图是版本化配置，不是运行时状态）。
    """

    entry: str
    nodes: dict[str, Node]
    max_nodes: int
    order: tuple[str, ...]
    # 入口到终点的最长路径上「会真的调 Agent」的节点数。预检用它比深度上限：
    # 跑到第三个节点才因为深度不够而死，用户看不出是图配错了还是权限不够。
    depth_hops: int

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    @property
    def size(self) -> int:
        return len(self.nodes)

    def as_json(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "max_nodes": self.max_nodes,
            # 按拓扑序写出：diff 时不会因为原作者把节点写在后面而满屏变色。
            "nodes": [self.nodes[nid].as_json() for nid in self.order],
        }


# ---------------------------------------------------------------- 解析与校验


def compile_definition(raw: Any) -> WorkflowPlan:
    """把一份 JSON 编译成可执行的图，顺手把所有能在保存时发现的问题挑出来。"""
    if not isinstance(raw, dict):
        raise DefinitionError("WORKFLOW_INVALID", "流程定义必须是一个对象。")

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise DefinitionError("WORKFLOW_NO_NODES", "流程至少要有一个节点。")

    if len(raw_nodes) > MAX_NODES_CEILING:
        raise DefinitionError(
            "WORKFLOW_TOO_MANY_NODES",
            f"一张流程最多 {MAX_NODES_CEILING} 个节点（现在是 {len(raw_nodes)} 个）。",
        )

    max_nodes = _clamp_int(
        raw.get("max_nodes"),
        default=DEFAULT_MAX_NODES,
        low=1,
        high=MAX_NODES_CEILING,
    )
    nodes: dict[str, Node] = {}
    for item in raw_nodes:
        node = _parse_node(item)
        if node.id in nodes:
            raise DefinitionError(
                "WORKFLOW_DUPLICATE_NODE",
                f"节点 id「{node.id}」重复了。",
                node=node.id,
            )
        nodes[node.id] = node

    entry = str(raw.get("entry") or "").strip()
    if not entry:
        raise DefinitionError("WORKFLOW_NO_ENTRY", "流程必须指定入口节点（entry）。")
    if entry not in nodes:
        raise DefinitionError(
            "WORKFLOW_NO_ENTRY",
            f"入口节点「{entry}」不存在。",
            node=entry,
        )

    # ---- 边与连通性 ----
    for node in nodes.values():
        for edge in node.next:
            if edge.to not in nodes:
                raise DefinitionError(
                    "WORKFLOW_DANGLING_EDGE",
                    f"节点「{node.id}」指向了不存在的节点「{edge.to}」。",
                    node=node.id,
                )

    order = _topological_order(nodes, entry)

    unreachable = [nid for nid in nodes if nid not in order]
    if unreachable:
        raise DefinitionError(
            "WORKFLOW_UNREACHABLE_NODE",
            f"节点「{unreachable[0]}」从入口 {entry} 走不到。"
            "配了却永远不会跑的节点会让图看起来比实际能做的事多。",
            node=unreachable[0],
        )

    node_hops, depth_hops = _longest_paths(nodes, order)
    if max_nodes < node_hops:
        # 判据是**最长路径**而不是节点总数：一张 15 个节点、最长路径 5 的图
        # 配 max_nodes=8 是合理的（分支不会同时走）。但最长路径都装不下，
        # 这张图就注定在某条路径上跑到一半停下——那是配错了，不是运行时意外。
        raise DefinitionError(
            "WORKFLOW_NODE_BUDGET_TOO_SMALL",
            f"max_nodes={max_nodes} 小于最长路径的 {node_hops} 个节点，"
            "这条路径跑到一半就会被上限截断。请调大 max_nodes 或简化流程。",
        )

    preds = _predecessors(nodes)
    must = _must_reach(nodes, preds, entry, order)

    # ---- 模板与 inputs 的可达性 ----
    for nid in order:
        node = nodes[nid]
        _check_template(node, must[nid], node.task)
        for name, ref in node.inputs.items():
            _check_input_ref(node, name, ref, must[nid])
        for branch in node.branches:
            _check_template(node, must[nid], branch.task)

    return WorkflowPlan(
        entry=entry,
        nodes=nodes,
        max_nodes=max_nodes,
        order=order,
        depth_hops=depth_hops,
    )


def _parse_node(item: Any) -> Node:
    if not isinstance(item, dict):
        raise DefinitionError("WORKFLOW_INVALID_NODE", "节点必须是对象。")

    node_id = str(item.get("id") or "").strip()
    if not _ID_RE.match(node_id):
        raise DefinitionError(
            "WORKFLOW_BAD_NODE_ID",
            f"节点 id「{node_id}」不合法：只能用小写字母开头，"
            "后接小写字母、数字或下划线，最长 32 个字符。",
        )

    kind = str(item.get("kind") or KIND_AGENT).strip()
    if kind not in NODE_KINDS:
        raise DefinitionError(
            "WORKFLOW_BAD_KIND",
            f"节点「{node_id}」的类型「{kind}」不认识（只能是 agent / compose / parallel）。",
            node=node_id,
        )

    task = str(item.get("task") or "").strip()
    # 并行节点自己不执行任何任务——它下面每个分支各带 task，引擎也只渲染分支的。
    # 所以这里不要求它写，写了也只当说明用。曾经的写法是「所有节点都要 task」，
    # 结果是画图的人被要求填一段永远不执行的文字，而那段文字在运行记录里也看不到。
    if not task and kind != KIND_PARALLEL:
        raise DefinitionError(
            "WORKFLOW_NO_TASK",
            f"节点「{node_id}」没有任务描述。",
            node=node_id,
        )
    if len(task) > MAX_TASK_CHARS:
        raise DefinitionError(
            "WORKFLOW_TASK_TOO_LONG",
            f"节点「{node_id}」的任务描述超过 {MAX_TASK_CHARS} 字。"
            "模板只能取黑板里的结论，不需要把材料贴进来。",
            node=node_id,
        )

    agent = str(item.get("agent") or "").strip()
    emit_field, emit_chars, emit_values, emit_pattern = _parse_emit(
        item.get("emit"), node_id
    )
    inputs = _parse_inputs(item.get("inputs"), node_id)
    on_error = str(item.get("on_error") or "fail").strip()
    if on_error not in ("fail", "skip"):
        raise DefinitionError(
            "WORKFLOW_BAD_ON_ERROR",
            f"节点「{node_id}」的 on_error 只能是 fail 或 skip。",
            node=node_id,
        )

    branches: tuple[Branch, ...] = ()
    if kind == KIND_PARALLEL:
        if agent:
            raise DefinitionError(
                "WORKFLOW_PARALLEL_HAS_AGENT",
                f"并行节点「{node_id}」不能再指定 agent——每个分支各自指定。",
                node=node_id,
            )
        branches = _parse_branches(item.get("branches"), node_id)
    elif kind == KIND_COMPOSE:
        if agent:
            raise DefinitionError(
                "WORKFLOW_COMPOSE_HAS_AGENT",
                f"汇总节点「{node_id}」不调用智能体，不能指定 agent。",
                node=node_id,
            )
    else:
        if not agent:
            raise DefinitionError(
                "WORKFLOW_NO_AGENT",
                f"节点「{node_id}」没有指定执行的智能体。",
                node=node_id,
            )

    edges = _parse_edges(item.get("next"), node_id)

    return Node(
        id=node_id,
        kind=kind,
        agent=agent,
        task=task,
        emit_field=emit_field,
        emit_chars=emit_chars,
        emit_values=emit_values,
        emit_pattern=emit_pattern,
        inputs=inputs,
        on_error=on_error,
        next=edges,
        branches=branches,
    )


def _parse_emit(raw: Any, node_id: str) -> tuple[str, int, tuple[str, ...], str]:
    if raw is None:
        return "", DEFAULT_EMIT_CHARS, (), ""
    if not isinstance(raw, dict):
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT", f"节点「{node_id}」的 emit 必须是对象。", node=node_id
        )

    emit_field = str(raw.get("field") or "").strip()
    if emit_field and not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,31}$", emit_field):
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit.field「{emit_field}」不合法。",
            node=node_id,
        )

    chars = _clamp_int(
        raw.get("max_chars"),
        default=DEFAULT_EMIT_CHARS,
        low=1,
        high=MAX_EMIT_CHARS,
    )

    values = _parse_emit_values(raw.get("values"), node_id)
    pattern = _parse_emit_pattern(raw.get("pattern"), node_id)

    if (values or pattern) and not emit_field:
        # 不拒的话，配了 values 却没配 field，看起来「该分流」，实际什么都没发生。
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」配了 values/pattern 却没有 emit.field——抽取出来的值无处可存。",
            node=node_id,
        )
    if values and pattern:
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit 不能同时给 values 和 pattern"
            f"（实际存哪个取决于优先序，看图看不出来）。",
            node=node_id,
        )
    return emit_field, chars, values, pattern


def _parse_emit_values(raw: Any, node_id: str) -> tuple[str, ...]:
    """枚举抽取。只保留非空项并去重，保持声明顺序（同位置时按它取）。"""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit.values 必须是数组。",
            node=node_id,
        )

    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise DefinitionError(
                "WORKFLOW_BAD_EMIT",
                f"节点「{node_id}」的 emit.values 里只能放字符串。",
                node=node_id,
            )
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_EMIT_VALUE_CHARS:
            raise DefinitionError(
                "WORKFLOW_BAD_EMIT",
                f"节点「{node_id}」的 emit.values 里有一项太长"
                f"（{len(text)} 字，上限 {MAX_EMIT_VALUE_CHARS}）——枚举是给短标签用的。",
                node=node_id,
            )
        if text in seen:
            continue
        seen.add(text)
        out.append(text)

    if not out:
        return ()
    if len(out) > MAX_EMIT_VALUES:
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit.values 有 {len(out)} 项，"
            f"上限 {MAX_EMIT_VALUES}——再多就不叫枚举了。",
            node=node_id,
        )
    return tuple(out)


def _parse_emit_pattern(raw: Any, node_id: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit.pattern 必须是字符串。",
            node=node_id,
        )

    pattern = raw.strip()
    if not pattern:
        return ""
    if len(pattern) > MAX_EMIT_PATTERN_CHARS:
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit.pattern 太长（上限 {MAX_EMIT_PATTERN_CHARS} 字符）。",
            node=node_id,
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        # 保存时就炸，别等到某个节点真跑完才发现正则写错——那时候要重跑整条链。
        raise DefinitionError(
            "WORKFLOW_BAD_EMIT",
            f"节点「{node_id}」的 emit.pattern 不是合法正则：{exc}",
            node=node_id,
        ) from exc
    return pattern


def _parse_inputs(raw: Any, node_id: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DefinitionError(
            "WORKFLOW_BAD_INPUTS", f"节点「{node_id}」的 inputs 必须是对象。", node=node_id
        )

    out: dict[str, str] = {}
    for name, ref in raw.items():
        key = str(name).strip()
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,31}$", key):
            raise DefinitionError(
                "WORKFLOW_BAD_INPUTS",
                f"节点「{node_id}」的 inputs 键「{key}」不合法。",
                node=node_id,
            )
        out[key] = str(ref or "").strip()
    return out


def _parse_branches(raw: Any, node_id: str) -> tuple[Branch, ...]:
    if not isinstance(raw, list) or not raw:
        raise DefinitionError(
            "WORKFLOW_NO_BRANCHES",
            f"并行节点「{node_id}」至少要有一个分支。",
            node=node_id,
        )
    if len(raw) > MAX_BRANCHES:
        raise DefinitionError(
            "WORKFLOW_TOO_MANY_BRANCHES",
            f"节点「{node_id}」最多 {MAX_BRANCHES} 个并行分支"
            "（数据库连接池与供应商速率限制，不是拍脑袋）。",
            node=node_id,
        )

    out: list[Branch] = []
    seen: set[str] = set()

    for item in raw:
        if not isinstance(item, dict):
            raise DefinitionError(
                "WORKFLOW_BAD_BRANCH", f"节点「{node_id}」的分支必须是对象。", node=node_id
            )
        bid = str(item.get("id") or "").strip()
        if not _ID_RE.match(bid):
            raise DefinitionError(
                "WORKFLOW_BAD_BRANCH",
                f"节点「{node_id}」的分支 id「{bid}」不合法。",
                node=node_id,
            )
        if bid in seen:
            raise DefinitionError(
                "WORKFLOW_DUPLICATE_BRANCH",
                f"节点「{node_id}」的分支 id「{bid}」重复了。",
                node=node_id,
            )
        seen.add(bid)

        agent = str(item.get("agent") or "").strip()
        task = str(item.get("task") or "").strip()
        if not agent or not task:
            raise DefinitionError(
                "WORKFLOW_BAD_BRANCH",
                f"节点「{node_id}」的分支「{bid}」需要同时给出 agent 与 task。",
                node=node_id,
            )
        emit_raw = item.get("emit")
        emit_field = ""
        if isinstance(emit_raw, dict):
            emit_field = str(emit_raw.get("field") or "").strip()
        elif isinstance(emit_raw, str):
            emit_field = emit_raw.strip()

        out.append(Branch(id=bid, agent=agent, task=task, emit_field=emit_field))

    return tuple(out)


def _parse_edges(raw: Any, node_id: str) -> tuple[Edge, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DefinitionError(
            "WORKFLOW_BAD_NEXT", f"节点「{node_id}」的 next 必须是数组。", node=node_id
        )

    edges: list[Edge] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DefinitionError(
                "WORKFLOW_BAD_NEXT", f"节点「{node_id}」的边必须是对象。", node=node_id
            )
        to = str(item.get("to") or "").strip()
        if not to:
            raise DefinitionError(
                "WORKFLOW_BAD_NEXT", f"节点「{node_id}」有一条边没写 to。", node=node_id
            )

        when_raw = item.get("when")
        when = _parse_condition(when_raw, node_id) if when_raw is not None else None
        edges.append(Edge(when=when, to=to))

    fallbacks = [e for e in edges if e.when is None]
    if edges and not fallbacks:
        # 没有兜底边意味着「条件都不满足时流程静默死掉」——所有工作流引擎最经典的坑。
        # 校验时必须拒：跑到那一步才停的流程，在用户看来就是「点了没反应」。
        raise DefinitionError(
            "WORKFLOW_NO_FALLBACK",
            f"节点「{node_id}」没有兜底边（when=null）。"
            "条件都不满足时该走哪一步没有答案，流程会停在这里不动。",
            node=node_id,
        )

    if len(fallbacks) > 1:
        raise DefinitionError(
            "WORKFLOW_DUPLICATE_FALLBACK",
            f"节点「{node_id}」有多条兜底边（when=null）。"
            "条件都不满足时该走哪一条没有答案。",
            node=node_id,
        )

    # 兜底边必须排在最后：写前面会让后面所有条件边永远不成立，
    # 而图上看起来它们还在。这是「配了但不起作用」，比报错难查。
    if fallbacks and edges[-1].when is not None:
        raise DefinitionError(
            "WORKFLOW_FALLBACK_NOT_LAST",
            f"节点「{node_id}」的兜底边必须写在最后一条。",
            node=node_id,
        )

    return tuple(edges)


def _parse_condition(raw: Any, node_id: str) -> Condition:
    if not isinstance(raw, dict):
        raise DefinitionError(
            "WORKFLOW_BAD_CONDITION",
            f"节点「{node_id}」的条件必须是对象。",
            node=node_id,
        )

    key = str(raw.get("key") or "").strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,31}$", key):
        raise DefinitionError(
            "WORKFLOW_BAD_CONDITION",
            f"节点「{node_id}」的条件缺少合法的 key（取黑板上哪个键）。",
            node=node_id,
        )

    op = str(raw.get("op") or "eq").strip()
    if op not in OPS:
        raise DefinitionError(
            "WORKFLOW_BAD_CONDITION",
            f"节点「{node_id}」的条件算子「{op}」不支持（只能是 {'/'.join(OPS)}）。",
            node=node_id,
        )

    value = raw.get("value")
    if op == "in":
        if not isinstance(value, list) or not value:
            raise DefinitionError(
                "WORKFLOW_BAD_CONDITION",
                f"节点「{node_id}」的 in 条件需要给一个非空数组。",
                node=node_id,
            )
    elif op in ("gt", "lt"):
        try:
            float(value)
        except (TypeError, ValueError):
            raise DefinitionError(
                "WORKFLOW_BAD_CONDITION",
                f"节点「{node_id}」的 {op} 条件需要一个数字。",
                node=node_id,
            ) from None
    elif not isinstance(value, (str, int, float, bool)):
        raise DefinitionError(
            "WORKFLOW_BAD_CONDITION",
            f"节点「{node_id}」的条件值必须是标量。",
            node=node_id,
        )

    return Condition(key=key, op=op, value=value)


def _check_template(node: Node, reachable: set[str], text: str) -> None:
    """模板只能取**这条路径上必然出现过的**节点的结论。

    为什么是「必然」而不是「可能」：`{{nodes.a}}` 在一条不经过 a 的分支上渲染，
    只能给出空串——而空串会被模型当成「a 说没有问题」。要汇总多条分支的结论，
    用 `{{upstream}}`，它渲染的是「已经跑过的那些」，语义是确定的。
    """
    for match in _PLACEHOLDER_RE.finditer(text or ""):
        token = match.group(1)

        if token == "input.text" or token == "upstream":
            continue

        if token.startswith("input."):
            raise DefinitionError(
                "WORKFLOW_BAD_PLACEHOLDER",
                f"节点「{node.id}」用了不认识的占位符「{{{{{token}}}}}」"
                "（用户输入只有 input.text）。",
                node=node.id,
            )

        if token.startswith("inputs."):
            name = token.split(".", 1)[1]
            if name not in node.inputs:
                raise DefinitionError(
                    "WORKFLOW_UNKNOWN_INPUT",
                    f"节点「{node.id}」引用了 inputs.{name}，但 inputs 里没有这个键。",
                    node=node.id,
                )
            continue

        if not token.startswith("nodes."):
            raise DefinitionError(
                "WORKFLOW_BAD_PLACEHOLDER",
                f"节点「{node.id}」用了不认识的占位符「{{{{{token}}}}}」"
                "（只支持 input.text / nodes.<id> / nodes.<id>.<field> / inputs.<name> / upstream）。",
                node=node.id,
            )

        parts = token.split(".")
        target = parts[1]
        if target not in reachable or target == node.id:
            raise DefinitionError(
                "WORKFLOW_UNREACHABLE_REFERENCE",
                f"节点「{node.id}」引用了「{target}」的结论，"
                f"但「{target}」不在这条路径的必经节点上——跑到这里时它可能还没跑过。",
                node=node.id,
            )


def _check_input_ref(node: Node, name: str, ref: str, reachable: set[str]) -> None:
    match = _INPUT_REF_RE.match(ref)
    if match is None:
        raise DefinitionError(
            "WORKFLOW_BAD_INPUTS",
            f"节点「{node.id}」的 inputs.{name}「{ref}」写法不对"
            "（应为 $nodes.<id> 或 $nodes.<id>.<field>）。",
            node=node.id,
        )
    target = match.group(1)
    if target not in reachable or target == node.id:
        raise DefinitionError(
            "WORKFLOW_UNREACHABLE_REFERENCE",
            f"节点「{node.id}」的 inputs.{name} 引用了「{target}」，"
            "但它不在这条路径的必经节点上。",
            node=node.id,
        )


def _predecessors(nodes: dict[str, Node]) -> dict[str, set[str]]:
    preds: dict[str, set[str]] = {nid: set() for nid in nodes}
    for node in nodes.values():
        for edge in node.next:
            preds[edge.to].add(node.id)
    return preds


def _topological_order(nodes: dict[str, Node], entry: str) -> tuple[str, ...]:
    """从入口出发的拓扑序。顺带发现环。

    只保留入口可达的部分：不可达的节点由调用方单独报错，它们不进 order。
    """
    indegree: dict[str, int] = {nid: 0 for nid in nodes}
    reachable: set[str] = {entry}
    stack = [entry]

    while stack:
        current = stack.pop()
        for edge in nodes[current].next:
            indegree[edge.to] += 1 if edge.to not in reachable else 0
            if edge.to not in reachable:
                reachable.add(edge.to)
                stack.append(edge.to)

    # 重新算一遍入度，只统计可达来源：上面的增量算法遇到「同一个目标被访问两次」
    # 会算错，而这类错只会在复杂图上显形。
    indegree = {nid: 0 for nid in nodes}
    for nid in reachable:
        for edge in nodes[nid].next:
            if edge.to in reachable:
                indegree[edge.to] += 1

    ready = sorted(nid for nid in reachable if indegree[nid] == 0)
    order: list[str] = []

    while ready:
        current = ready.pop(0)
        order.append(current)
        for edge in nodes[current].next:
            if edge.to not in reachable:
                continue
            indegree[edge.to] -= 1
            if indegree[edge.to] == 0:
                ready.append(edge.to)
        ready.sort()

    if len(order) != len(reachable):
        # 找到环上的一个节点，报出来才有用。
        stuck = next(nid for nid in reachable if nid not in order)
        raise DefinitionError(
            "WORKFLOW_CYCLE",
            f"节点「{stuck}」处有环。只允许 DAG——要重复就显式写多个节点，"
            "不给自己留死循环的可能。",
            node=stuck,
        )

    return tuple(order)


def _must_reach(
    nodes: dict[str, Node],
    preds: dict[str, set[str]],
    entry: str,
    order: tuple[str, ...],
) -> dict[str, set[str]]:
    """每个节点的「必经集合」（含自身）。

    `must[v] = ∩_{u ∈ preds(v)} (must[u] ∪ {u})`。分支汇合处交集会缩小——
    这正是我们要的：汇合点不能引用只有一条分支才跑过的节点。
    """
    must: dict[str, set[str]] = {}

    for nid in order:
        if nid == entry or not preds[nid]:
            must[nid] = {nid}
            continue

        common: set[str] | None = None
        for pred in preds[nid]:
            if pred not in must:
                # 拓扑序保证前驱已经算过；真出现这种情况说明图不连通，
                # 那种情况在 `_topological_order` 之后已经报过错了。
                continue
            candidate = must[pred] | {pred}
            common = candidate if common is None else (common & candidate)

        must[nid] = (common or set()) | {nid}

    return must


def _longest_paths(nodes: dict[str, Node], order: tuple[str, ...]) -> tuple[int, int]:
    """最长路径的两个口径：`(节点数, 会调 Agent 的节点数)`。

    - 节点数用来校验 `max_nodes`：一次执行最多能跑几步；
    - Agent 节点数用来预检深度：`compose` 不调模型，也就不占委派跳数。
      混用会让一张 5 节点的图在第 3 个节点上因为深度而死——用户看不出是图配错了。
    """
    node_hops = {nid: 1 for nid in order}
    agent_hops = {nid: (1 if nodes[nid].invokes_agent else 0) for nid in order}

    for nid in order:
        for edge in nodes[nid].next:
            by_node = node_hops[nid] + 1
            if by_node > node_hops[edge.to]:
                node_hops[edge.to] = by_node

            by_agent = agent_hops[nid] + (1 if nodes[edge.to].invokes_agent else 0)
            if by_agent > agent_hops[edge.to]:
                agent_hops[edge.to] = by_agent

    return (
        max(node_hops.values()) if node_hops else 0,
        max(agent_hops.values()) if agent_hops else 0,
    )


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


# ---------------------------------------------------------------- 渲染与选边


def render(
    text: str,
    *,
    node: Node,
    blackboard: dict[str, dict[str, Any]],
    input_text: str,
    visited: list[str],
) -> str:
    """把 `{{...}}` 换成黑板上的结论。

    缺失就抛 `TemplateMissing`——**不**渲染成空串。空串最难查：下一个节点会
    拿着「什么都没说」当成「没有问题」，然后给出一段看起来完整的结论。
    """

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)

        if token == "input.text":
            return input_text

        if token == "upstream":
            return _render_upstream(blackboard, visited)

        if token.startswith("inputs."):
            name = token.split(".", 1)[1]
            return _render_input(node, name, blackboard)

        if token.startswith("nodes."):
            parts = token.split(".")
            target = parts[1]
            entry = blackboard.get(target)
            if entry is None:
                raise TemplateMissing(
                    "WORKFLOW_TEMPLATE_MISSING",
                    f"节点「{node.id}」要取「{target}」的结论，但这次执行里还没有它。",
                    node=node.id,
                )
            if len(parts) == 2:
                return str(entry.get("text") or "")
            keys = entry.get("keys") or {}
            value = keys.get(parts[2])
            if value is None:
                raise TemplateMissing(
                    "WORKFLOW_TEMPLATE_MISSING",
                    f"节点「{node.id}」要取「{target}」的字段「{parts[2]}」，"
                    "但那个节点没有产出这个字段。",
                    node=node.id,
                )
            return str(value)

        raise TemplateMissing(
            "WORKFLOW_BAD_PLACEHOLDER",
            f"节点「{node.id}」的占位符「{{{{{token}}}}}」不认识。",
            node=node.id,
        )

    return _PLACEHOLDER_RE.sub(_replace, text or "")


def _render_input(
    node: Node,
    name: str,
    blackboard: dict[str, dict[str, Any]],
) -> str:
    ref = node.inputs.get(name)
    if ref is None:
        raise TemplateMissing(
            "WORKFLOW_UNKNOWN_INPUT",
            f"节点「{node.id}」引用了 inputs.{name}，但 inputs 里没有这个键。",
            node=node.id,
        )

    match = _INPUT_REF_RE.match(ref)
    if match is None:
        raise TemplateMissing(
            "WORKFLOW_BAD_INPUTS",
            f"节点「{node.id}」的 inputs.{name}「{ref}」写法不对。",
            node=node.id,
        )

    target, field_name = match.group(1), match.group(2)
    entry = blackboard.get(target)
    if entry is None:
        raise TemplateMissing(
            "WORKFLOW_TEMPLATE_MISSING",
            f"节点「{node.id}」的 inputs.{name} 要取「{target}」，但它还没有产出。",
            node=node.id,
        )

    if field_name:
        value = (entry.get("keys") or {}).get(field_name)
        if value is None:
            raise TemplateMissing(
                "WORKFLOW_TEMPLATE_MISSING",
                f"节点「{node.id}」的 inputs.{name} 要取「{target}.{field_name}」，"
                "但那个字段不存在。",
                node=node.id,
            )
        return str(value)

    return str(entry.get("text") or "")


def _render_upstream(blackboard: dict[str, dict[str, Any]], visited: list[str]) -> str:
    """把已经跑过的节点结论按执行顺序拼起来。

    这是「汇总多条分支」唯一的正确写法：具体节点名在汇合点上不一定都跑过，
    而 `upstream` 的语义是确定的——已经产出的就是这些。
    """
    blocks: list[str] = []
    for nid in visited:
        entry = blackboard.get(nid) or {}
        text = str(entry.get("text") or "").strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def resolve_key(
    key: str,
    *,
    node: Node,
    blackboard: dict[str, dict[str, Any]],
    visited: list[str],
) -> Any:
    """条件取值：先看当前节点刚产出的字段，再按执行顺序倒着找最近一次写过该键的节点。"""
    own = (blackboard.get(node.id) or {}).get("keys") or {}
    if key in own:
        return own[key]

    for nid in reversed(visited):
        keys = (blackboard.get(nid) or {}).get("keys") or {}
        if key in keys:
            return keys[key]

    return None


def condition_holds(condition: Condition, value: Any) -> bool:
    """求值。`None` 一律不成立——「没产出这个键」不等于「它的值是空」。

    后者会让一条 `neq` 条件在分支没跑时意外命中，然后流程走进一条本不该走的路。
    """
    if value is None:
        return False

    if condition.op == "eq":
        return str(value) == str(condition.value)
    if condition.op == "neq":
        return str(value) != str(condition.value)
    if condition.op == "in":
        allowed = {str(item) for item in (condition.value or [])}
        return str(value) in allowed
    if condition.op in ("gt", "lt"):
        try:
            left = float(value)
            right = float(condition.value)
        except (TypeError, ValueError):
            return False
        return left > right if condition.op == "gt" else left < right

    return False


def agent_edges(plan: WorkflowPlan) -> list[tuple[str, str]]:
    """图里声明了哪些「Agent 之间」的边。给拓扑对账用。

    只取能确定的部分（`docs/ORCHESTRATION.md` §7）：

    - `agent → agent`：确定；
    - `agent → parallel`：确定（并行节点一进来，每个分支各调一次那个 Agent）；
    - `parallel → 任何`：**不取**。并行之后引擎用的是「进入并行之前」的父上下文，
      静态图上无法确定下一个节点实际由谁发起——硬画一条边只会在对账时变成
      一条假的「跑过但没声明」。
    - `compose` 两端都不参与：它不调用任何人。
    """
    out: list[tuple[str, str]] = []

    for nid in plan.order:
        node = plan.nodes[nid]
        if node.kind != KIND_AGENT:
            continue

        for edge in node.next:
            target = plan.nodes[edge.to]
            if target.kind == KIND_AGENT:
                out.append((node.agent, target.agent))
            elif target.kind == KIND_PARALLEL:
                for branch in target.branches:
                    out.append((node.agent, branch.agent))

    return out


def pick_edge(
    node: Node,
    *,
    blackboard: dict[str, dict[str, Any]],
    visited: list[str],
) -> str | None:
    """按顺序判断边，第一条命中的胜出；兜底边排最后（校验时已保证）。

    返回 None = 条件全不满足且没有兜底边。校验时已经禁止了这种图，
    所以这里返回 None 只可能发生在手改过数据库的情况下——引擎会据此判失败，
    并说清是「这条边没人接」而不是「跑完了」。
    """
    for edge in node.next:
        if edge.when is None:
            return edge.to
        value = resolve_key(
            edge.when.key,
            node=node,
            blackboard=blackboard,
            visited=visited,
        )
        if condition_holds(edge.when, value):
            return edge.to
    return None
