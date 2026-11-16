# app/mcp_server/tools.py
"""对外暴露的两个工具：一次委派，一个句柄查询。

**为什么只有一个能力入口。** PAP 是多智能体平台，它本身就是智能体。
把「查台账」「查知识库」「跑编排」拆成独立工具交出去，等于让调用方
替 PAP 做规划——那正是 PAP 规划器存在的理由，也是这些工具之间
上下文（站点、型号、告警等级）唯一的交汇处。拆开之后，调用方得自己
记住上一跳的结论，再决定下一步调哪个；它做不到，也不该做。

所以：**一整段意图交过去，PAP 决定用什么。** 调用方只负责两件事——
把话带到，把结论带回来。

**委派方最容易犯的错是重写结论。** 返回体里带 `trace` 与 `citations`，
并提供 `hint` 说明这一步该怎么转述。少了这层，PAP 查到的规程会被
上层模型洗成它自己「知道」的事，出了错连是哪一步错的都追不回来。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory
from app.mcp_server.auth import identity_from_ctx
from app.models.conversation import AgentRun, Message, MessageRole, RunStatus
from app.observability.context import span
from app.services import agent_service
from app.services.chat_service import ChatService
from app.tenancy.context import tenant_scope
from app.tenancy.threads import (
    client_thread_id,
    internal_thread_id,
    validate_client_thread_id,
)

logger = logging.getLogger(__name__)

# 还在跑的委派必须留一个强引用：`asyncio` 只持弱引用，事件循环随时可能
# 把任务回收掉，表现是「委派一开始就没了、日志里什么都没有」。
_RUNNING: set[asyncio.Task] = set()

MAX_TASK_CHARS = 8000

# 等待地板。只防 0 与负数变成「永不返回」，不是性能参数：
# 调用方想要立刻拿句柄，传 0.1 就该给他 0.1，而不是被抬到几十秒。
_MIN_WAIT_SECONDS = 0.5

# 对外只有这五个状态。`ChatResponse` 内部的 `error` 与 `rejected`
# 都是「没办成」，在 `_shape` 里折成 `failed`——漏掉的那个分支才是真正的坑：
# 调用方按五个值写 switch，第六个值会掉进 default，而 default 通常是
# 「当作正常结束」或者什么都不做。
_STATUS_ALIASES = {"error": "failed", "rejected": "failed"}

# 能力声明。MCP 注解是**给宿主看的**，本身不构成服务端约束——真正挡住写操作的
# 是网关里的 `agent_can_write` 与图内审批，不是这几个布尔值。写它们的目的只有一个：
# 让宿主在「可能产生副作用」的调用上给出提示，而不是把委派当成无风险查询放行。
_ANNOTATIONS_DELEGATE = ToolAnnotations(
    title="委派给 PAP 智能体平台",
    # 关键位：PAP 侧可能建单、改配置。宿主看到 False 才会在自动执行前提醒。
    read_only_hint=False,
    # 只增不毁——写操作是落一张工单，不是删除或覆盖现场数据。
    destructive_hint=False,
    # 同一句话交两次就是两件事，不是幂等重放。
    idempotent_hint=False,
    # 会去查外部数据源（站点目录、台账、规程），结果不由 PAP 独占。
    open_world_hint=True,
)

_ANNOTATIONS_TASK_STATUS = ToolAnnotations(
    title="查询委派任务结果",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# `completed` 有两种：拿到了东西，和「办完了但没拿到数据」。
# 后者沿用同一句 hint 时，调用方会把「远端拒绝了」当成结论转述出去——
# 这正是第一次真实委派踩到的：status=completed、answer 是一句拒绝、
# 上层照 hint 说「PAP 已经办完」。
_HINT_COMPLETED_NO_DATA = (
    "PAP 没能拿到数据：answer 里是远端拒绝或工具失败的原因，不是结论。"
    "如实转述原因，不要包装成结论，也不要原样重试。"
)

# 每个状态的 `hint` 是接口契约，不是提示词修辞。
# 三件事必须写死：不要重写结论、不要拆解任务、不要在原样失败后重试。
_HINTS = {
    "completed": (
        "PAP 已经办完。把 answer 转述给用户时标明结论来自 PAP；"
        "citations 里的出处要保留，不要改写成自己的判断。"
    ),
    "needs_input": (
        "PAP 缺必要信息，这一轮没有办成。把 questions 拿去问用户，"
        "拿到答案后带上同一个 conversation_id 再委派一次。不要把反问当成结论。"
    ),
    "awaiting_approval": (
        "写操作已停在审批，尚未执行。请让用户在 PAP 控制台确认这张审批单；"
        "不要重试这次委派，重试只会再开一张单。"
    ),
    "running": (
        "任务还在跑。用 pap_task_status 取回结果，不要重新委派同一个任务。"
    ),
    "failed": (
        "任务失败。原因在 error 里，先如实转述；不要原样重试，"
        "失败大多是参数或权限问题，重试不会变好。"
    ),
}


def _settle(task: asyncio.Task) -> None:
    """收尾：摘掉强引用，并把没人等的异常取出来。

    超时移交之后这一轮没人 `await`：不主动取一次异常，Python 只在垃圾回收时
    打一行「Task exception was never retrieved」，既没有会话号也没有原因，
    而这正是最需要日志的那种失败（用户那边只看到任务一直 running）。
    """
    _RUNNING.discard(task)

    if task.cancelled():
        return

    exc = task.exception()

    if exc is not None:
        logger.error(
            "超时移交后的委派执行失败。",
            extra={
                "event": "mcp_delegate_detached_failed",
                "conversation": getattr(task, "get_name", lambda: "")(),
                "error": type(exc).__name__,
            },
            exc_info=exc,
        )


def _dump(payload: dict[str, Any]) -> str:
    """工具结果统一走 JSON 文本。

    不返回结构化内容块：调用方拿到的是「一段可以读的文字」还是
    「一个对象」，决定了它在模型上下文里被怎么对待。文本更稳，
    而且各家客户端对 structuredContent 的支持并不一致。
    """
    return json.dumps(payload, ensure_ascii=False)


def _outcome(status: str, error_category: str | None) -> dict[str, str]:
    """把 `error_category` 分到两个字段上。

    顶层 `error` 只表示「任务没办成」，是调用方唯一必须处理的那一种。
    任务办完了、只是某个工具失败，那是过程细节，放 `tool_error`。

    两者混在同一个字段里的后果是实测过的：`status=completed` 配一个
    `error`，调用方一边看到「完成」一边看到「错误」，按哪个处理全凭运气——
    实际发生的是「什么都没有查到」被当成正常结论转述了出去。
    """
    if not error_category:
        return {}

    if status == "failed":
        return {"error": error_category}

    return {"tool_error": error_category}


def _shape(
    *,
    task_id: str | None,
    status: str,
    conversation_id: str | None,
    answer: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    trace: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
    approval_id: str | None = None,
    error: str | None = None,
    tool_error: str | None = None,
    read_only: bool = False,
) -> str:
    # 折在这里而不是各调用点上：`delegate` 与 `_shape_run` 都会经过这里，
    # 状态别名只要有一处漏了，同一次失败就会有两种对外写法。
    status = _STATUS_ALIASES.get(status, status)

    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "conversation_id": conversation_id,
        "answer": answer,
        "hint": _HINTS.get(status),
    }

    if citations:
        payload["citations"] = citations
    if trace:
        payload["trace"] = trace
    if questions:
        payload["questions"] = questions
    if approval_id:
        payload["approval_id"] = approval_id
    if error:
        payload["error"] = error
    if read_only:
        # 只在这一轮确实被收窄时出现。调用方据此解释「为什么没有落单」：
        # 不是 PAP 不会写，是这次没给它写的能力。
        payload["read_only"] = True
    if tool_error:
        payload["tool_error"] = tool_error
        if status == "completed":
            # 结论是「没拿到数据」，不是「办完了」。
            payload["hint"] = _HINT_COMPLETED_NO_DATA

    return _dump(payload)


def _questions_from(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """把追问卡片拆成「问什么 + 有哪些候选」。

    界面那一侧渲染成单选卡；委派方没有这个界面，只能拿到结构化的清单，
    再转成人话去问用户。**不要给它拼好的整段文本**——那样它没法判断
    哪些项已经有答案、哪些还得问。
    """
    clarify = (metadata or {}).get("clarify") or {}
    items = clarify.get("items") or []
    out: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        options = [
            str(option.get("label") or "").strip()
            for option in (item.get("options") or [])
            if isinstance(option, dict) and str(option.get("label") or "").strip()
        ]
        out.append({"question": question, "options": options})

    return out


def _capability_boundary(boundary, *, allow_write: bool):
    """把调用方声明的能力叠到智能体边界上。

    **只能收窄，不能放宽。** 能不能写是智能体自身的属性（`agents.can_write`），
    调用方无权凭一次参数就取得它——所以这里只有一个方向：True 改 False，
    其它情况原样返回。已经只读的智能体传 `allow_write=True` 也不会变回可写。

    收窄之后落到网关层就是 `agent_can_write=False`：写风险工具**从能力目录里消失**，
    既不会被执行，也不会留下一条没人会去点的待审批记录。目录过滤与执行拦截
    共用 `ToolGateway.agent_denial` 这一份判断，所以两处不会分叉。
    """
    if allow_write:
        return boundary
    return replace(boundary, can_write=False)


async def _boundary(identity) -> Any:
    """取本轮该用的智能体边界。

    不带 `agent` 参数是刻意的：调用方一旦能钉死某个智能体，
    PAP 之后调整内部分工它感知不到，连接器显示「已连接」而行为已经过时。
    要按角色分，就做成两个连接器，而不是一个连接器加一个参数。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            boundary = await agent_service.resolve_for_chat(
                session,
                tenant_id=identity.tenant_id,
                ref=None,
                actor_user_id=identity.user_id,
            )
            # 默认 Agent 是惰性创建的：不提交的话下一次请求又要建一遍。
            await session.commit()
            return boundary


