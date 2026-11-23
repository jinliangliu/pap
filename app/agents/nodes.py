# app/agents/nodes.py
"""执行图节点。

分工：
- plan_request          意图识别 + 工具选择 + 槽位校验（不执行）
- execute_read_tool     只读工具执行
- retrieve_knowledge    知识检索 + 可追溯引用
- create_and_wait_approval  生成预览 → interrupt → 审批后执行 → 回读核验
- respond               组装最终答复
- persist_state         落运行记录
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from app.agents.clarify import has_choices, normalize_items
from app.agents.planner import (
    ModelInvocationError,
    ModelNotConfiguredError,
    create_action_plan,
)
from app.agents.schemas import ActionType
from app.agents.slots import (
    build_slot_question,
    get_missing_required_slots,
    merge_slots,
)
from app.agents.state import AgentState
from app.core.config import settings
from app.db.session import async_session_factory
from app.knowledge.compose import ComposeResult, compose_answer
from app.knowledge.service import answer_from_knowledge
from app.models.conversation import RunStatus
from app.observability.callbacks import stash_usage_for_run
from app.observability.context import current_operation_id, current_trace, span
from app.services import agent_service, memory_service, skill_service
from app.services.agent_invoker import context_from_state
from app.services.agent_service import AgentBoundary, render_prompt_block
from app.services.approval_dependencies import get_approval_service
from app.services.approval_service import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalParameterMismatchError,
    ApprovalStateError,
)
from app.services.delegation_limits import DelegationLimits
from app.services.run_service import finish_run
from app.tenancy.context import tenant_scope
from app.tools.catalog import build_model_tool_catalog
from app.tools.dependencies import get_tool_gateway
from app.tools.gateway import (
    ToolApprovalRequiredError,
    ToolGatewayError,
    ToolPermissionDeniedError,
    ToolPreflightRejectedError,
    ToolRateLimitedError,
    ToolResourceDeniedError,
)
from app.tools.types import ToolExecutionContext, ToolExecutionResult

logger = logging.getLogger(__name__)

MODEL_ERROR_MESSAGES = {
    "model_not_configured": "当前未配置模型服务，无法处理该请求。请联系管理员。",
    "model_auth_failed": "模型认证失败，请联系管理员检查模型密钥配置。",
    "model_rate_limited": "模型服务当前限流，请稍后重试。",
    "model_timeout": "模型响应超时，本次请求未完成，请稍后重试。",
    "model_network_error": "模型服务网络异常，请稍后重试。",
    "model_invalid_response": "模型返回内容无法解析，本次请求未完成。",
    "model_invalid_request": "当前模型不接受该请求格式，请联系管理员检查模型兼容性。",
    "model_unavailable": "当前模型服务暂时不可用，请稍后重试。",
}


def _context(state: AgentState) -> ToolExecutionContext:
    """工具上下文。实现在 `agent_invoker.context_from_state`（唯一一份）。"""
    return context_from_state(state)


def _tenant_bound(node: Callable[[AgentState], Awaitable[dict[str, Any]]]):
    """
    给节点套上租户上下文。

    RLS 的过滤条件取自会话事务里的 `app.tenant_id`，而这个值由 contextvar
    提供。生产链路是 chat_service 在外面包了一层 `tenant_scope`；Studio 入口
    只补齐了身份字段，没有设置上下文，于是：

    - 读租户策略拿到 0 行，策略形同未配置；
    - 审计/审批写库撞 `row-level security policy` 报错（审计被吞掉，
      审批直接 500）。

    同租户重入是允许的（见 `tenancy.context.tenant_scope`），所以生产路径
    再包一层不改变行为。
    """

    @functools.wraps(node)
    async def wrapper(state: AgentState) -> dict[str, Any]:
        with tenant_scope(state["tenant_id"]):
            return await node(state)

    return wrapper


def traced(node: Callable[[AgentState], Awaitable[dict[str, Any]]]):
    """给节点开一个观测 span。

    与 `_tenant_bound` 分开，是因为两者管的不是一件事：租户上下文只有需要
    读库的节点才要，而**每个**节点都该在观测里占一条 span。合成一个装饰器的话，
    像 `begin_turn`、`respond` 这种不碰数据库的节点就会从观测里消失——
    而 `respond` 恰恰是最终答复成型的那个节点。

    具体在哪装配由 `graph.py` 决定（那里才知道这次跑的是哪几个节点）。
    """

    @functools.wraps(node)
    async def wrapper(state: AgentState) -> dict[str, Any]:
        async with span(node.__name__, "node") as handle:
            result = await node(state)
            if isinstance(result, dict):
                # 只记「这个节点产出了哪些字段」，不记字段值——
                # 值已经由 llm/tool span 各记各的了，重复存会把体积翻倍。
                handle.add(output_keys=",".join(sorted(result.keys()))[:300] or None)
            return result

    return wrapper


# 每一轮请求开始时必须归零的字段：它们描述「本次请求做了什么」，
# 而不是「这个会话曾经做过什么」。
_PER_TURN_FIELDS: dict[str, Any] = {
    "intent": None,
    "tool_observations": [],
    "tool_steps": 0,
    "final_response": None,
    "missing_fields": [],
    # 追问选项也是「本轮产出了什么」。留着上一轮的 items，本轮若同样走
    # clarify 却只写了一句文字，界面会继续显示上一轮那张已经答过的卡片。
    "clarify_items": [],
    "response_metadata": None,
    "error_category": None,
    "approval_id": None,
    "approval_preview": None,
    "execution_status": None,
    "pending_delegated_approval": None,
    "planned_reason": None,
    "planned_tool_name": None,
    "planned_arguments": None,
}


async def begin_turn(state: AgentState) -> dict[str, Any]:
    """清掉上一轮留下的单轮状态。

    LangGraph 会把 checkpoint 里「本轮输入没有提到」的通道原样带进来，
    而 chat_service 只提交了消息、附件与身份。于是上一轮的
    tool_observations / tool_steps / planned_* / final_response 全部沿用：

    1. `_render_observations` 把上一轮的工具结果拼进本轮的 user_message，
       模型会把它当成本轮附件的识别结果。2026-11-05 实测：同一会话先传图片
       「1.jpg」、再传 PDF 测试报告，第二轮模型回答「识别结果与文件名不符」——
       它看到的「已执行的 OCR」其实是上一轮那张调压箱照片的识别文本，
       而本轮 PDF 一次都没被识别（整轮耗时 3.8 秒）。
    2. `tool_steps` 从上一轮的值起步，步数预算被凭空吃掉。
    3. `_repeats_last_observation` 拿上一轮的结果比对，工具与会话相同时
       直接回放旧结果，新一轮的查询根本没有执行。
    4. `final_response` 残留时，`respond` 可能把上一轮的答案再发一遍。

    例外：上一轮以「缺槽位」收尾时保留 planned_tool_name / planned_arguments，
    多轮补参靠它（第一轮缺 station_id，用户补充后第二轮沿用已确认参数）。
    那一轮并没有执行过工具，所以 observations 与步数照常清零。
    """
    reset = dict(_PER_TURN_FIELDS)
    # 每个 checkpoint 各自持有一份，别把同一个 list 对象写进多轮状态。
    reset["tool_observations"] = []
    reset["missing_fields"] = []
    reset["clarify_items"] = []

    carried = bool(state.get("missing_fields")) and bool(state.get("planned_tool_name"))
    if carried:
        reset["planned_tool_name"] = state.get("planned_tool_name")
        reset["planned_arguments"] = state.get("planned_arguments")

    dropped = len(state.get("tool_observations") or [])
    steps = int(state.get("tool_steps") or 0)
    if dropped or steps or state.get("final_response"):
        logger.info(
            "新一轮请求，已重置上一轮的单轮状态。",
            extra={
                "event": "agent_turn_reset",
                "dropped_observations": dropped,
                "dropped_steps": steps,
                "carried_slots": carried,
            },
        )

    return reset


def _last_user_text(state: AgentState) -> str:
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", None) == "human":
            return str(message.content).strip()

    return ""


# 这些键回答的是「怎么取的」，不是「取到了什么」。用户不需要看到
# success / data_source / timestamp 这一层——它们只会把结论埋掉。
_META_KEYS = frozenset(
    {
        "success",
        "data_source",
        "timestamp",
        "demo_mode",
        "truncated",
        "original_chars",
        "excerpt",
        "note",
        "scope",
        "query",
        # 查询对象（agent / tool / run …）：这是工具的入参回显，不是结果。
        # 漏在这里的代价是用户会看到「resource：tool」「total：99」这种内部字段，
        # 混在答案后面像半截报文。
        "resource",
        # 权限元数据：回答的是「你的授权怎么配的」，不是「你查到了什么」。
        # 需要解释可见范围时，由 message 用自然语言说明，不列字段名。
        "total_authorized",
        "visible_to_identity",
        "identity_station_filter",
        # 结构图是给界面画的，不是给人读的：混进 _render_payload 会变成
        # 「graph：{entry: check, nodes: [...]}」这种整段 JSON，把结论埋掉。
        "graph",
    }
)

# 列表类结果按条目渲染，不整段丢 JSON。
_COLLECTION_KEYS = (
    "stations",
    "records",
    "items",
    "rows",
    "alarms",
    "points",
    "series",
    "channels",
    "available_channels",
    "files",
)

_COLLECTION_LABELS = {
    "stations": "站点",
    "records": "记录",
    "items": "条目",
    "rows": "行",
    "alarms": "报警",
    "points": "测点",
    "series": "曲线",
    "channels": "通道",
    "available_channels": "可选通道",
    "files": "文件",
}

# 只有量词是「个」的列表才适合说「共 N 个 X」。
_HEADLINE_KEYS = ("stations", "records", "items", "rows", "alarms", "points")

# 说明类字段：一条一句完整的话，并成一行会读成一句长句。
_BULLET_KEYS = frozenset({"notes", "blockers", "warnings", "hints", "errors"})

# 这些 message 只说「跑完了」，没回答用户问的任何东西。
_GENERIC_MESSAGES = frozenset({"已完成远端调用。", "已完成。"})

# 条目里优先当作标题的字段，命中就用它组「名称（编号）」。
_ITEM_TITLE_FIELDS = ("name", "station_name", "title", "short_name", "label")
_ITEM_ID_FIELDS = ("serial", "station_id", "id", "code", "device_code", "access")

_MAX_LISTED_ITEMS = 20
_MAX_INLINE_ITEMS = 8
_SCALAR_PREVIEW_CHARS = 300


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _is_flat(value: dict[str, Any]) -> bool:
    """对象里没有嵌套结构（空列表算没有）。这种对象一行就能说完。"""
    return all(_is_scalar(item) or (isinstance(item, list) and not item) for item in value.values())


def _clip(text: str) -> str:
    if len(text) <= _SCALAR_PREVIEW_CHARS:
        return text
    return text[:_SCALAR_PREVIEW_CHARS] + "…"


def _render_inline(value: dict[str, Any]) -> str:
    """扁平对象排成「键=值，键=值」。整段 JSON 会撑满屏幕又读不出重点。"""
    parts = []
    for key, item in value.items():
        if isinstance(item, list):
            parts.append(f"{key}=空")
        elif item is None:
            continue
        else:
            parts.append(f"{key}={item}")

    return _clip("，".join(parts))


def _render_scalar_list(value: list[Any]) -> str:
    """标量列表排成「a、b、c」，长列表说清总数。"""
    items = [str(item) for item in value[:_MAX_INLINE_ITEMS]]
    text = "、".join(items)
    if len(value) > _MAX_INLINE_ITEMS:
        text += f" …（共 {len(value)} 项）"
    return _clip(text)

# 纯计数键。message 已经解释了它们时不必再列一遍。
_COUNT_KEYS = frozenset({"count", "total"})


def _render_item(item: Any, index: int) -> str:
    if not isinstance(item, dict):
        return f"{index}. {str(item)[:120]}"

    # 曲线条目：用户问「多少点、峰值多少」，点数与区间就是答案。
    if "points" in item:
        label = str(item.get("name") or item.get("access") or "")
        text = f"{item.get('points')} 个点"
        lower, upper = item.get("min"), item.get("max")
        if lower is not None and upper is not None:
            text += f"，范围 {lower}~{upper}"
        elif item.get("points") in (0, "0"):
            text += "（该时间段没有采样数据）"
        return f"{index}. {label}：{text}" if label else f"{index}. {text}"

    title = next((str(item[k]) for k in _ITEM_TITLE_FIELDS if item.get(k)), "")
    ident = next((str(item[k]) for k in _ITEM_ID_FIELDS if item.get(k)), "")

    # 站名本身常带编号（"硬件测试(T002511111)"），再括注一次就是重复。
    if title and ident and ident not in title:
        return f"{index}. {title}（{ident}）"

    if title or ident:
        return f"{index}. {title or ident}"

    return f"{index}. {_render_inline(item)}"


def _collection_headline(data: dict[str, Any]) -> str:
    """
    结果主体是一个列表时，给一句能直接回答「有多少」的开头。

    「已完成远端调用」对「列出所有站」这种请求等于没答，而条数正是他要的。
    只对「个」作量词的列表生效：曲线该说「条」，套用这里是病句。
    """
    filled = [
        (key, value)
        for key, value in data.items()
        if key in _HEADLINE_KEYS and isinstance(value, list) and value
    ]

    if len(filled) != 1:
        return ""

    key, value = filled[0]
    return f"共 {len(value)} 个{_COLLECTION_LABELS.get(key, key)}。"


def _render_measurement(value: dict[str, Any]) -> str:
    """
    测点结构：{name, value, unit, status_text, limit_*}。

    这是远端最常用的返回形状（进出口压力、阀位、流量都一样）。
    整段 JSON 化后用户得自己找 value 在哪、单位是什么，所以这里直接
    组一句「进口压力：1.538 kPa（正常）」。
    """
    label = str(value.get("name") or "")
    reading = value.get("value")
    unit = str(value.get("unit") or "")
    status = str(value.get("status_text") or "")

    text = f"{reading}{f' {unit}' if unit else ''}"

    if status:
        text += f"（{status}）"

    return f"{label}：{text}" if label else text


def _render_struct(key: str, value: dict[str, Any]) -> str:
    """嵌套对象尽量说成人话，说不成再退回 JSON。"""
    if "value" in value and ("name" in value or "unit" in value):
        return _render_measurement(value)

    # 站点对象：station_name 里已经带了序列号。
    if value.get("station_name"):
        return str(value["station_name"])

    # 扁平对象（时间窗、产物目录）一行说完。
    if _is_flat(value):
        return f"{key}：{_render_inline(value)}"

    # 真正嵌套的对象很少见。宁可说「有几项明细」，
    # 也不要把 JSON 截成 `"csv_path": nul…` 这种半截报文。
    return f"{key}：共 {len(value)} 项明细（结构较深，未展开）"


def _render_payload(data: dict[str, Any]) -> str:
    """
    把工具返回排成用户能读的文本。

    读工具这条路径**不经过模型**——结果就是最终答复，所以这里不是
    「给模型看的摘要」，而是「给人看的结果」。整段 JSON 丢给用户，
    等于让他自己解析报文。
    """
    filled = [
        (key, value)
        for key, value in data.items()
        if isinstance(value, list) and value
    ]
    # 只有一个列表时，条数由 headline 说，这里不再重复标签。
    needs_label = len(filled) > 1

    lines: list[str] = []

    for key, value in data.items():
        if key in _META_KEYS:
            continue

        # 计数类字段一律不渲染：有列表时它与列表长度重复，没有列表时它也不构成答案
        # （「total：99」单独一行，读者只会以为报文被截断了）。
        if key in _COUNT_KEYS:
            continue

        if isinstance(value, list):
            if not value:
                continue

            label = _COLLECTION_LABELS.get(key, key)

            # 说明类字段一条一行：四条注意事项并成一句没人读得下去。
            if key in _BULLET_KEYS and all(_is_scalar(item) for item in value):
                lines.append(f"{label}：")
                lines.extend(f"  · {item}" for item in value[:_MAX_LISTED_ITEMS])
                continue

            # 标量列表（通道名、备注）一行排完，不必编号。
            if all(_is_scalar(item) for item in value):
                lines.append(f"{label}：{_render_scalar_list(value)}")
                continue

            if needs_label:
                lines.append(f"{label}：")

            for index, item in enumerate(value[:_MAX_LISTED_ITEMS], 1):
                # 有标签行时才缩进；单列表直接顶格，避免整段右移两格。
                lines.append(("  " if needs_label else "") + _render_item(item, index))
            if len(value) > _MAX_LISTED_ITEMS:
                lines.append(f"  …另有 {len(value) - _MAX_LISTED_ITEMS} 条未列出")
            continue

        if isinstance(value, dict):
            lines.append(_render_struct(key, value))
            continue

        if value is None or value == "":
            continue

        lines.append(f"{key}：{value}")

    return "\n".join(lines)


def _has_substantive_data(data: dict[str, Any]) -> bool:
    """
    这个 payload 除了「空集合 + 计数」之外还有别的东西吗？

    空白名单的站点查询返回的就是 {count: 0, total_authorized: 10,
    visible_to_identity: 0, stations: []}。message 已经把这几个数字
    解释了，再列一遍只是噪音。
    """
    for key, value in data.items():
        if key in _META_KEYS or key in _COUNT_KEYS:
            continue
        if isinstance(value, list) and not value:
            continue
        return True

    return False


def _empty_result_note(data: Any) -> str:
    """空结果要区分「没有数据」和「没有权限」。

    listStations 的 count=0 常被模型读成「当前身份看不到任何站点」，
    而同一份结果里明明写着 visible_to_identity=10——用户会以为授权坏了。
    站点列表是唯一带这两个字段的工具，所以只在两者同时出现时提示。
    """
    if not isinstance(data, dict):
        return ""

    visible = data.get("visible_to_identity")
    if data.get("count") != 0 or not isinstance(visible, int) or visible <= 0:
        return ""

    return f"注意：该身份可见 {visible} 个站点，本次是查询条件没有匹配到，不是没有站点授权。"


def _result_graph(result: ToolExecutionResult | None) -> dict[str, Any]:
    """工具若要「把结果画出来」，就把图放在 `result["graph"]` 这个固定键上。

    约定这一个键而不是让每个工具各起名字：气泡那侧只有一份渲染代码，键名一分叉
    就变成「有的工具能画、有的画不出来」，而这种不一致没人会去报 bug。
    非 dict 一律当没给——气泡宁可少一张图，也不能因为格式不对整条消息渲染失败。
    """
    if result is None:
        return {}

    graph = (result.result or {}).get("graph")
    return {"graph": graph} if isinstance(graph, dict) and graph.get("nodes") else {}


def _summarize_result(result: ToolExecutionResult) -> str:
    if not result.success:
        return result.message or "工具未成功完成。"

    message = result.message or "已完成远端调用。"
    data = result.result or {}

    if _has_substantive_data(data):
        if message in _GENERIC_MESSAGES:
            message = _collection_headline(data) or message

        rendered = _render_payload(data)
        parts = [message, rendered] if rendered else [message]
    else:
        parts = [message]

    note = _empty_result_note(data)
    if note:
        parts.append(note)

    if not result.verified:
        parts.append("注意：该结果尚未通过回读核验。")

    return "\n\n".join(parts)


def _render_observations(state: AgentState) -> str:
    """把已执行工具的结果渲染成给规划器的观察块。

    这是「模型自主决定何时调用工具」的关键：OCR 之类的工具执行完，
    结果回到规划器面前，模型据此决定继续调用下一个工具还是直接作答。
    """
    observations = state.get("tool_observations") or []
    if not observations:
        return ""

    blocks: list[str] = []

    for index, item in enumerate(observations, start=1):
        tool_name = item.get("tool_name") or "unknown"
        text = (item.get("text") or "").strip()

        if not item.get("success"):
            blocks.append(f"{index}. {tool_name} 调用失败：{text}")
            continue

        blocks.append(f"{index}. {tool_name} 返回：\n{text}")

    return (
        "[本轮工具结果]\n"
        + "\n\n".join(blocks)
        + "\n\n请基于以上结果判断：信息足够就直接作答（action=chat）；"
        "还需要其他工具就继续调用，但不要用相同参数重复调用同一个工具。"
    )


def _repeats_last_observation(
    state: AgentState,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    observations = state.get("tool_observations") or []
    if not observations:
        return False

    last = observations[-1]
    return last.get("tool_name") == tool_name and last.get("arguments") == arguments


@_tenant_bound
async def _memory_block(state: AgentState) -> str:
    """本轮要注入的长期记忆。

    失败就当没有：记忆是让模型少问两句的加分项，
    查库出问题不能变成「对话打不开」。和观测写入失败的处理一致——
    辅助能力绝不反向拖垮主流程。
    """
    if not settings.memory_enabled:
        return ""
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        return ""
    try:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                rows = await memory_service.load_for_prompt(
                    session, user_id=state.get("user_id") or None
                )
        return memory_service.render_prompt_block(rows)
    except Exception:
        logger.warning(
            "读取长期记忆失败，本轮不注入。",
            extra={"event": "memory_load_failed"},
            exc_info=True,
        )
        return ""


def _boundary(state: AgentState) -> AgentBoundary:
    """从 state 还原边界。

    只还原、不重新查库：一次对话里 Agent 的边界应当是稳定的快照。
    若中途改了配置，本轮仍按进入时的边界执行——否则同一轮内前后两个工具
    可能适用两套规则，出了问题没人复现得出来。
    """
    allowed_tools = state.get("agent_allowed_tools")
    allowed_skills = state.get("agent_allowed_skills")
    allowed_connectors = state.get("agent_allowed_connectors")

    return AgentBoundary(
        agent_id=state.get("agent_id"),
        agent_name=state.get("agent_name") or "",
        display_name=state.get("agent_display_name") or "",
        revision=int(state.get("agent_revision") or 0),
        allowed_tools=None if allowed_tools is None else tuple(allowed_tools),
        allowed_skills=None if allowed_skills is None else tuple(allowed_skills),
        # 漏了这一行的表现是「配了连接器却还是全都看不见」，而配置页面上
        # 显示得好好的——因为 state 里从来没有这一项。
        allowed_connectors=(
            None if allowed_connectors is None else tuple(allowed_connectors)
        ),
        can_write=bool(state.get("agent_can_write", True)),
        max_delegation_depth=state.get("agent_max_delegation_depth"),
        delegation_budget=state.get("agent_delegation_budget"),
        instruction=state.get("agent_instruction") or "",
    )


DELEGATE_TOOL = "delegate_to_agent"


@_tenant_bound
async def _delegation_block(state: AgentState) -> str:
    """把「可以转交的智能体」列给规划器。

    没有这一段，模型只能靠名字猜目标——它会挑一个听起来最像的，
    然后在 409/404 上白走一轮。技能与记忆一样，查库失败就当没有：
    少一段提示词不影响这一轮回答，让对话打不开才是事故。
    """
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        return ""

    try:
        async with async_session_factory() as session:
            rows = await agent_service.delegatable_agents(
                session,
                tenant_id=tenant_id,
                exclude=state.get("agent_name") or None,
            )
            # 只列真能转交的：把边界更宽的目标也列出来，模型会挑一个、
            # 然后撞在子集校验上，白烧一轮。判定用同一个函数，不另写一套。
            parent = _boundary(state)
            rows = [
                row
                for row in rows
                if agent_service.agent_subset(agent_service.boundary_of(row), parent)[0]
            ]
    except Exception:
        logger.warning("读取可转交智能体失败。", extra={"event": "delegation_targets_failed"})
        return ""

    if not rows:
        return ""

    lines = ["## 可转交的智能体"]
    for row in rows:
        duty = (row.description or "").strip().splitlines()[0][:120] if row.description else "未填写职责"
        lines.append(f"- {row.name}（{row.display_name}）：{duty}")

    lines.append(
        "遇到不属于你职责范围、而上面某个智能体更专业的事，用 "
        f"{DELEGATE_TOOL} 把任务交给它。task 要写成对方看不到这段对话也能独立完成的一句话，"
        "不要复述整段对话，也不要转交给自己；对方返回的结论与缺失项照实转达。"
    )
    return "\n".join(lines)


@_tenant_bound
async def _skill_pick(
    state: AgentState,
) -> tuple[Any, str, str] | None:
    """本轮命中的技能。与记忆一样，失败就当没配。

    技能是让模型按既定步骤作业的加分项；查库出问题不能变成「对话打不开」。
    """
    if not settings.skills_enabled:
        return None
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        return None
    try:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                return await skill_service.pick(session, _last_user_text(state))
    except Exception:
        logger.warning(
            "读取技能失败，本轮不注入。",
            extra={"event": "skill_load_failed"},
            exc_info=True,
        )
        return None


async def _record_skill_hit(
    state: AgentState,
    skill: Any,
    matched_by: str,
    keyword: str,
) -> None:
    """记一次命中。失败只记日志。

    命中统计是评估指标，不是业务事实；写不进去不该让这一轮对话失败。
    """
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        return
    run_id = str(state.get("run_id") or "")
    try:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                if await skill_service.was_hit(session, run_id):
                    # 同一轮的重规划：只在第一次命中时记账。
                    return
                await skill_service.record_hit(
                    session,
                    skill=skill,
                    matched_by=matched_by,
                    keyword=keyword,
                    thread_id=state.get("thread_id"),
                    run_id=run_id or None,
                    trace_id=(current_trace().trace_id if current_trace() else None),
                    user_id=state.get("user_id"),
                )
                await session.commit()
    except Exception:
        logger.warning(
            "记录技能命中失败。",
            extra={"event": "skill_hit_record_failed", "skill": getattr(skill, "name", "")},
            exc_info=True,
        )


async def plan_request(state: AgentState) -> dict[str, Any]:
    gateway = get_tool_gateway()
    context = _context(state)

    # 工具目录是同步生成的，先刷新租户策略，
    # 否则「本租户已禁用某工具」要等下一个缓存周期才反映到目录里。
    await gateway.refresh_tenant_policy(context.tenant_id)

    user_message = _last_user_text(state)
    observations = _render_observations(state)
    if observations:
        user_message = f"{user_message}\n\n{observations}"

    known_user_facts = await _memory_block(state)

    # 更早的对话：摘要 + 最近几轮原文，由 `context_service` 在轮间算好。
    # 空值不放键（沿用 known_user_facts 的写法），免得模型去猜「系统是不是漏给了」。
    context_block = state.get("context_block") or {}
    conversation_summary = str(context_block.get("summary") or "")
    recent_turns = context_block.get("turns") or []

    catalog = build_model_tool_catalog(gateway=gateway, context=context)

    # 被边界裁掉的工具不会走到「拒绝」那一步，只能在这里主动记一笔：
    # 否则「这个 Agent 的边界配窄了」在运营面上毫无痕迹。
    trace = current_trace()
    if trace is not None and context.agent_name:
        trace.attrs["agent_revision"] = context.agent_revision
    hidden = _agent_hidden_tools(gateway, context)
    if hidden:
        if trace is not None:
            trace.attrs["agent_hidden_tools"] = hidden
        logger.info(
            "智能体边界减少了可用工具。",
            extra={
                "event": "agent_hidden_tools",
                "agent": context.agent_name,
                "hidden_count": len(hidden),
                "hidden_sample": hidden[:10],
            },
        )
    skill_block = ""
    skill_meta: dict[str, Any] | None = None
    boundary = _boundary(state)
    agent_block = render_prompt_block(boundary)

    # 只有委派工具真的在目录里时才列目标：列了却调不动，
    # 模型会挑一个名字然后撞在边界上，白烧一轮。
    delegation_block = ""
    if any(item.get("tool_name") == DELEGATE_TOOL for item in catalog):
        delegation_block = await _delegation_block(state)

    picked = await _skill_pick(state)
    if picked is not None and not boundary.allows_skill(picked[0].name):
        # 被 Agent 边界排除的技能当作没命中：不注入指令、不记命中。
        # 「命中了但不给用」会让 skill_hits 统计与真实行为对不上。
        logger.info(
            "技能被智能体边界排除。",
            extra={
                "event": "skill_blocked_by_agent",
                "agent": boundary.agent_name,
                "skill": picked[0].name,
            },
        )
        picked = None

    if picked is not None:
        skill, matched_by, keyword = picked
        narrowed = skill_service.narrow_catalog(catalog, skill)
        instruction_chars = len(skill.instruction or "")
        truncated = instruction_chars > skill_service.MAX_INSTRUCTION_CHARS
        # 截断在 render_block 里统一处理：历史数据与导入文件都可能绕过写入路径。
        skill_block = skill_service.render_block(
            skill,
            tools=[item["tool_name"] for item in narrowed],
            truncated=truncated,
        )
        skill_meta = {
            "name": skill.name,
            "matched_by": matched_by,
            "keyword": keyword or None,
            "tools_before": len(catalog),
            "tools_after": len(narrowed),
            "truncated": truncated,
            # 详情页要显示「正文 1,240 / 8,000 字符」，光有 truncated 布尔看不出来用了多少。
            "chars": instruction_chars,
            "limit": skill_service.MAX_INSTRUCTION_CHARS,
        }
        catalog = narrowed

        # 观测上单独成列（skill_name），列表页才能按技能筛；
        # attrs 里的 skill.* 留给详情看命中方式与收窄前后的工具数。
        trace = current_trace()
        if trace is not None:
            trace.skill_name = skill.name
        await _record_skill_hit(state, skill, matched_by, keyword)

    usage: dict[str, Any] = {}
    try:
        plan = await create_action_plan(
            user_message=user_message,
            available_tools=catalog,
            pending_tool_name=state.get("planned_tool_name"),
            pending_slots=state.get("planned_arguments") or {},
            known_user_facts=known_user_facts,
            conversation_summary=conversation_summary,
            recent_turns=recent_turns,
            agent_block=agent_block,
            skill_block=skill_block,
            delegation_block=delegation_block,
            skill_meta=skill_meta,
            usage_sink=usage,
        )
    except ModelNotConfiguredError:
        return _model_failure("model_not_configured")
    except ModelInvocationError as exc:
        return _model_failure(exc.category)

    # 实测 token 交给这一轮之外的 `chat_service` 用：按 run_id 暂存，
    # 图返回后它会取走并落库，下一轮据此决定要不要压缩。
    stash_usage_for_run(state.get("run_id"), usage)

    if plan.action is ActionType.CHAT:
        return _answer(plan.response or "我已收到你的请求。", intent="chat")

    if plan.action is ActionType.CLARIFY:
        # 模型的原始输出要洗过才能进界面与库：条数、长度、重复项都不受控，
        # 规则见 `app/agents/clarify.py`。
        items = normalize_items(
            plan.clarify_items,
            allowed_fields=plan.missing_fields,
        )
        fallback = (
            "请在下面对应的一项里选一个，或直接补充说明。"
            if has_choices(items)
            else "请补充完成该请求所需的信息。"
        )
        return _answer(
            plan.clarification or fallback,
            intent="clarify",
            missing_fields=plan.missing_fields,
            clarify_items=items,
        )

    if plan.action is ActionType.KNOWLEDGE:
        return {
            "intent": "knowledge",
            "planned_arguments": {"query": plan.query or _last_user_text(state)},
            "missing_fields": [],
        }

    if not plan.tool_name:
        return _answer("未能确定可用的处理方式，请补充更具体的请求。", intent="error")

    try:
        tool = gateway.get_tool(plan.tool_name)
    except ToolGatewayError:
        return _answer(
            "该操作不在当前可用的受控工具范围内。",
            intent="error",
            error_category="tool_not_registered",
        )

    # 槽位只在「同一个工具」之间继承，用于多轮补参（第一轮缺 station_id，
    # 用户补充后第二轮沿用上一轮已确认的参数）。
    #
    # 换工具时必须丢弃旧槽位：那是另一个工具的 schema，带过去就是非法参数。
    # 实测 2026-11-04：OCR 那轮的 image_id 被合并进站点查询工具，
    # 网关报「Additional properties are not allowed ('image_id' was unexpected)」，
    # 于是每轮都失败（配合当时的「失败不记账」就成了死循环）。
    previous_tool = state.get("planned_tool_name")
    if previous_tool and previous_tool == tool.name:
        merged = merge_slots(state.get("planned_arguments"), plan.arguments)
    else:
        merged = merge_slots(None, plan.arguments)
    missing = get_missing_required_slots(
        gateway=gateway,
        tool_name=tool.name,
        values=merged,
    )
    for field in plan.missing_fields:
        if field not in missing:
            missing.append(field)

    if missing:
        failed = {
            "intent": "clarify",
            "planned_tool_name": tool.name,
            "planned_arguments": merged,
            "planned_reason": plan.reason,
            "missing_fields": missing,
            "final_response": build_slot_question(missing_fields=missing),
        }
        # 模型可能已经把追问拆好了，只是把 action 标成了 read_tool/write_tool。
        # 这时别丢掉那些选项——它问的和 schema 缺的是同一批字段。
        clarify_items = normalize_items(plan.clarify_items, allowed_fields=missing)
        if clarify_items:
            failed["clarify_items"] = clarify_items
        return failed

    # 同一工具同一参数紧接重放，说明模型在自循环而不是推进任务。
    # 直接用它上一轮的结果收敛，避免无意义的重复调用与费用。
    if _repeats_last_observation(state, tool_name=tool.name, arguments=merged):
        last = (state.get("tool_observations") or [])[-1]
        return _answer(
            str(last.get("text") or "该工具已执行过，本次没有新的结果。"),
            intent="chat",
        )

    # 保守判定：只要工具有副作用，就必须走审批路径，
    # 不接受模型把写工具标记为 read_tool 来绕过审批。
    #
    # 用带上下文的判定而不是 `requires_approval(tool)`：租户把某个工具设成
    # 「需确认」时，它即便是只读工具也要走这一条路；反之平台默认动作改成
    # 拒绝时，这里也不会凭空造出一张「该工具已被拒绝」的审批单。
    intent = (
        "write_tool"
        if gateway.requires_approval_for(tool=tool, context=context)
        else "read_tool"
    )

    # 写操作在生成审批单之前就做授权预检：未授权用户不该看到操作预览，
    # 更不该在库里留下一条永远不会被正确执行的待审批记录。
    #
    # 放在规划节点而不是审批节点：审批节点在 interrupt 恢复时会整段重跑，
    # 若预检放在那里，恢复时一旦授权变化就会挡住本来合法的确认流程。
    if intent == "write_tool":
        try:
            await gateway.check_resource_access(
                context=context,
                tool_name=tool.name,
                arguments=merged,
            )
        except ToolResourceDeniedError:
            return _answer(
                "你当前没有该站点或该操作的权限，本次请求已被拒绝，未创建审批。",
                intent="error",
                error_category="not_authorized",
            )
        except ToolPreflightRejectedError as exc:
            # 工具自己判定这次操作不成立（例如目标是内置智能体）。
            # 原样转达它的理由：这句话就是给人看的，不该再包一层「权限」。
            return _answer(
                f"{exc}未创建审批。",
                intent="error",
                error_category="precheck_rejected",
            )
        except ToolGatewayError as exc:
            return _answer(
                f"无法校验该操作权限：{exc}",
                intent="error",
                error_category="preview_failed",
            )

    return {
        "intent": intent,
        "planned_tool_name": tool.name,
        "planned_arguments": merged,
        "planned_reason": plan.reason,
        "missing_fields": [],
    }


def _model_failure(category: str) -> dict[str, Any]:
    return {
        "intent": "error",
        "error_category": category,
        "final_response": MODEL_ERROR_MESSAGES.get(
            category, MODEL_ERROR_MESSAGES["model_unavailable"]
        ),
    }


def _agent_hidden_tools(gateway: Any, context: ToolExecutionContext) -> list[str]:
    """因为 Agent 边界而没出现在工具目录里的工具名。

    **这类工具不会产生任何「拒绝」记录。** 模型根本看不到它们，只会换个说法
    说「我这边做不了」。所以「边界配窄了」这个配置问题，在日常使用里完全没有
    信号——只能在这里拿「不设边界的目录」和「设了边界的目录」做一次差值。

    基线上下文（`agent_allowed_tools=None`、`agent_can_write=True`）下直接返回空，
    不额外扫一遍目录：没配边界的租户不该为这个功能付任何成本。
    """
    if context.agent_allowed_tools is None and context.agent_can_write:
        return []

    baseline = replace(context, agent_allowed_tools=None, agent_can_write=True)
    before = {view.tool_name for view in gateway.list_available_for_context(context=baseline)}
    after = {view.tool_name for view in gateway.list_available_for_context(context=context)}
    return sorted(before - after)


def _denial_category(exc: ToolPermissionDeniedError) -> str:
    """把「谁拒的」翻译成可聚合的 `error_category`。

    四类拒绝混成一个 `tool_rejected`，运营就只能看到「有一堆拒绝」，
    看不出该改配置（`agent`）还是该改授权（`scope`/`role`/`tenant`）。
    """
    return {
        "agent": "tool_blocked_by_agent",
        "role": "tool_blocked_by_role",
        "tenant": "tool_blocked_by_tenant",
    }.get(exc.reason, "tool_blocked_by_scope")


def _answer(
    text: str,
    *,
    intent: str,
    missing_fields: list[str] | None = None,
    clarify_items: list[dict[str, Any]] | None = None,
    error_category: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "intent": intent,
        "final_response": text,
        "missing_fields": missing_fields or [],
        "error_category": error_category,
    }
    # 只在有时才写：空列表进 state 会让 `begin_turn` 之外的地方也看不出
    # 「这一轮到底有没有追问项」。
    if clarify_items:
        out["clarify_items"] = clarify_items
    return out


def _round_failure(
    state: AgentState,
    text: str,
    *,
    error_category: str,
) -> dict[str, Any]:
    """
    本轮工具没跑成的统一出口。

    必须留下一条 success=False 的 observation，并把 tool_steps +1。
    原因：route_after_read_tool 靠「最后一条 observation 是否成功」决定要不要回到规划器，
    而 tool_observations / tool_steps 在 AgentState 里**没有 reducer**（默认覆盖式合并），
    返回的字典不带这两个键，就等于「这一轮从未发生」。

    两个后果叠加就是死循环：
    - 路由读到的是**上一轮**那条成功记录，判定可以继续 → 回 plan_request
    - 步数一直停在原值，`tool_steps >= max` 永远不成立 → 上限失效

    2026-11-04 实测：带图提问时计划器反复选中同一个只读工具、每轮都在网关前置校验被拒，
    于是 plan ↔ execute_read_tool 空转 45 分钟、521 次模型调用，直到手工重启才停；
    期间 nginx 早在第 180 秒就返回了 504，客户端断开并不会取消后端。

    失败不重试是原设计（失败原因已足够让模型解释），所以这里直接以 error 收尾。
    """
    steps = int(state.get("tool_steps") or 0) + 1
    tool_name = state.get("planned_tool_name")
    observation = {
        "tool_name": tool_name,
        "arguments": dict(state.get("planned_arguments") or {}),
        "success": False,
        "text": text,
        "error_code": error_category,
        "result": None,
        "round": steps,
    }
    logger.warning(
        "只读工具本轮未执行，直接收尾。",
        extra={
            "event": "agent_read_tool_unavailable",
            "tool_name": tool_name,
            "error_category": error_category,
            "tool_steps": steps,
            "detail": text[:200],
        },
    )
    return {
        "intent": "error",
        "final_response": text,
        "missing_fields": [],
        "error_category": error_category,
        "tool_steps": steps,
        "tool_observations": [*list(state.get("tool_observations") or []), observation],
    }


@_tenant_bound
async def execute_read_tool(state: AgentState) -> dict[str, Any]:
    gateway = get_tool_gateway()
    tool_name = state.get("planned_tool_name")

    if not tool_name:
        return _round_failure(
            state,
            "未能确定需要执行的查询。",
            error_category="tool_not_planned",
        )

    try:
        result = await gateway.execute_read_only(
            context=_context(state),
            tool_name=tool_name,
            arguments=state.get("planned_arguments") or {},
        )
    except ToolApprovalRequiredError:
        return _round_failure(
            state,
            "该操作具有外部副作用，必须经审批后在独立路径执行。",
            error_category="approval_required",
        )
    except ToolRateLimitedError:
        return _round_failure(
            state,
            "操作请求过于频繁，请稍后再试。",
            error_category="rate_limited",
        )
    except ToolPermissionDeniedError as exc:
        return _round_failure(
            state,
            str(exc),
            error_category=_denial_category(exc),
        )
    except ToolGatewayError as exc:
        return _round_failure(
            state,
            str(exc),
            error_category="tool_rejected",
        )

    steps = int(state.get("tool_steps") or 0) + 1
    observation = {
        "tool_name": tool_name,
        "arguments": dict(state.get("planned_arguments") or {}),
        "success": result.success,
        "text": _summarize_result(result),
        "error_code": result.error_code,
        "result": result.result,
        # 与 tool_steps 对齐，路由据此确认「这一轮真的落地了」。
        "round": steps,
    }

    observations = list(state.get("tool_observations") or [])
    observations.append(observation)

    updates: dict[str, Any] = {
        "intent": "read_tool",
        "tool_steps": steps,
        "tool_observations": observations,
        "response_metadata": {
            "tool_name": tool_name,
            "success": result.success,
            "verified": result.verified,
            "error_code": result.error_code,
            "result": result.result,
            "tool_steps": steps,
        },
        "error_category": None if result.success else (result.error_code or "tool_failed"),
    }

    # 委派记账。两个计数的口径不同，不能合成一个：
    # - 次数按**尝试**算：防的是模型在对方答不好时反复重试，失败的重试也算花钱；
    # - 路径只在真的进去了才记：环检测问的是「这个 Agent 已经进过没有」。
    if tool_name == DELEGATE_TOOL:
        updates["delegation_count"] = int(state.get("delegation_count") or 0) + 1
        payload = result.result or {}
        entered = str(payload.get("agent") or "") if result.success else ""
        if entered:
            updates["delegation_path"] = [
                *(state.get("delegation_path") or []),
                entered,
            ]

        # 被转交方停在审批上：本图没有中断，但这一轮同样在等人点确认。
        # 抄进 state 是为了让 chat_service 能出与「自己发起写操作」一样的确认卡——
        # 否则用户只看到一句「已转交」，却没有任何可点的东西。
        if result.success and payload.get("status") == "awaiting_approval":
            updates["pending_delegated_approval"] = {
                "approval_id": str(payload.get("approval_id") or ""),
                "preview": payload.get("preview") or {},
                "expires_at": payload.get("expires_at"),
                "agent": payload.get("agent"),
                "display_name": payload.get("display_name"),
                "operation": payload.get("operation"),
                "message": payload.get("summary") or result.message,
            }

    # 不写 final_response：这一轮的结论要交回规划器，
    # 由模型决定「结果够了就作答」还是「继续调用下一个工具」。
    return updates


@_tenant_bound
async def retrieve_knowledge(state: AgentState) -> dict[str, Any]:
    query = str((state.get("planned_arguments") or {}).get("query") or "")

    answer = await answer_from_knowledge(
        query=query,
        scopes=state.get("scopes", []),
        tenant_id=state.get("tenant_id"),
        roles=tuple(state.get("roles", [])),
        user_id=state.get("user_id", ""),
    )

    # 检索已经决定了「哪几段」，这里再决定「怎么讲」。
    # 归纳失败时 answer.answer 仍是原文摘录拼接——与加这一步之前完全一致，
    # 所以知识问答不会因为模型侧的问题变成空答复。
    composed = ComposeResult()
    if answer.documents:
        composed = await compose_answer(query=query, documents=answer.documents)

    text = composed.text or answer.answer
    mode = "summary" if composed.text else "excerpt"

    metadata: dict[str, Any] = {"citation_count": len(answer.citations)}
    if query:
        metadata["knowledge_query"] = query
    if answer.documents:
        # 只给排查用：「这条答复是归纳出来的，还是原样摘的」。
        # 界面上不显示，翻库和看日志时才需要它解释「措辞为什么不一样」。
        metadata["knowledge_answer_mode"] = mode
        if composed.meta.fallback:
            metadata["knowledge_answer_fallback"] = composed.meta.fallback

    return {
        "intent": "knowledge",
        "final_response": text,
        "citations": answer.citations,
        "retrieved_documents": [
            {
                "document_id": document.get("document_id"),
                "title": document.get("title"),
                "chunk_id": document.get("chunk_id"),
            }
            for document in answer.documents
        ],
        # 把真正送进检索的那句话带出来：规划器会做最小改写，
        # 用户看到的却是自己那句原话。两者不一致时（对话命中 A、验证页
        # 命中 B）只能靠这个字段自查，否则只能去翻 checkpoint。
        "response_metadata": metadata,
        "error_category": None if answer.found else "knowledge_not_found",
    }


def _approval_message(headline: str, summary: str) -> str:
    """确认卡正文：先说这次要做什么，再说「还没做」。

    顺序不能反：摘要（几步、每步谁做、按什么分流）排在「尚未执行」之后，
    读到那句的人已经准备点确认了。这一段以前没有，于是「新建流程」的卡上
    只有一行工具名与一份折叠的 JSON，用户批的其实是他没看过的东西。
    """
    parts = [headline]
    if summary:
        parts.append(summary)
    parts.append("该操作尚未执行。请确认或取消本次操作。")
    return "\n\n".join(parts)


@_tenant_bound
async def create_and_wait_approval(state: AgentState) -> dict[str, Any]:
    gateway = get_tool_gateway()
    approval_service = get_approval_service()
    context = _context(state)

    tool_name = state.get("planned_tool_name")
    arguments = state.get("planned_arguments") or {}

    if not tool_name:
        return _answer("无法确定待审批的操作。", intent="error")

    try:
        plan = await gateway.prepare_call(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            reason=state.get("planned_reason") or "用户请求执行写操作。",
        )
    except ToolPermissionDeniedError as exc:
        return _answer(
            f"无法生成操作预览：{exc}",
            intent="error",
            error_category=_denial_category(exc),
        )
    except ToolGatewayError as exc:
        return _answer(
            f"无法生成操作预览：{exc}",
            intent="error",
            error_category="preview_failed",
        )

    # 授权预检已在 plan_request 完成：此处不再重复校验，
    # 因为本节点在 interrupt 恢复时会重跑，重复校验会让授权正常变更
    # 阻断合法的确认流程。执行期仍由 Gateway 兜底校验。

    if not plan.requires_approval:
        return _answer(
            "该操作不满足写操作审批条件，不会在此路径执行。",
            intent="error",
            error_category="approval_path_mismatch",
        )

    preview = {
        "operation": plan.display_name,
        "tool_name": plan.tool_name,
        "parameters": plan.arguments,
        "risk_level": plan.risk_level.value,
        "impact": "该操作会修改外部系统的数据或状态。",
        "notice": "尚未执行，确认后才会提交。",
        "created_at": datetime.now(UTC).isoformat(),
    }

    # 摘要必须同时进正文与 preview：正文是用户真正读的那段话，preview 给界面
    # 与控制台留一份结构化副本（翻审批记录时不必去解析 parameters）。
    if plan.summary:
        preview["summary"] = plan.summary

    # 结构图只进 preview，不进正文：正文是文字，图由界面按这份数据画。
    # 放 preview 而不是随响应一次性给出，是为了刷新后还在——卡片是从审批单
    # 重新取的，只放在响应里，刷新一次图上就只剩一行「步骤：a → b」。
    if plan.graph:
        preview["graph"] = plan.graph

    # 委派链：空表示这张单子由用户直接对话的那个 Agent 开出。
    #
    # 冻结整条链而不是只冻结直接上级：复核要问「当初那一跳现在还应不应该发生」，
    # 而 A 收窄时 A→B→C 这条上 C 看起来完全合法——只查一级会漏掉这一类。
    delegation_chain = [
        {"id": agent_id or None, "name": name}
        for agent_id, name in context.delegation_ancestors
    ]
    delegated_from = context.delegation_ancestors[-1][1] if delegation_chain else ""

    if delegated_from:
        preview["delegation"] = {
            "from": delegated_from,
            "depth": len(delegation_chain),
        }

    approval = await approval_service.create_approval(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        plan=plan,
        preview=preview,
        # interrupt 恢复时节点会重跑，必须复用同一条审批记录。
        idempotency_key=f"approval:{context.thread_id}:{context.run_id}",
        # 边界随审批一起冻结：执行时与当时的配置取交集。
        agent_id=context.agent_id,
        agent_name=context.agent_name or None,
        agent_revision=context.agent_revision or None,
        agent_allowed_tools=(
            None
            if context.agent_allowed_tools is None
            else list(context.agent_allowed_tools)
        ),
        agent_can_write=context.agent_can_write,
        delegation_chain=delegation_chain,
        # 子调用的结论要回到发起方所在的会话，而不是这条一次性的子线程。
        parent_thread_id=context.delegation_origin_thread or None,
        # 记下所属运行。审批恢复是新 trace，没有这一列就只能从自己重新起一次，
        # 运行视图里「发起」与「执行」会断成两条。观测关闭时这里是 None。
        operation_id=current_operation_id(),
    )

    preview["expires_at"] = approval.expires_at.isoformat()
    preview["approval_id"] = str(approval.id)

    # 审批人要能判断「这是不是一条越权链的末端」：只说「ops 要改工单」，
    # 他不知道这张单子是 cs 转过来的。所以委派来的单子必须点名上游。
    if delegated_from:
        headline = (
            f"「{context.agent_name}」在「{delegated_from}」的委派下，"
            f"要执行：{plan.display_name}"
        )
    else:
        headline = f"待确认操作：{plan.display_name}"

    # 图在此中断并持久化；恢复时 Command(resume=...) 的返回值进入 decision。
    decision = interrupt(
        {
            "kind": "approval",
            "approval_id": str(approval.id),
            "thread_id": context.thread_id,
            "operation": plan.display_name,
            "preview": preview,
            "expires_at": approval.expires_at.isoformat(),
            "message": _approval_message(headline, plan.summary),
        }
    )

    if not isinstance(decision, dict):
        return _answer("未收到有效的确认结果，本次不会执行。", intent="error")

    if decision.get("action") == "cancel":
        await approval_service.reject(
            approval_id=approval.id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
        return {
            "intent": "write_tool",
            "approval_id": str(approval.id),
            "execution_status": "rejected",
            "final_response": "已取消本次操作，未执行任何写入。",
            # 取消不是失败，但**原因必须留下来**：`persist_state` 用状态里的
            # `error_category` 覆盖运行的同一列，这里不给就会把审批侧写好的
            # `approval_rejected` 抹成空——两条取消路径（图内取消 / 图不可恢复
            # 时的兜底 reject）于是给出两种记账，谁都说不清哪条算数。
            "error_category": "approval_rejected",
            "response_metadata": {"approval_id": str(approval.id)},
        }

    if decision.get("action") != "confirm":
        return _answer("确认结果无效，本次不会执行。", intent="error")

    try:
        result = await approval_service.confirm_and_execute(
            approval_id=approval.id,
            context=context,
            confirmed_parameters=arguments,
        )
    except ApprovalExpiredError as exc:
        # 过期不是「执行失败」：这一次写入从来没有发生，而且原因不在工具。
        # 记成 FAILED 会和真正的执行失败混在一个桶里。
        return _approval_failure(
            approval.id,
            str(exc),
            "approval_expired",
            execution_status="EXPIRED",
        )
    except ApprovalParameterMismatchError as exc:
        return _approval_failure(approval.id, str(exc), "approval_parameter_mismatch")
    except ApprovalNotFoundError as exc:
        return _approval_failure(approval.id, str(exc), "approval_not_found")
    except ApprovalStateError as exc:
        return _approval_failure(approval.id, str(exc), "approval_state_invalid")
    except ApprovalError as exc:
        return _approval_failure(approval.id, str(exc), "approval_error")

    tool_result = result.tool_result

    return {
        "intent": "write_tool",
        "approval_id": str(approval.id),
        "execution_status": result.status.value,
        "final_response": result.message,
        "response_metadata": {
            "approval_id": str(approval.id),
            "status": result.status.value,
            "success": result.success,
            "verified": result.verified,
            "already_processed": result.already_processed,
            "result": (tool_result.result if tool_result else None),
            "verification": (tool_result.verification if tool_result else None),
            # 结果气泡里的结构图。与确认卡上那张是同一份数据，但走的是另一条
            # 路径：卡从审批单取（刷新也在），气泡从这一轮的 metadata 取。
            # 只放响应不给落库，刷新一次图就没了，只剩「步骤：a → b」那行字。
            **_result_graph(tool_result),
        },
        "error_category": None if result.success else "write_failed",
    }


def _approval_failure(
    approval_id: uuid.UUID,
    message: str,
    category: str,
    *,
    execution_status: str = "failed",
) -> dict[str, Any]:
    return {
        "intent": "write_tool",
        "approval_id": str(approval_id),
        "execution_status": execution_status,
        "final_response": message,
        "error_category": category,
        "response_metadata": {"approval_id": str(approval_id)},
    }


async def respond(state: AgentState) -> dict[str, Any]:
    text = state.get("final_response")

    if not text:
        # 回路跑满步数仍未生成答复时，用最后一次工具结果兜底，
        # 而不是给用户一句「无法生成有效回复」把结果吞掉。
        observations = state.get("tool_observations") or []
        if observations:
            text = str(observations[-1].get("text") or "").strip() or None

    text = text or "当前无法生成有效回复。"
    update: dict[str, Any] = {
        "messages": [AIMessage(content=text)],
        "final_response": text,
    }

    # 追问卡片随答复一起出去：正文那几句话在气泡里，选项在卡片里，
    # 两者靠同一条 metadata 配对。合并而不是覆盖——这一轮可能先查过一次再反问，
    # 那条 tool 元信息不能被吞掉。
    items = normalize_items(state.get("clarify_items"))
    if items and state.get("intent") == "clarify":
        merged = dict(state.get("response_metadata") or {})
        merged["clarify"] = {"items": items}
        update["response_metadata"] = merged

    return update


# 写操作落终态时，「这一轮算完成还是失败」由审批的结论决定。
#
# 原先只判断 `execution_status == "PENDING"`，其余一律 COMPLETED——于是
# 「工具执行失败」与「用户点了取消」都被记成完成。运行记录与审批单互相打脸，
# 而且这种错法是**朝好看的方向错**：失败率与 p95 都不会报警。
_WRITE_FAILED_STATUSES = {"failed", "FAILED", "EXECUTION_UNKNOWN", "RECOVERY_REQUIRED"}
_WRITE_CANCELLED_STATUSES = {"rejected", "REJECTED", "EXPIRED"}


def run_status_for(state: AgentState) -> RunStatus:
    """把一次运行的终态从 state 推导出来。拆成纯函数是为了能直接断言。"""
    intent = state.get("intent")

    if intent == "error":
        return RunStatus.FAILED

    if intent != "write_tool":
        return RunStatus.COMPLETED

    execution = str(state.get("execution_status") or "")

    if execution == "PENDING":
        return RunStatus.WAITING_FOR_APPROVAL
    if execution in _WRITE_FAILED_STATUSES:
        return RunStatus.FAILED
    if execution in _WRITE_CANCELLED_STATUSES:
        return RunStatus.CANCELLED

    return RunStatus.COMPLETED


@_tenant_bound
async def persist_state(state: AgentState) -> dict[str, Any]:
    intent = state.get("intent")
    error_category = state.get("error_category")

    status = run_status_for(state)

    run_id = state.get("run_id")

    if run_id:
        await finish_run(
            run_id=run_id,
            status=status,
            intent=intent,
            tool_name=state.get("planned_tool_name"),
            approval_id=state.get("approval_id"),
            error_category=error_category,
            output_summary=state.get("final_response"),
        )

    return {}


def route_after_plan(
    state: AgentState,
) -> Literal["respond", "execute_read_tool", "retrieve_knowledge", "create_and_wait_approval"]:
    intent = state.get("intent")

    if intent == "read_tool":
        return "execute_read_tool"
    if intent == "knowledge":
        return "retrieve_knowledge"
    if intent == "write_tool":
        return "create_and_wait_approval"

    return "respond"


def route_after_read_tool(state: AgentState) -> Literal["plan_request", "respond"]:
    """只读工具执行完的走向。

    成功 → 回到规划器，让模型看着结果决定下一步（这就是「模型自主决定」）。
    失败 → 直接作答，不重试第二遍：失败原因已足够让模型解释，重试只会放大故障。
    步数用尽 → 强制收敛，防止模型在工具之间无限自循环。
    """
    observations = state.get("tool_observations") or []
    if not observations:
        return "respond"

    last = observations[-1]
    steps = int(state.get("tool_steps") or 0)

    # 防御：确认这一轮的 observation 真的写进来了。
    # observation.round 由 execute_read_tool 按「本轮步数」写入，与 tool_steps 天然对齐；
    # 对不上说明这一轮没有记账（例如将来新增的分支漏了 _round_failure），
    # 此时回规划器必然空转，宁可收尾。
    #
    # 这条判断是 2026-11-04 那次死循环的直接补丁：当时失败分支只返回了
    # final_response，两个键都没写，于是路由一直看到上一轮的成功结果。
    if int(last.get("round") or 0) != steps:
        logger.warning(
            "本轮工具结果未落地，跳过回路直接收尾。",
            extra={
                "event": "agent_tool_round_missing",
                "tool_steps": steps,
                "observed_round": last.get("round"),
            },
        )
        return "respond"

    if not last.get("success"):
        return "respond"

    if steps >= settings.agent_max_tool_steps:
        logger.info(
            "工具步数达到上限，强制收敛作答。",
            extra={
                "event": "agent_tool_steps_exhausted",
                "tool_steps": steps,
                "max_steps": settings.agent_max_tool_steps,
            },
        )
        return "respond"

    return "plan_request"
