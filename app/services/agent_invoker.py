# app/services/agent_invoker.py
"""一次「把任务交给另一个智能体」的完整执行：纪律检查 → 目标解析 → 边界比对 →
引用解析 → 跑子图 → 结果解释 → 审计。

**为什么单独一个模块。** 这个动作有两个入口：模型调用 `delegate_to_agent`
（`app/tools/builtins/delegate_tools.py`），以及编排节点（`app/orchestration/`）。
两边各写一份的下场是：编排绕过 `agent_subset` 而所有单测都还是绿的——所以
`invoke()` 是唯一实现，`delegate_to_agent` 只是它的薄壳（解析参数、转结果形状）。

三件事一处不改地沿用 `docs/A2A.md`：

1. **单调收窄**：被委派方的边界必须是委派方的子集，判定只有 `agent_service.agent_subset`；
2. **不同步继承上下文**：只传 `task` / `brief` / 显式 `refs`，不传原始消息与附件；
3. **写操作仍过人的手**：审批单由被调方开，本图不跟着中断。

编排多出来的只有两个开关（见 `docs/ORCHESTRATION.md` §4 D3）：

- `counts_against_budget=False` —— 次数预算是给「模型即兴再转一次」踩刹车的，
  一条人写死的流程不该被它卡死；深度不在此列，仍然共用；
- `allow_revisit=True` —— 图里同一个 Agent 出现两次（做两件事）是正常的，
  终止性由 DAG + 节点数上限保证，不靠环检测。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from app.db.session import async_session_factory
from app.services import agent_service, reference_service
from app.services.agent_service import AgentBoundary
from app.services.audit_service import write_audit_event
from app.services.delegation_limits import DelegationLimits
from app.tenancy.context import tenant_scope
from app.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# 结果的三种形态。`awaiting_approval` 算「交出去了但停在别人手里」，
# 与「办完了」必须分开：混在一起会把一张没被点过的确认卡说成既成事实。
STATUS_COMPLETED = "completed"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_FAILED = "failed"

# 没有指定 Agent 的对话（平台基线）也要在委派链上留一个可读的名字。
# 它在复核时会被跳过（没有配置行就没有能收窄的东西），但审批卡上那句
# 「在 X 的委派下」不能是空的——空一句会让人以为是数据丢了。
_BASELINE_NAME = "平台默认智能体"


class InvocationErrorCode:
    """错误码集中在这里。

    它们是**对外约定**：前端按码分支、审计按码统计、文档按码解释。
    散在各处的字符串字面量迟早会出现 `DELEGATE_WIDENS` 与 `DELEGATE_WIDEN`
    同时在库里的情况，而那种错查起来毫无线索。
    """

    ARGUMENT_MISSING = "DELEGATE_ARGUMENT_MISSING"
    DEPTH_EXCEEDED = "DELEGATE_DEPTH_EXCEEDED"
    CYCLE = "DELEGATE_CYCLE"
    BUDGET_EXHAUSTED = "DELEGATE_BUDGET_EXHAUSTED"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    WIDENS = "DELEGATE_WIDENS"
    REF_INVALID = "DELEGATE_REF_INVALID"
    FAILED = "DELEGATE_FAILED"
    NO_EVIDENCE = "DELEGATE_NO_EVIDENCE"


@dataclass(frozen=True, slots=True)
class Invocation:
    """一次子调用的结论。

    `child_context` 是「被调方此刻的上下文」：编排拿它当下一节点的父上下文，
    于是逐跳收窄与深度累积都是自然发生的，不需要另一套链路推算。
    """

    status: str
    target: str
    display_name: str = ""
    output: str = ""
    message: str = ""
    error_code: str | None = None

    # 边界比对失败时的具体违反项（`allowed_tools:x` 这类），供界面精确展示。
    violation: str = ""

    # 额外的结构化细节（引用的 `as_detail()` 等）。原样并进工具结果，
    # 少了它前端从「引用不可用」里就看不出是哪一个不可用。
    extra: dict[str, Any] = field(default_factory=dict)

    # 子图停在审批上时的原始载荷（approval_id / preview / expires_at / operation）。
    approval: dict[str, Any] | None = None

    evidence: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    depth: int = 0
    readonly: bool = True
    child_context: ToolExecutionContext | None = None

    @property
    def succeeded(self) -> bool:
        """「这次调用算不算交出去了」。失败才算没交出去。"""
        return self.status != STATUS_FAILED


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]


def parent_boundary(context: ToolExecutionContext) -> AgentBoundary:
    """发起方边界。只用于子集判定，不进入子调用。"""
    return AgentBoundary(
        agent_id=context.agent_id,
        agent_name=context.agent_name,
        revision=context.agent_revision,
        allowed_tools=context.agent_allowed_tools,
        allowed_skills=context.agent_allowed_skills,
        allowed_connectors=context.agent_allowed_connectors,
        can_write=context.agent_can_write,
        max_delegation_depth=context.agent_max_delegation_depth,
        delegation_budget=context.agent_delegation_budget,
    )


def effective_limits(context: ToolExecutionContext) -> DelegationLimits:
    """本跳生效的上限 = 上游折下来的 × 这一环的配置，取更严的。"""
    return context.delegation_limits.narrowed_by(
        max_depth=context.agent_max_delegation_depth,
        budget=context.agent_delegation_budget,
    )


def child_context_of(
    context: ToolExecutionContext,
    *,
    boundary: AgentBoundary,
    limits: DelegationLimits,
    counts_against_budget: bool,
) -> ToolExecutionContext:
    """被调方此刻的上下文。

    这是 `_child_state` 的 context 版：编排要沿着链继续往下走，就必须能拿到
    「B 走完之后，C 眼里的世界是什么」。两者必须同源地算，否则子图里看到的
    深度与审计里记的深度会不一致——那种不一致只会在排障时暴露。

    `delegation_count` 只在计数口径里 +1：编排节点不占模型即兴的额度，
    但它确实发生了一次转交，所以在别人的账上它仍然是一次。
    """
    hop = len(context.delegation_path) + 1

    return ToolExecutionContext(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        request_id=context.request_id,
        roles=context.roles,
        scopes=context.scopes,
        is_platform_admin=context.is_platform_admin,
        conversation_id=context.conversation_id,
        approval_id=None,
        agent_id=boundary.agent_id,
        agent_name=boundary.agent_name or boundary.display_name,
        agent_revision=boundary.revision,
        agent_allowed_tools=boundary.allowed_tools,
        agent_allowed_skills=boundary.allowed_skills,
        agent_allowed_connectors=boundary.allowed_connectors,
        agent_can_write=boundary.can_write,
        agent_max_delegation_depth=boundary.max_delegation_depth,
        agent_delegation_budget=boundary.delegation_budget,
        delegation_limits=limits,
        delegation_path=(*context.delegation_path, boundary.agent_name),
        delegation_count=(
            context.delegation_count + 1 if counts_against_budget else context.delegation_count
        ),
        delegation_ancestors=(
            *context.delegation_ancestors,
            (context.agent_id or "", context.agent_name or _BASELINE_NAME),
        ),
        delegation_origin_thread=(
            context.delegation_origin_thread or context.thread_id
        ),
        # 子图自己的审批只能由它自己等：本图不会跟着中断。
        attachment_ids=context.attachment_ids,
    )


def context_from_state(state: dict[str, Any]) -> ToolExecutionContext:
    """从执行图的 state 还原工具上下文。

    **唯一实现。** 编排要在对话里触发（它拿到的也是一个 state），如果它自己再
    拼一份，深度/委派链/边界的默认值迟早与节点里那份对不上——而那种不一致
    只会在「同一条链上两种判定」的时候暴露。
    """
    raw_limits = state.get("delegation_limits")
    limits = (
        # 子调用：上限已经从父链折下来，这里只还原。
        DelegationLimits.from_state(raw_limits)
        if raw_limits
        # 根节点：从本 Agent 的配置出发，而不是平台默认。否则
        # 「最大转交深度 = 5」这个配置永远不起作用（根是 2，之后只取 min）。
        else DelegationLimits.for_root(
            max_depth=state.get("agent_max_delegation_depth"),
            budget=state.get("agent_delegation_budget"),
        )
    )

    return ToolExecutionContext(
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        thread_id=state["thread_id"],
        run_id=state["run_id"],
        request_id=state.get("request_id", ""),
        roles=tuple(state.get("roles", [])),
        scopes=tuple(state.get("scopes", [])),
        is_platform_admin=bool(state.get("is_platform_admin", False)),
        conversation_id=state.get("conversation_id", ""),
        # 智能体边界随上下文进入网关：目录过滤与执行拦截共用同一份判断，
        # 避免「目录里没有、调用却能过」的缝。
        agent_id=state.get("agent_id"),
        agent_name=state.get("agent_name") or "",
        agent_revision=int(state.get("agent_revision") or 0),
        agent_allowed_tools=(
            None
            if state.get("agent_allowed_tools") is None
            else tuple(state.get("agent_allowed_tools") or ())
        ),
        agent_allowed_skills=(
            None
            if state.get("agent_allowed_skills") is None
            else tuple(state.get("agent_allowed_skills") or ())
        ),
        agent_allowed_connectors=(
            None
            if state.get("agent_allowed_connectors") is None
            else tuple(state.get("agent_allowed_connectors") or ())
        ),
        agent_can_write=bool(state.get("agent_can_write", True)),
        agent_max_delegation_depth=state.get("agent_max_delegation_depth"),
        agent_delegation_budget=state.get("agent_delegation_budget"),
        delegation_limits=limits,
        delegation_path=tuple(state.get("delegation_path") or ()),
        delegation_count=int(state.get("delegation_count") or 0),
        delegation_ancestors=tuple(
            (str(item.get("id") or ""), str(item.get("name") or ""))
            for item in (state.get("delegation_ancestors") or [])
            if isinstance(item, dict)
        ),
        delegation_origin_thread=str(state.get("delegation_origin_thread") or ""),
        attachment_ids=tuple(
            str(item.get("attachment_id"))
            for item in (state.get("attachments") or [])
            if isinstance(item, dict) and item.get("attachment_id")
        ),
    )


def _child_state(
    context: ToolExecutionContext,
    *,
    boundary: AgentBoundary,
    task: str,
    brief: str,
    thread_id: str,
    chain: list[str],
    limits: DelegationLimits,
    materials: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """子调用的初始状态。

    三处刻意：
    - `user_id` / `scopes` / `roles` 继承发起人：换成 B 的服务身份就要给 B 发凭据，
      那是「万能 Token」的入口；而 `agent_*` 用 B 自己的，否则根本不是委派。
    - `run_id` 留空：子调用不写 `agent_runs`。它没有独立的用户请求，
      写一条会污染「这个会话跑了几轮」的口径。
    - `attachments` 默认不带：附件句柄是会话级的，不自动过桥（`A2A.md` §7）。
    """
    user_text = task if not brief else f"{task}\n\n背景：{brief}"
    if materials:
        user_text = f"{user_text}\n\n{materials}"

    return {
        "messages": [HumanMessage(content=user_text)],
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "thread_id": thread_id,
        "conversation_id": context.conversation_id,
        "run_id": "",
        "request_id": context.request_id,
        "roles": list(context.roles),
        "scopes": list(context.scopes),
        "is_platform_admin": context.is_platform_admin,
        "attachments": list(attachments or []),
        "agent_id": boundary.agent_id,
        "agent_name": boundary.agent_name,
        "agent_display_name": boundary.display_name,
        "agent_instruction": boundary.instruction,
        "agent_revision": boundary.revision,
        "agent_allowed_tools": (
            None if boundary.allowed_tools is None else list(boundary.allowed_tools)
        ),
        "agent_allowed_skills": (
            None if boundary.allowed_skills is None else list(boundary.allowed_skills)
        ),
        "agent_allowed_connectors": (
            None
            if boundary.allowed_connectors is None
            else list(boundary.allowed_connectors)
        ),
        # 解除只读（P2）的前提是子集校验已经通过：B 可写要求 A 可写，
        # 所以这里的取值不会让「只读的 A」通过委派绕成可写。
        "agent_can_write": boundary.can_write,
        "agent_max_delegation_depth": boundary.max_delegation_depth,
        "agent_delegation_budget": boundary.delegation_budget,
        "delegation_path": [*chain, boundary.agent_name],
        "delegation_count": context.delegation_count,
        "delegation_limits": limits.as_state(),
        "delegation_ancestors": [
            *({"id": agent_id or None, "name": name}
               for agent_id, name in context.delegation_ancestors),
            {
                "id": context.agent_id,
                "name": context.agent_name or _BASELINE_NAME,
            },
        ],
        "delegation_origin_thread": (
            context.delegation_origin_thread or context.thread_id
        ),
        "pending_delegated_approval": None,
    }


def awaiting_approval_result(
    pending: dict[str, Any],
    *,
    target: str,
    display_name: str,
    depth: int,
) -> dict[str, Any]:
    """子图停在审批上时，给发起方的结果形状。

    `status` 用 `awaiting_approval` 而不是 completed：**什么都没执行**，
    转述成完成会把一张还没被点过的确认卡说成既成事实。

    这个结果会被 `execute_read_tool` 抄进 state，由 `chat_service` 变成
    与「自己发起写操作」完全相同的确认卡。
    """
    approval_id = str(pending.get("approval_id") or "")
    preview = pending.get("preview") or {}
    operation = str(
        pending.get("operation") or preview.get("operation") or preview.get("tool_name") or ""
    )

    message = (
        f"「{display_name or target}」需要你确认一项写操作"
        + (f"：{operation}" if operation else "")
        + "。确认后它才会执行；现在还没有写入任何内容。"
    )

    return {
        "agent": target,
        "display_name": display_name,
        "status": STATUS_AWAITING_APPROVAL,
        "approval_id": approval_id,
        "operation": operation,
        "preview": preview,
        "expires_at": pending.get("expires_at"),
        "readonly": False,
        "depth": depth,
        "summary": message,
    }


def evidence_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    """把子调用的依据摘成引用。

    只留 id / 标题 / 小节 / 来源，不搬正文：A 拿到的应该是「凭什么」，
    而不是一份可以再分发的内容副本。
    """
    evidence: list[dict[str, Any]] = []

    for citation in state.get("citations") or []:
        if not isinstance(citation, dict):
            continue
        evidence.append(
            {
                "kind": "knowledge",
                "id": citation.get("document_id"),
                "title": citation.get("title"),
                "section": citation.get("section"),
                "updated_at": citation.get("updated_at"),
            }
        )

    for observation in state.get("tool_observations") or []:
        if not isinstance(observation, dict) or not observation.get("tool_name"):
            continue
        evidence.append(
            {
                "kind": "tool",
                "id": observation.get("tool_name"),
                "success": bool(observation.get("success")),
            }
        )

    return evidence


async def _audit(
    context: ToolExecutionContext,
    *,
    target: str,
    display_name: str,
    success: bool,
    status: str,
    depth: int,
    task: str,
    reason: str | None = None,
    agent_id: str | None = None,
    refs: list[dict[str, str]] | None = None,
    approval_id: str | None = None,
    limits: dict[str, int] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """委派记账。

    为什么一定要有：trace 有保留期（`obs_retention_days`），审计是长期账。
    「这个智能体被谁叫去干过活」必须在 trace 过期后仍然能回答。

    `resource_id` 用**名字**而不是 UUID：名字创建后不可改、租户内唯一，
    所以它同样能反查；而深度/环/预算这些拒绝发生在查库之前，那时手上只有名字。

    `task` 只记哈希与长度，不记原文：任务描述里常常带着业务细节
    （哪个场站、哪个客户），而审计表是长期保留的。
    """
    try:
        with tenant_scope(context.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    await write_audit_event(
                        session,
                        event_type="agent_delegated",
                        tenant_id=context.tenant_id,
                        actor_id=context.user_id,
                        resource_type="agent",
                        resource_id=target,
                        thread_id=context.thread_id,
                        run_id=context.run_id,
                        success=success,
                        message=(
                            f"{context.agent_name or '发起方'} 把任务转交给了「{display_name or target}」"
                            + (f"，未执行：{reason}" if reason else "")
                        ),
                        metadata={
                            "via": "delegate_to_agent",
                            "from_agent": context.agent_name,
                            "from_revision": context.agent_revision,
                            "to_agent": target,
                            "to_agent_id": agent_id,
                            "depth": depth,
                            "status": status,
                            "reject_reason": reason,
                            "task_chars": len(task),
                            "task_sha256": _task_hash(task),
                            "refs": refs or [],
                            "approval_id": approval_id,
                            "limits": limits,
                            # 编排节点在这里补 `workflow_run_id` / `node_id`。
                            # 不另开一张审计表：一次节点调用就是一次转交，
                            # 分成两张表之后「某条边是谁发起的」要连表才能答。
                            **(extra or {}),
                        },
                    )
    except Exception:
        # 审计失败不改写业务结论：委派是否发生，取决于子调用有没有跑，
        # 而不是取决于这一行有没有写进去。
        logger.exception(
            "委派审计写入失败。",
            extra={"event": "delegate_audit_failed", "to_agent": target},
        )


async def _discard_child_thread(thread_id: str) -> None:
    """丢掉子调用的检查点（实现在 `app.db.checkpoint`，恢复路径也要用）。"""
    from app.db.checkpoint import discard_thread

    await discard_thread(thread_id)


def _failed(
    code: str,
    message: str,
    *,
    target: str,
    depth: int = 0,
    violation: str = "",
    display_name: str = "",
    extra: dict[str, Any] | None = None,
) -> Invocation:
    return Invocation(
        status=STATUS_FAILED,
        target=target,
        display_name=display_name,
        message=message,
        error_code=code,
        violation=violation,
        depth=depth,
        extra=dict(extra or {}),
    )


async def invoke(
    context: ToolExecutionContext,
    *,
    target: str,
    task: str,
    brief: str = "",
    refs: Any = None,
    counts_against_budget: bool = True,
    allow_revisit: bool = False,
    span_attrs: dict[str, Any] | None = None,
    audit_extra: dict[str, Any] | None = None,
) -> Invocation:
    """把 `task` 交给 `target` 执行一次，拿回它的结论。

    参数含义与 `delegate_to_agent` 的工具入参一一对应，唯一的不同是它不认识
    `ToolExecutionResult`——编排不需要一个假的工具调用栈就能用它，
    而 `delegate_to_agent` 负责把结论翻译成工具结果。
    """
    target = str(target or "").strip()
    task = str(task or "").strip()
    brief = str(brief or "").strip()

    if not target or not task:
        return _failed(
            InvocationErrorCode.ARGUMENT_MISSING,
            "转交需要同时给出目标智能体与任务描述。",
            target=target,
        )

    # ---------------- 纪律检查（全部在跑子图之前） ----------------

    # 链条包含发起方：只记「被转交过谁」的话，A→B→A 里那个 A 永远不在链上，
    # 环检测会漏掉最典型的一种环。
    chain = list(context.delegation_path) or [context.agent_name]
    hop = len(chain)

    limits = effective_limits(context)

    if hop > limits.max_depth:
        await _audit(
            context,
            target=target,
            display_name=target,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason="depth",
            limits=limits.as_state(),
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.DEPTH_EXCEEDED,
            limits.depth_refusal(),
            target=target,
            depth=hop,
        )

    # `allow_revisit` 只关掉「同一 Agent 不能出现两次」这一条。
    # 编排的终止性由 DAG + 节点数上限保证，游标只前进；而「转交给自己」
    # 在编排里也是合法的（图的第一节点常常就是发起方本人）。
    if not allow_revisit and (target == context.agent_name or target in chain):
        await _audit(
            context,
            target=target,
            display_name=target,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason="cycle",
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.CYCLE,
            f"不能再转交给「{target}」：它已经在这条转交链上了。",
            target=target,
            depth=hop,
        )

    # 次数预算：只对「模型即兴再转一次」生效。编排节点不占这份额度——
    # 一条人写死的 5 节点流程被默认的 3 次卡死在第四步，是把两种风险混成一件事。
    if counts_against_budget and context.delegation_count >= limits.max_per_turn:
        await _audit(
            context,
            target=target,
            display_name=target,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason="budget",
            limits=limits.as_state(),
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.BUDGET_EXHAUSTED,
            limits.budget_refusal(),
            target=target,
            depth=hop,
        )

    # ---------------- 目标解析 ----------------

    try:
        with tenant_scope(context.tenant_id):
            async with async_session_factory() as session:
                row = await agent_service.get_agent(session, ref=target)
                boundary = agent_service.boundary_of(row)
                display_name = row.display_name
                unavailable = (
                    "未启用" if row.status.value != "active" else
                    ("默认智能体" if row.is_default else ("内置智能体" if row.is_builtin else ""))
                )
    except agent_service.AgentNotFound:
        return _failed(
            InvocationErrorCode.AGENT_UNAVAILABLE,
            f"当前租户下没有名为「{target}」的智能体。",
            target=target,
            depth=hop,
        )

    if unavailable:
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason=unavailable,
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.AGENT_UNAVAILABLE,
            f"「{display_name or target}」不能承接转交任务（{unavailable}）。",
            target=target,
            depth=hop,
        )

    ok, violation = agent_service.agent_subset(boundary, parent_boundary(context))
    if not ok:
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason=violation,
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.WIDENS,
            f"不能转交给「{display_name or target}」："
            f"{agent_service.describe_violation(violation)}",
            target=target,
            depth=hop,
            violation=violation,
            display_name=display_name,
            extra={"violation": violation},
        )

    # ---------------- 引用解析（仍在跑子图之前） ----------------

    # 顺序是刻意的：纪律 → 目标 → 边界 → 引用。前三条都是「这次转交根本不该发生」，
    # 引用是「转交成立，但材料给不过去」。
    try:
        specs = reference_service.parse_refs(refs)
    except reference_service.ReferenceError as exc:
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason=f"ref:{exc.code}",
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.REF_INVALID,
            str(exc),
            target=target,
            depth=hop,
            display_name=display_name,
            extra=exc.as_detail(),
        )

    try:
        materials = await reference_service.resolve_refs(
            specs,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            scopes=context.scopes,
            roles=context.roles,
        )
    except reference_service.ReferenceError as exc:
        # 全有或全无：一个引用不过就整单拒绝，不静默丢掉那一个。
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="rejected",
            depth=hop,
            task=task,
            reason=f"ref:{exc.code}",
            refs=reference_service.summary_of_specs(specs),
            extra=audit_extra,
        )
        return _failed(
            exc.code,
            str(exc),
            target=target,
            depth=hop,
            display_name=display_name,
            extra=exc.as_detail(),
        )

    refs_summary = reference_service.audit_summary(materials)

    # ---------------- 跑子图 ----------------

    # 子调用必须是独立的 thread：用父 thread 会让两轮消息与单轮字段混进同一份
    # checkpoint，父级 `begin_turn` 清掉的正是子图写下的那些。
    child_thread = f"{context.thread_id}:d{hop}:{target}:{uuid.uuid4().hex[:6]}"

    from app.agents.graph import get_agent_graph
    from app.agents.interrupts import extract_interrupt
    from app.observability.context import bind_agent, span

    # `bind_agent` 套在 span **外面**：`agent.{target}` 这个 span 自己也要归
    # 被委派方，否则拓扑图里这条边会指回发起方。
    try:
        with bind_agent(target):
            async with span(
                f"agent.{target}",
                "agent",
                attrs={
                    "agent": target,
                    "revision": boundary.revision,
                    "depth": hop,
                    "delegated_from": context.agent_name or None,
                    **(span_attrs or {}),
                },
            ) as handle:
                state = await get_agent_graph().ainvoke(
                    _child_state(
                        context,
                        boundary=boundary,
                        task=task,
                        brief=brief,
                        thread_id=child_thread,
                        chain=chain,
                        limits=limits,
                        materials=reference_service.render_materials(materials),
                        attachments=[
                            ref.attachment for ref in materials if ref.attachment is not None
                        ],
                    ),
                    config={"configurable": {"thread_id": child_thread}},
                )
                handle.add(
                    status=str(state.get("error_category") or "completed"),
                    tool_calls=len(state.get("tool_observations") or []),
                )
    except Exception:
        await _discard_child_thread(child_thread)
        logger.exception(
            "委派子调用失败。",
            extra={"event": "delegate_failed", "to_agent": target, "depth": hop},
        )
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="failed",
            depth=hop,
            task=task,
            reason="exception",
            refs=refs_summary,
            extra=audit_extra,
        )
        # 子调用崩在审批之前，所以「崩了」不会留下未知态的外部副作用。
        return _failed(
            InvocationErrorCode.FAILED,
            f"「{display_name or target}」没有完成这次转交，请稍后重试或直接说明需要什么信息。",
            target=target,
            depth=hop,
            display_name=display_name,
        )

    # 子图停在审批上：**不删它的检查点**，用户确认后要从那一跳恢复。
    # 这一步必须在读 `final_response` 之前判断——中断时那些字段根本没生成。
    pending = extract_interrupt(state)
    if pending is not None and pending.get("kind") == "approval":
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=True,
            status=STATUS_AWAITING_APPROVAL,
            depth=hop,
            task=task,
            refs=refs_summary,
            approval_id=str(pending.get("approval_id") or "") or None,
            extra=audit_extra,
        )
        payload = awaiting_approval_result(
            pending,
            target=target,
            display_name=display_name,
            depth=hop,
        )
        return Invocation(
            status=STATUS_AWAITING_APPROVAL,
            target=target,
            display_name=display_name,
            message=str(payload["summary"]),
            approval=payload,
            depth=hop,
            readonly=not boundary.can_write,
            child_context=child_context_of(
                context,
                boundary=boundary,
                limits=limits,
                counts_against_budget=counts_against_budget,
            ),
        )

    await _discard_child_thread(child_thread)

    answer = str(state.get("final_response") or "").strip()
    error = str(state.get("error_category") or "")
    evidence = evidence_of(state)
    unknown = [str(item) for item in (state.get("missing_fields") or [])]

    if error and not answer:
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="failed",
            depth=hop,
            task=task,
            reason=error,
            refs=refs_summary,
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.FAILED,
            f"「{display_name or target}」未能完成（{error}）。",
            target=target,
            depth=hop,
            display_name=display_name,
        )

    # 无证据结论不过桥（`AGENT_CONNECT.md` §4 第 7 条）。
    #
    # 判据是「三样都没有」：没调用过工具、没声明缺什么、发起方也没给材料。
    # 这种回答只可能是凭空生成的，转述出去就是把 A 的信用借给一段捏造。
    if not evidence and not unknown and not materials:
        await _audit(
            context,
            target=target,
            display_name=display_name,
            agent_id=boundary.agent_id,
            success=False,
            status="failed",
            depth=hop,
            task=task,
            reason="no_evidence",
            extra=audit_extra,
        )
        return _failed(
            InvocationErrorCode.NO_EVIDENCE,
            f"「{display_name or target}」给了结论但没有任何依据"
            "（既没查资料，也没说明缺什么），不能当作事实转述。"
            "请在任务里明确要求它先检索或先说明缺什么，再转交一次。",
            target=target,
            depth=hop,
            display_name=display_name,
        )

    await _audit(
        context,
        target=target,
        display_name=display_name,
        agent_id=boundary.agent_id,
        success=True,
        status=STATUS_COMPLETED,
        depth=hop,
        task=task,
        refs=refs_summary,
        extra=audit_extra,
    )

    return Invocation(
        status=STATUS_COMPLETED,
        target=target,
        display_name=display_name,
        output=answer,
        message=f"「{display_name or target}」已完成转交的任务。",
        evidence=evidence,
        unknown=unknown,
        depth=hop,
        readonly=not boundary.can_write,
        child_context=child_context_of(
            context,
            boundary=boundary,
            limits=limits,
            counts_against_budget=counts_against_budget,
        ),
    )