async def _run_turn(*, identity, thread_id: str, message: str, boundary) -> Any:
    return await ChatService().handle_message(
        identity=identity,
        thread_id=thread_id,
        message=message,
        boundary=boundary,
    )


async def _run_by_thread(*, identity, thread_id: str) -> AgentRun | None:
    """按内部线程取最近一次运行。

    超时之后要靠它把 task_id 交出去。用 `started_at` 倒序而不是
    「状态为 RUNNING」：恰好卡在边界上时，那一轮可能已经跑完了，
    这时直接返回结论比返回一个已经失效的 running 更有用。
    """
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            return await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == identity.tenant_id,
                    AgentRun.thread_id == thread_id,
                    AgentRun.user_id == identity.user_id,
                )
                .order_by(AgentRun.started_at.desc())
                .limit(1)
            )


async def _answer_of(identity, run: AgentRun) -> str | None:
    """运行结束后取那一条回答。

    `agent_runs.output_summary` 只存前 2000 字，够日志不够交付；
    正文以 `messages` 为准，同一条消息也是前端刷新后显示的那一条。
    """
    if run.conversation_id is None:
        return run.output_summary

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(Message)
                .where(
                    Message.tenant_id == identity.tenant_id,
                    Message.conversation_id == run.conversation_id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.created_at >= run.started_at,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )

    return (row.content if row else None) or run.output_summary


