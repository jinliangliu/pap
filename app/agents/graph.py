# app/agents/graph.py
"""执行图装配。

    START
      → begin_turn                        清掉上一轮的单轮状态（工具结果、步数、计划）
      → plan_request                      模型选工具（含 OCR 等按需工具）
          ├─ chat / clarify / error  → respond
          ├─ read_tool  → execute_read_tool ─┬─ 还有步数 → plan_request（回路）
          │                                  └─ 收敛     → respond
          ├─ knowledge  → retrieve_knowledge → respond
          └─ write_tool → create_and_wait_approval（interrupt）→ respond
      → persist_state
      → END

回路的意义：工具是模型自主选用的，不是流程里写死的节点。
一轮工具执行的结果回灌给规划器，模型据此决定继续调用还是作答；
步数上限（AGENT_MAX_TOOL_STEPS）保证回路一定收敛。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.nodes import (
    begin_turn,
    create_and_wait_approval,
    execute_read_tool,
    persist_state,
    plan_request,
    respond,
    retrieve_knowledge,
    route_after_plan,
    route_after_read_tool,
    traced,
)
from app.agents.state import AgentState


def build_agent_graph(
    checkpointer,
    *,
    prelude: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
):
    """
    装配执行图。

    prelude 仅给 LangGraph Studio 使用：Studio 只提供 messages，
    需要一个入口节点补齐 tenant_id / user_id / roles 等运行时事实。
    生产链路由 chat_service 构造完整状态，prelude 保持 None。
    """
    builder = StateGraph(AgentState)

    # begin_turn 必须在入口处：它清掉上一轮留在 checkpoint 里的工具结果与步数，
    # 否则新一轮会把旧结果当成本轮工具的输出（详见 begin_turn 的说明）。
    #
    # 每个节点都套一层 traced：详情页的耗时聚合要靠「每个节点一条 span」才算得出来
    # 用户感知的「这一步在干什么」。漏掉 begin_turn / respond 这种不碰数据库的
    # 节点，图上就会出现无归属的空档。
    builder.add_node("begin_turn", traced(begin_turn))
    builder.add_node("plan_request", traced(plan_request))
    builder.add_node("execute_read_tool", traced(execute_read_tool))
    builder.add_node("retrieve_knowledge", traced(retrieve_knowledge))
    builder.add_node("create_and_wait_approval", traced(create_and_wait_approval))
    builder.add_node("respond", traced(respond))
    builder.add_node("persist_state", traced(persist_state))

    entry: Any = START

    if prelude is not None:
        builder.add_node("prepare_context", traced(prelude))
        builder.add_edge(START, "prepare_context")
        entry = "prepare_context"

    builder.add_edge(entry, "begin_turn")
    builder.add_edge("begin_turn", "plan_request")

    builder.add_conditional_edges(
        "plan_request",
        route_after_plan,
        {
            "respond": "respond",
            "execute_read_tool": "execute_read_tool",
            "retrieve_knowledge": "retrieve_knowledge",
            "create_and_wait_approval": "create_and_wait_approval",
        },
    )

    # 只读工具执行完不直接收尾：结果回到规划器，由模型决定下一步。
    builder.add_conditional_edges(
        "execute_read_tool",
        route_after_read_tool,
        {
            "plan_request": "plan_request",
            "respond": "respond",
        },
    )

    builder.add_edge("retrieve_knowledge", "respond")
    builder.add_edge("create_and_wait_approval", "respond")
    builder.add_edge("respond", "persist_state")
    builder.add_edge("persist_state", END)

    return builder.compile(checkpointer=checkpointer)


_compiled = None


def init_agent_graph(checkpointer) -> None:
    global _compiled
    _compiled = build_agent_graph(checkpointer)


def get_agent_graph():
    if _compiled is None:
        raise RuntimeError(
            "执行图尚未初始化，请在应用 lifespan 中调用 init_agent_graph()。"
        )

    return _compiled