def _status_of(run: AgentRun) -> str:
    if run.status is RunStatus.COMPLETED:
        return "completed"
    if run.status is RunStatus.WAITING_FOR_APPROVAL:
        return "awaiting_approval"
    if run.status in (RunStatus.FAILED,):
        return "failed"
    if run.status is RunStatus.CANCELLED:
        return "failed"
    return "running"


async def _shape_run(*, identity, run: AgentRun) -> str:
    """把一行运行记录折成与 `pap_delegate` 同形的返回体。

    同形是刻意的：调用方不该区分「这次是当场拿到的」还是「后来查到的」，
    否则它要写两套解析。差别只在 `agent` 拿不到——那条信息在本次调用的
    边界里，重新查库拿不到准确值，宁缺勿编。
    """
    status = _status_of(run)

    answer = await _answer_of(identity, run) if status == "completed" else None

    return _shape(
        task_id=str(run.id),
        status=status,
        conversation_id=client_thread_id(run.thread_id) if run.thread_id else None,
        answer=answer,
        trace={
            "agent": None,
            "intent": run.intent,
            "tool": run.tool_name,
        },
        approval_id=run.approval_id if status == "awaiting_approval" else None,
        **_outcome(status, run.error_category),
    )


async def delegate(
    *,
    identity,
    task: str,
    conversation_id: str | None = None,
    wait_seconds: float | None = None,
    allow_write: bool = True,
) -> str:
    """委派的实体。身份由调用方给，不从请求里摸。

    这样拆是为了可测：身份解析要连库、要验签，而「怎么等、等不到怎么办、
    返回体长什么样」才是容易写错的部分，它不该被绑在一次真实 HTTP 请求上。
    """
    message = (task or "").strip()

    if not message:
        return _shape(
            task_id=None,
            status="failed",
            conversation_id=conversation_id,
            error="task 不能为空。",
        )

    if len(message) > MAX_TASK_CHARS:
        return _shape(
            task_id=None,
            status="failed",
            conversation_id=conversation_id,
            error=f"task 超过 {MAX_TASK_CHARS} 字，请精简后重试。",
        )

    try:
        client_thread = validate_client_thread_id(
            conversation_id or f"mcp-{uuid.uuid4().hex[:12]}"
        )
    except ValueError as exc:
        return _shape(
            task_id=None,
            status="failed",
            conversation_id=conversation_id,
            error=str(exc),
        )

    boundary = await _boundary(identity)
    boundary = _capability_boundary(boundary, allow_write=allow_write)

    wait = settings.mcp_inbound_wait_seconds
    if wait_seconds is not None:
        # 只允许缩短：配置才是上限。让调用方要多久就等多久，
        # 等于把「一次请求最坏占多久」交出去。
        wait = max(_MIN_WAIT_SECONDS, min(float(wait_seconds), wait))

    async with span(
        "mcp.pap_delegate",
        "mcp",
        attrs={
            "tool": "pap_delegate",
            "source": "mcp",
            "agent": boundary.agent_name,
            "conversation": client_thread,
        },
    ):
        turn = asyncio.create_task(
            _run_turn(
                identity=identity,
                thread_id=client_thread,
                message=message,
                boundary=boundary,
            ),
            name=f"mcp-delegate-{client_thread}",
        )
        _RUNNING.add(turn)
        turn.add_done_callback(_settle)

        done, _ = await asyncio.wait({turn}, timeout=wait)

    if turn in done:
        try:
            response = turn.result()
        except Exception as exc:  # noqa: BLE001 - 委派失败不能变成 MCP 协议错误
            logger.exception(
                "MCP 委派执行失败。",
                extra={"event": "mcp_delegate_failed", "conversation": client_thread},
            )
            return _shape(
                task_id=None,
                status="failed",
                conversation_id=client_thread,
                error=f"委派执行失败：{type(exc).__name__}",
            )

        metadata = response.metadata or {}
        # 状态先折一次，`_outcome` 要按折过的状态决定错误落在哪个字段。
        status = _STATUS_ALIASES.get(response.status, response.status)
        return _shape(
            task_id=response.run_id or None,
            status=status,
            conversation_id=response.thread_id,
            answer=response.answer,
            citations=response.citations,
            trace={
                "agent": boundary.agent_name,
                "intent": metadata.get("intent"),
                "tool": metadata.get("tool_name"),
            },
            questions=_questions_from(metadata),
            approval_id=(response.approval.approval_id if response.approval else None),
            read_only=not allow_write,
            **_outcome(status, response.error_category),
        )

    # 超时：任务继续跑，把句柄交出去。
    internal = internal_thread_id(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        client_id=client_thread,
    )
    run = await _run_by_thread(identity=identity, thread_id=internal)

    if run is not None and _status_of(run) != "running":
        # 边界上刚好跑完：直接给结论，别让调用方为一个已经完成的任务再问一次。
        return await _shape_run(identity=identity, run=run)

    return _shape(
        task_id=str(run.id) if run else None,
        status="running",
        conversation_id=client_thread,
        read_only=not allow_write,
    )


async def task_status(*, identity, task_id: str) -> str:
    """按 task_id 取回委派结果。"""
    try:
        run_id = uuid.UUID(str(task_id).strip())
    except (ValueError, AttributeError):
        return _shape(
            task_id=None,
            status="failed",
            conversation_id=None,
            error="task_id 不是有效标识。",
        )

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)

    # RLS 已经挡了一层，这里再显式比一次：两个租户的记录串了，
    # 是那种「接口 200、内容却是别人的」的事故，代价远大于一行判断。
    if run is None or run.tenant_id != identity.tenant_id:
        return _shape(
            task_id=str(run_id),
            status="failed",
            conversation_id=None,
            error="找不到这个任务。",
        )

    return await _shape_run(identity=identity, run=run)


def register_tools(server) -> None:
    """把工具挂到 MCP 服务端。

    每个工具都是薄壳：取身份 → 调上面的实体 → 返回它给的那段文本。
    业务逻辑一行都不在这里复制，否则「控制台怎么做、MCP 怎么做」
    迟早变成两套行为，而其中一套永远没人测。
    """

    @server.tool(
        name="pap_delegate",
        title="委派给 PAP 智能体平台",
        annotations=_ANNOTATIONS_DELEGATE,
        description=(
            "把一整件事委派给 PAP 智能体平台，拿回结论。"
            "PAP 会自己决定用哪个智能体、调哪些数据源、要不要查规程。"
            "不要拆解任务，不要指定工具，一次把完整意图交过去。"
            "返回 status 为 completed / needs_input / awaiting_approval / "
            "running / failed 之一；除 completed 外都必须按 hint 处理，"
            "尤其不要把 needs_input 当成结论。"
            "completed 但带 tool_error 表示没拿到数据——answer 里是原因，"
            "不是结论。allow_write=false 时返回体带 read_only=true，"
            "这就是它没落单的原因。"
        ),
    )
    async def pap_delegate(
        task: Annotated[
            str,
            Field(
                description=(
                    "完整意图。把已知信息一次写全：站点编号、时间范围、"
                    "现象描述、涉及设备。写的是一件事，不是一串步骤。"
                )
            ),
        ],
        conversation_id: Annotated[
            str | None,
            Field(
                description=(
                    "同一件事分几次交代时带上上一次返回的 conversation_id，"
                    "PAP 会接着上文。省略表示开一件新的事。"
                )
            ),
        ] = None,
        wait_seconds: Annotated[
            float | None,
            Field(
                description=(
                    "本次最多等多久（秒），省略用平台默认值。"
                    "只用于缩短，传更大的值不会更久。"
                )
            ),
        ] = None,
        allow_write: Annotated[
            bool,
            Field(
                description=(
                    "本轮是否允许 PAP 产生写操作（建工单、改配置）。"
                    "默认允许；拿不准就传 false，本轮退化为只读，"
                    "写操作在到达审批前就被拒，返回体带 read_only=true。"
                    "它只能收窄，不能给智能体加上它本来没有的权限。"
                )
            ),
        ] = True,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> str:
        """把一整件事交给 PAP，等待并返回结论。"""
        identity = await identity_from_ctx(ctx)

        if identity is None:
            # 门槛已经挡过一轮，走到这里说明是直接调函数（单测）或门槛被绕过。
            # 仍然 fail-closed：绝不回落到任何默认身份。
            return _shape(
                task_id=None,
                status="failed",
                conversation_id=None,
                error="未授权的调用：缺少有效令牌。",
            )

        return await delegate(
            identity=identity,
            task=task,
            conversation_id=conversation_id,
            wait_seconds=wait_seconds,
            allow_write=allow_write,
        )

    @server.tool(
        name="pap_task_status",
        title="查询委派任务结果",
        annotations=_ANNOTATIONS_TASK_STATUS,
        description=(
            "查询一次委派的结果。只用于 pap_delegate 返回 running 之后续查；"
            "不要用它轮询已完成的任务，也不要用它代替新的一次委派。"
            "返回体与 pap_delegate 同形。"
        ),
    )
    async def pap_task_status(
        task_id: Annotated[
            str,
            Field(description="pap_delegate 返回的 task_id。"),
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> str:
        """按 task_id 取回委派结果。"""
        identity = await identity_from_ctx(ctx)

        if identity is None:
            return _shape(
                task_id=None,
                status="failed",
                conversation_id=None,
                error="未授权的调用：缺少有效令牌。",
            )

        return await task_status(identity=identity, task_id=task_id)
