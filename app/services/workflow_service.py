# app/services/workflow_service.py
"""编排的服务层：图的增删改查，以及一次 run 的建立、推进、续跑。

**这里不做权限判定。** 谁能跑一张图，取决于「他本人的 scope」与「发起方 Agent 的
边界」——两者都在每次节点调用时由 `agent_invoker.invoke()` 逐跳判定。图里连了
不等于有权连（`docs/ORCHESTRATION.md` §2 第一条）。

三条实现上的约定：

1. **`revision` 在 run 开始时冻结**。管理员在流程跑到一半时改图，改的是下一次；
   已发的 run 按旧图走完——否则 `cursor` 会指向一个已经改名的节点。
2. **游标先落库再调用**（`_persist`）。崩在中间的 run 因此知道自己停在哪一步。
3. **续跑不重跑停住的节点**。审批恢复已经把那个节点跑完了，重跑会在外部系统上
   再写一次，那正是审批要防的事。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import delete, select, update

from app.auth.dependencies import Identity
from app.db.session import async_session_factory
from app.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from app.orchestration import engine
from app.orchestration.definition import (
    KIND_AGENT,
    DefinitionError,
    WorkflowPlan,
    compile_definition,
)
from app.services import agent_invoker, agent_service
from app.services.agent_invoker import Invocation
from app.tenancy.context import tenant_scope
from app.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# 会话内的触发词。刻意做成显式前缀而不是「让模型自己挑一张图」
# （`ORCHESTRATION.md` §4 D5）：让模型选图，等于把刚买到的确定性又交回给模型。
COMMAND = "/workflow"

_COMMAND_RE = re.compile(r"^/workflow(?:\s+(\S+))?(?:\s+([\s\S]*))?$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

PENDING_STATUSES = (WorkflowRunStatus.RUNNING, WorkflowRunStatus.PAUSED)


class WorkflowError(Exception):
    """配置层错误。`code` 给接口与前端分支，`message` 直接给人看。

    `node` 是编译期指出的那个节点 id（能定位时才填）。带上它，界面就能把
    对应的那张卡片标红——「第几步错了」比「哪个 id 错了」好找得多。
    """

    def __init__(self, code: str, message: str, *, node: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node


@dataclass(frozen=True, slots=True)
class Command:
    """`/workflow` 解析结果。

    `name` 为空且 `invalid=False` 表示「列出有哪些流程」；
    `invalid=True` 表示名字本身不合法——两者要给不同的话，否则用户会以为
    自己写的流程没配上。
    """

    name: str
    input_text: str
    invalid: bool = False


# ---------------------------------------------------------------- 命令解析


def parse_command(message: str) -> Command | None:
    """解析 `/workflow <name> <输入>`。

    只认这一种写法。不做 `/wf` 之类的别名：多一个入口就多一处「为什么这个不生效」
    的排查，而这个功能的价值恰恰在于「行为可预测」。
    """
    text = (message or "").strip()
    if not text.startswith(COMMAND):
        return None

    match = _COMMAND_RE.match(text)
    if match is None:
        return None

    name = (match.group(1) or "").strip()
    payload = (match.group(2) or "").strip()

    if name and not _NAME_RE.match(name):
        # 名字不合法时不静默当「列清单」：那会让用户以为流程没配。
        return Command(name="", input_text="", invalid=True)
    return Command(name=name, input_text=payload)


def triggered_command(name: str, input_text: str) -> Command:
    """界面上直接选了一条流程时，拼出与 `/workflow` 完全相同的 `Command`。

    两个入口必须共用同一份名字校验：口径分开写，就会出现「手打的名字不合法、
    下拉选同一个名字却放行」，排查时谁也想不到是那两个分支不一样。
    """
    clean = (name or "").strip()
    if not clean or not _NAME_RE.match(clean):
        return Command(name="", input_text="", invalid=True)
    return Command(name=clean, input_text=(input_text or "").strip())


# ---------------------------------------------------------------- 图的管理


async def list_workflows(tenant_id: str) -> list[Workflow]:
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            rows = await session.scalars(select(Workflow).order_by(Workflow.name))
            return list(rows)


async def get_workflow(tenant_id: str, ref: str) -> Workflow | None:
    """按 name 或 id 取。name 优先——它是文档与命令里用的那个。

    租户上下文必须在这里就绑上：`workflows` 挂了 RLS，无上下文时查出来是空行
    而不是报错——那种「静默查不到」最难查。
    """
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            row = await session.scalar(select(Workflow).where(Workflow.name == ref))
            if row is not None:
                return row
            try:
                return await session.get(Workflow, uuid.UUID(ref))
            except (ValueError, AttributeError):
                return None


async def save_workflow(
    *,
    tenant_id: str,
    user_id: str,
    name: str,
    display_name: str,
    description: str | None,
    definition: Any,
    enabled: bool = True,
) -> Workflow:
    """新建或更新。**保存即编译**：存进库的一定是能跑的图。

    改名不允许：名字出现在 `/workflow <name>` 与审计里，改了会让历史记录指向一个
    再也找不到的入口。要改就新建一张。

    「能跑」包含一件 `compile_definition` 查不到的事：图里指定的智能体得真的存在
    且处于启用状态。编译是纯函数（不碰库），所以这一步在本层做——控制台与构建
    助手走同一个 `save_workflow`，两边口径因此不会分叉。

    **只在这张图真的变了时才做那两件事**（校验智能体、+1 修订号）：只改显示名、
    说明或启停时 `definition` 是原样传回的，此时既不该让在飞的 run 看起来「跑的
    是另一版」，也不该因为某个早已删掉的智能体而连名字都改不了。
    """
    if not _NAME_RE.match(name or ""):
        raise WorkflowError(
            "WORKFLOW_BAD_NAME",
            "流程名只能用小写字母开头，后接小写字母、数字、短横线或下划线，最长 64 字符。",
        )

    try:
        plan = compile_definition(definition)
    except DefinitionError as exc:
        raise WorkflowError(exc.code, exc.message, node=exc.node) from exc

    compiled = plan.as_json()

    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await session.scalar(select(Workflow).where(Workflow.name == name))

                graph_changed = row is None or compiled != (row.definition or {})
                if graph_changed:
                    await _ensure_agents_available(session, plan)

                if row is None:
                    row = Workflow(
                        tenant_id=tenant_id,
                        name=name,
                        display_name=(display_name or name)[:128],
                        description=description,
                        definition=compiled,
                        revision=1,
                        enabled=enabled,
                        created_by=user_id,
                    )
                    session.add(row)
                else:
                    row.display_name = (display_name or row.display_name)[:128]
                    row.description = description
                    row.definition = compiled
                    row.enabled = enabled
                    # 修订号只在**定义**变化时 +1：改个显示名不该让在飞的 run
                    # 看起来像「跑的是另一版」。运行记录与 span 上记的就是这个号，
                    # 它回答的是「那次到底跑了哪张图」，不是「这行什么时候被改过」。
                    if graph_changed:
                        row.revision = int(row.revision or 0) + 1

                await session.flush()
                # `updated_at` 是库端的 `onupdate`，flush 之后本地那份已经被置为
                # 过期——这时 `expunge` 会让它变成一个读不出值的游离对象，接口层
                # 拿 `updated_at` 拼响应就 500。先刷新再脱离，代价是一次回读。
                await session.refresh(row)
                session.expunge(row)
                return row


async def delete_workflow(*, tenant_id: str, name: str) -> bool:
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(Workflow).where(Workflow.name == name)
                )
                return bool(result.rowcount)


# ---------------------------------------------------------------- 运行


@dataclass(frozen=True, slots=True)
class RunResult:
    """一次 run 的结论。`outcome` 是引擎给的原始结果，这里补上 run 的 id。"""

    run_id: str
    outcome: engine.Outcome

    @property
    def status(self) -> str:
        return self.outcome.status

    @property
    def answer(self) -> str:
        return self.outcome.answer

    @property
    def approval(self) -> dict[str, Any] | None:
        return self.outcome.approval


async def start(
    *,
    identity: Identity,
    context: ToolExecutionContext,
    workflow: Workflow,
    input_text: str,
    conversation_id: str,
    thread_id: str,
    run_id: str,
) -> RunResult:
    """从头跑一次。

    `context` 是发起方（会话里那个 Agent）的上下文——它决定了第一跳的深度与边界。
    """
    try:
        plan = compile_definition(workflow.definition)
    except DefinitionError as exc:
        # 库里存的一定是编译过的，走到这里说明有人直接改了数据库。
        # 仍然按「这张图跑不了」处理，并留下一条可查的 run。
        return await _record_immediate_failure(
            identity=identity,
            workflow=workflow,
            conversation_id=conversation_id,
            thread_id=thread_id,
            run_id=run_id,
            root_agent=context.agent_name,
            code=exc.code,
            message=f"这张流程的图不成立：{exc.message}",
        )

    # 深度预检只问一件事：**发起方还能不能转交**。
    #
    # 编排的每一跳都以发起方为父（见 `engine.run`），运行时深度恒为 1，
    # 所以「图里有几个 Agent 节点」不再等于「要走几跳」。按节点数判会把一张
    # 3 节点的合法图在 `max_depth=2` 时拒掉——判据与运行时不一致，比不判更坏。
    limits = agent_invoker.effective_limits(context)
    if plan.depth_hops > 0 and limits.max_depth < 1:
        return await _record_immediate_failure(
            identity=identity,
            workflow=workflow,
            conversation_id=conversation_id,
            thread_id=thread_id,
            run_id=run_id,
            root_agent=context.agent_name,
            code="WORKFLOW_DEPTH_EXCEEDED",
            message=(
                f"发起方智能体的「最大转交深度」是 {limits.max_depth} 跳，"
                "等于不允许把任何任务交给别的智能体，这张流程跑不起来。"
                "请把它调到至少 1 跳；或者换一个允许转交的智能体发起。"
            ),
        )

    workflow_run_id = await _create_run(
        identity=identity,
        workflow=workflow,
        conversation_id=conversation_id,
        thread_id=thread_id,
        run_id=run_id,
        root_agent=context.agent_name,
    )

    outcome = await engine.run(
        plan,
        input_text=input_text,
        context=context,
        invoke=_invoker(
            run_id=workflow_run_id,
            workflow_name=workflow.name,
            revision=int(workflow.revision or 0),
        ),
        persist=_persister(workflow_run_id, identity.tenant_id),
    )

    await _finish(
        workflow_run_id=workflow_run_id,
        tenant_id=identity.tenant_id,
        outcome=outcome,
    )
    return RunResult(run_id=workflow_run_id, outcome=outcome)


async def resume(
    *,
    identity: Identity,
    root_context: ToolExecutionContext,
    run: WorkflowRun,
    node_output: str,
    outcome_status: str,
) -> RunResult:
    """从停住的节点继续。

    `outcome_status` 是审批那一跳的结局，三值而不是一个布尔：

    - `ok`：批准并执行成功 → 把结论写进黑板，继续往下；
    - `cancelled`：用户取消 / 过期 → 整条流程取消。继续跑就等于
      「用户拒绝了写操作，但流程替他做了下一步决定」；
    - `failed`：批准了但执行失败 → run 记失败。说成「取消」会把一次真实的
      故障伪装成用户改主意。
    """
    run_id = str(run.id)

    if outcome_status != "ok":
        await _update_run(
            run_id=run_id,
            tenant_id=identity.tenant_id,
            **(
                {
                    "status": WorkflowRunStatus.CANCELLED,
                    "error_code": "approval_rejected",
                    "error_message": "审批被取消，流程停在原处，后续节点未执行。",
                }
                if outcome_status == "cancelled"
                else {
                    "status": WorkflowRunStatus.FAILED,
                    "error_code": "approval_failed",
                    "error_message": "审批对应的写操作没有执行成功，流程在此停下。",
                }
            ),
            finished=True,
        )
        return RunResult(
            run_id=run_id,
            outcome=engine.Outcome(
                status=(
                    engine.STATUS_CANCELLED
                    if outcome_status == "cancelled"
                    else engine.STATUS_FAILED
                ),
                blackboard=dict(run.blackboard or {}),
                nodes_used=int(run.nodes_used or 0),
                cursor=run.cursor,
                answer=str(run.output_text or ""),
                error_code=(
                    "approval_rejected" if outcome_status == "cancelled" else "approval_failed"
                ),
                message=(
                    "审批被取消，流程停在原处，后续节点未执行。"
                    if outcome_status == "cancelled"
                    else "审批对应的写操作没有执行成功，流程在此停下。"
                ),
            ),
        )

    try:
        plan = compile_definition(run_definition(run))
    except DefinitionError as exc:
        await _update_run(
            run_id=run_id,
            tenant_id=identity.tenant_id,
            status=WorkflowRunStatus.FAILED,
            error_code=exc.code,
            error_message=f"这张流程的图已经跑不了：{exc.message}",
            finished=True,
        )
        return RunResult(
            run_id=run_id,
            outcome=engine.Outcome(
                status=engine.STATUS_FAILED,
                blackboard=dict(run.blackboard or {}),
                nodes_used=int(run.nodes_used or 0),
                cursor=run.cursor,
                error_code=exc.code,
                message=exc.message,
            ),
        )

    cursor = run.cursor or plan.entry
    node = plan.nodes.get(cursor)
    if node is None:
        await _update_run(
            run_id=run_id,
            tenant_id=identity.tenant_id,
            status=WorkflowRunStatus.FAILED,
            error_code="WORKFLOW_UNKNOWN_NODE",
            error_message=f"续跑时找不到节点「{cursor}」，图可能已经被改过。",
            finished=True,
        )
        return RunResult(
            run_id=run_id,
            outcome=engine.Outcome(
                status=engine.STATUS_FAILED,
                blackboard=dict(run.blackboard or {}),
                nodes_used=int(run.nodes_used or 0),
                cursor=run.cursor,
                error_code="WORKFLOW_UNKNOWN_NODE",
                message=f"续跑时找不到节点「{cursor}」，图可能已经被改过。",
            ),
        )

    blackboard = dict(run.blackboard or {})
    nodes_json = list(run.nodes_json or [])
    visited = list(blackboard.keys())

    # 续跑同样以**发起方**为父（与 `engine.run` 里那段注释同一条理由）：
    # 停住的节点是被发起方调度的一跳，不是被上一个节点转交的。
    # 逐跳重放会把它算成「上一节点 → 它」，于是「只读查完交给可写落单」被判越权，
    # 恢复后第一步就死——而这一跳其实已经被人批过了。
    context = await _hydrate_root(root_context)

    resumed = engine.NodeOutcome(
        node_id=node.id,
        kind=node.kind,
        status=engine.NODE_OK,
        agent=node.agent,
        output=node_output,
        depth=len(context.delegation_path) + 1,
    )

    outcome = await engine.run(
        plan,
        input_text="",
        context=context,
        invoke=_invoker(
            run_id=run_id,
            workflow_name=str(run.workflow_name),
            revision=int(run.revision or 0),
        ),
        persist=_persister(run_id, identity.tenant_id, prior_nodes=nodes_json),
        snapshot=engine.Snapshot(
            cursor=cursor,
            blackboard=blackboard,
            nodes_used=int(run.nodes_used or 0),
            visited=visited,
        ),
        resumed=resumed,
        # 续跑时 head 里已经把暂停节点算过一次，但它在暂停时没有计入 nodes_used。
        max_nodes=None,
    )

    await _finish(
        workflow_run_id=run_id,
        tenant_id=identity.tenant_id,
        outcome=outcome,
        prior_nodes=nodes_json,
    )
    return RunResult(run_id=run_id, outcome=outcome)


def run_definition(run: WorkflowRun) -> Any:
    """run 行上冻结的图。

    不读 `workflows` 表的当前行：管理员可能已经改图，而这条 run 的游标是按旧图
    走过来的。拿新图跑旧游标，轻则节点找不到，重则跳过某个本该执行的节点。
    """
    return run.definition or {}


async def find_paused_run(*, tenant_id: str, approval_id: str) -> WorkflowRun | None:
    """这张审批单是不是某条编排停下来的原因。"""
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            return await session.scalar(
                select(WorkflowRun)
                .where(WorkflowRun.approval_id == approval_id)
                .where(WorkflowRun.status == WorkflowRunStatus.PAUSED)
                .order_by(WorkflowRun.started_at.desc())
            )


async def recent_runs(
    *, tenant_id: str, workflow_name: str, limit: int = 20
) -> list[WorkflowRun]:
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            rows = await session.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.workflow_name == workflow_name)
                .order_by(WorkflowRun.started_at.desc())
                .limit(limit)
            )
            return list(rows)


async def list_runs(*, tenant_id: str, limit: int = 50) -> list[WorkflowRun]:
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            rows = await session.scalars(
                select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(limit)
            )
            return list(rows)


async def get_run(tenant_id: str, run_id: str) -> WorkflowRun | None:
    with tenant_scope(tenant_id):
        async with async_session_factory() as session:
            try:
                return await session.get(WorkflowRun, uuid.UUID(run_id))
            except (ValueError, AttributeError):
                return None


# ---------------------------------------------------------------- 内部


# 智能体状态的界面说法。起草工具也要用（它不抛异常，只把「这一步停了」说给人听），
# 所以是公开的：两份文案各写一遍，会出现「保存说你停用了、起草说你启用了」。
AGENT_STATUS_TEXT = {"draft": "草稿", "disabled": "停用", "active": "启用"}

# 拒绝时列几个可用标识。列全一屏没有意义，列三个又能让人看出「我该填什么」——
# 取 8 是本层自己定的上限，与界面无关。
_AGENT_HINTS = 8


def plan_agents(plan: WorkflowPlan) -> list[tuple[str, str]]:
    """图里用到的智能体，`(第一次出现的节点 id, 标识)`。

    同一个智能体出现在多个节点上只校验一次，但**保留第一次出现的位置**：
    拒绝理由要说「第几步」，说「图里有错」等于没说。

    公开（`_plan_agents` 改名）：起草工具要拿同一份清单去查「谁不在、谁停用了」，
    另写一份「大概一样的遍历」迟早与保存时的判定分叉——而分叉的表现是
    「起草说没问题、保存被拒」，比不校验更让人不信任。
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    for nid in plan.order:
        node = plan.nodes[nid]
        names = [node.agent] if node.kind == KIND_AGENT and node.agent else []
        names.extend(branch.agent for branch in node.branches if branch.agent)

        for name in names:
            if name in seen:
                continue
            seen.add(name)
            out.append((nid, name))

    return out


async def _ensure_agents_available(session, plan: WorkflowPlan) -> None:
    """图里的智能体必须存在且处于启用状态。

    这条校验不属于 `compile_definition`：那是纯函数，读不到库。可是「保存即编译，
    存进库的一定是能跑的图」这句话如果漏掉它，就只对了一半——图里写一个不存在的
    智能体照样存得进去，一直到有人在对话里 `/workflow` 触发它才失败，而那时看到的
    是一句「当前租户下没有名为「x」的智能体」，配图的人早就不在屏幕前了。

    用自然语言建流程时这不是边角情况：模型不知道租户里有哪些智能体，凭语义编一个
    `inspector` 是最自然的动作。所以拒绝理由必须把**可用的标识**一起给出来，
    下一轮它才能改对，而不是反复试名字。
    """
    wanted = plan_agents(plan)
    if not wanted:
        return

    rows = await agent_service.list_agents(session)
    available = {str(row.name): row for row in rows}

    for nid, name in wanted:
        row = available.get(name)
        if row is None:
            names = [str(item.name) for item in rows[:_AGENT_HINTS]]
            hint = (
                f"当前租户的智能体：{'、'.join(names)}"
                + ("…" if len(rows) > _AGENT_HINTS else "")
                + "。"
                if names
                else "当前租户还没有任何智能体。"
            )
            raise WorkflowError(
                "WORKFLOW_UNKNOWN_AGENT",
                f"第「{nid}」步指定的智能体「{name}」不存在。{hint}"
                "要用别的智能体，先把它建出来并启用，再回来保存这张图。",
                node=nid,
            )

        status = str(getattr(row.status, "value", row.status))
        if status != "active":
            raise WorkflowError(
                "WORKFLOW_INACTIVE_AGENT",
                f"第「{nid}」步指定的智能体「{name}」当前是"
                f"{AGENT_STATUS_TEXT.get(status, status)}状态，流程跑到那一步会被拒。"
                "先启用它，或换一个启用中的智能体。",
                node=nid,
            )


def _invoker(*, run_id: str, workflow_name: str, revision: int):
    """节点调用器：编排与委派共用 `agent_invoker.invoke`。

    两个开关是全篇最关键的一处（`ORCHESTRATION.md` §4 D3）：
    - `counts_against_budget=False`：人写死的 5 步不该被「模型每轮最多 3 次」卡死在第四步；
    - `allow_revisit=True`：同一个 Agent 在图里出现两次是正常的，终止性由 DAG 与
      节点数上限保证。
    深度不在此列——它照旧逐跳累积，因为那是链路安全阀，不是配额。
    """

    async def call(
        parent: ToolExecutionContext,
        *,
        agent: str,
        task: str,
        node_id: str,
        branch: str,
    ) -> Invocation:
        return await agent_invoker.invoke(
            parent,
            target=agent,
            task=task,
            counts_against_budget=False,
            allow_revisit=True,
            span_attrs={
                "workflow_run_id": run_id,
                "workflow": workflow_name,
                "node_id": node_id,
                "branch": branch or None,
                "revision": revision,
            },
            audit_extra={
                "workflow_run_id": run_id,
                "workflow": workflow_name,
                "node_id": node_id,
                "branch": branch or None,
                "revision": revision,
            },
        )

    return call


def _merge_nodes(
    prior: list[dict[str, Any]] | None,
    tail: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把「这次续跑的流水」接在「暂停前已经跑过的流水」后面。

    不接的话，续跑写回的 `nodes_json` 只有恢复之后的那几步——运行页上一条停过
    审批的 run，会看起来像是从第二个节点凭空开始的。末尾那条 `paused` 记录要去掉：
    它记录的是「还没产出」，而本次 trace 的第一条正是它的产出。
    """
    head = [dict(item) for item in (prior or []) if isinstance(item, dict)]
    if head and head[-1].get("status") == engine.NODE_PAUSED:
        head.pop()
    return head + [dict(item) for item in tail]


def _persister(run_id: str, tenant_id: str, *, prior_nodes: list[dict[str, Any]] | None = None):
    """把「现在跑到哪」落库。在调用**之前**发生（引擎负责保证顺序）。"""

    async def save(
        *,
        cursor: str,
        blackboard: dict[str, dict[str, Any]],
        nodes_used: int,
        visited: list[str],
        status: str,
        approval_id: str | None = None,
        nodes: list[dict[str, Any]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "cursor": cursor,
            "blackboard": _jsonable(blackboard),
            "nodes_used": nodes_used,
        }
        if nodes is not None:
            payload["nodes_json"] = _merge_nodes(prior_nodes, nodes)
        if approval_id:
            payload["approval_id"] = approval_id
        if status == engine.STATUS_PAUSED:
            payload["status"] = WorkflowRunStatus.PAUSED

        await _update_run(run_id=run_id, tenant_id=tenant_id, **payload)

    return save


async def _create_run(
    *,
    identity: Identity,
    workflow: Workflow,
    conversation_id: str,
    thread_id: str,
    run_id: str,
    root_agent: str = "",
) -> str:
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = WorkflowRun(
                    tenant_id=identity.tenant_id,
                    workflow_id=workflow.id,
                    workflow_name=workflow.name,
                    revision=int(workflow.revision or 0),
                    thread_id=thread_id,
                    conversation_id=_as_uuid(conversation_id),
                    run_id=run_id,
                    status=WorkflowRunStatus.RUNNING,
                    definition=workflow.definition,
                    cursor=None,
                    blackboard={},
                    nodes_json=[],
                    nodes_used=0,
                    started_by=identity.user_id,
                    root_agent=(root_agent or None),
                )
                session.add(row)
                await session.flush()
                return str(row.id)


async def _update_run(
    *,
    run_id: str,
    tenant_id: str,
    status: WorkflowRunStatus | None = None,
    finished: bool = False,
    **values: Any,
) -> None:
    """独立事务、失败只记日志：编排的收尾不该把「已经跑完的节点」变成报错。"""
    from datetime import UTC, datetime

    payload = dict(values)
    if status is not None:
        payload["status"] = status
    if finished:
        payload["finished_at"] = datetime.now(UTC)

    try:
        with tenant_scope(tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(WorkflowRun)
                        .where(WorkflowRun.id == uuid.UUID(run_id))
                        .values(**payload)
                    )
    except Exception:
        logger.exception(
            "编排运行记录写入失败。",
            extra={"event": "workflow_run_persist_failed", "workflow_run_id": run_id},
        )


async def _finish(
    *,
    workflow_run_id: str,
    tenant_id: str,
    outcome: engine.Outcome,
    prior_nodes: list[dict[str, Any]] | None = None,
) -> None:
    status_map = {
        engine.STATUS_COMPLETED: WorkflowRunStatus.COMPLETED,
        engine.STATUS_FAILED: WorkflowRunStatus.FAILED,
        engine.STATUS_PAUSED: WorkflowRunStatus.PAUSED,
        engine.STATUS_CANCELLED: WorkflowRunStatus.CANCELLED,
    }

    await _update_run(
        run_id=workflow_run_id,
        tenant_id=tenant_id,
        status=status_map.get(outcome.status, WorkflowRunStatus.FAILED),
        cursor=outcome.cursor,
        blackboard=_jsonable(outcome.blackboard),
        nodes_json=_merge_nodes(
            prior_nodes, [item.as_dict() for item in outcome.trace]
        ),
        nodes_used=outcome.nodes_used,
        output_text=(outcome.answer or "")[:20000] or None,
        error_code=outcome.error_code or None,
        error_message=outcome.message or None,
        approval_id=(outcome.approval or {}).get("approval_id") or None,
        finished=outcome.status != engine.STATUS_PAUSED,
    )


async def _record_immediate_failure(
    *,
    identity: Identity,
    workflow: Workflow,
    conversation_id: str,
    thread_id: str,
    run_id: str,
    root_agent: str,
    code: str,
    message: str,
) -> RunResult:
    """还没开跑就失败的 run 也要留痕。

    不记的话，「点了没反应」与「流程不存在」在界面上长得一样。
    """
    workflow_run_id = await _create_run(
        identity=identity,
        workflow=workflow,
        conversation_id=conversation_id,
        thread_id=thread_id,
        run_id=run_id,
        root_agent=root_agent,
    )

    outcome = engine.Outcome(
        status=engine.STATUS_FAILED,
        blackboard={},
        nodes_used=0,
        cursor=None,
        error_code=code,
        message=message,
    )
    await _finish(workflow_run_id=workflow_run_id, tenant_id=identity.tenant_id, outcome=outcome)
    return RunResult(run_id=workflow_run_id, outcome=outcome)


async def _hydrate_root(root: ToolExecutionContext) -> ToolExecutionContext:
    """把发起方 Agent 的边界补回根上下文。

    续跑时手上只有「谁发起的」（`root_agent`），没有它当时的边界。不补的话，
    暂停那一跳会拿到一个「什么都不收窄」的根——而深度上限会退回平台默认，
    于是同一条 run 在中断前后适用两套判定。

    查不到（Agent 已删除）时保留原样：删掉的 Agent 无法收窄，只会更严不会更宽。
    """
    if not root.agent_name:
        return root

    try:
        with tenant_scope(root.tenant_id):
            async with async_session_factory() as session:
                row = await agent_service.get_agent(session, ref=root.agent_name)
                boundary = agent_service.boundary_of(row)
    except Exception:
        logger.info(
            "续跑时取不出发起方边界，按不额外收窄处理。",
            extra={"event": "workflow_root_agent_missing", "agent": root.agent_name},
        )
        return root

    return replace(
        root,
        agent_id=boundary.agent_id,
        agent_revision=boundary.revision,
        agent_allowed_tools=boundary.allowed_tools,
        agent_allowed_skills=boundary.allowed_skills,
        agent_allowed_connectors=boundary.allowed_connectors,
        agent_can_write=boundary.can_write,
        agent_max_delegation_depth=boundary.max_delegation_depth,
        agent_delegation_budget=boundary.delegation_budget,
    )


def resume_context(
    identity: Identity,
    run: WorkflowRun,
    *,
    thread_id: str,
    run_id: str,
) -> ToolExecutionContext:
    """续跑的起点上下文。

    这里刻意只放**身份**与发起方名字：编排的每一跳都以发起方为父，所以
    「恢复」与「首次执行」用的是同一个根，不需要预先算出一条链路塞进来。
    之前这里靠 `nodes_json` 逐跳重放，代价是恢复后的第一跳被算成「上一节点转交」，
    于是「只读查完交给可写落单」在恢复时被判越权。
    """
    return ToolExecutionContext(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        thread_id=thread_id,
        run_id=run_id,
        request_id="",
        roles=tuple(identity.roles),
        scopes=tuple(identity.scopes),
        is_platform_admin=bool(identity.is_platform_admin),
        conversation_id=str(run.conversation_id or ""),
        agent_name=str(run.root_agent or ""),
        delegation_origin_thread=thread_id,
    )


def _jsonable(blackboard: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """黑板进 JSON 列前的归一化。

    只留可序列化的部分：`keys` 里可能是模型产出的一段文本，也可能是条件用到的
    数字，两种都能进 JSON；但 `__` 开头的内部键不进——它们不属于「结论」，
    写进去之后运行页会把它们当成一个节点的产出显示出来。
    """
    out: dict[str, dict[str, Any]] = {}

    for node_id, entry in (blackboard or {}).items():
        if str(node_id).startswith("__"):
            continue
        if not isinstance(entry, dict):
            continue
        keys = {
            str(k): v
            for k, v in (entry.get("keys") or {}).items()
            if not str(k).startswith("__")
        }
        out[str(node_id)] = {
            "text": str(entry.get("text") or ""),
            "keys": keys,
            "truncated": bool(entry.get("truncated")),
            **({"missing": list(entry.get("missing") or [])} if entry.get("missing") else {}),
        }

    return out


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def plan_of(workflow: Workflow) -> WorkflowPlan | None:
    """给接口层用：列表里显示节点数与跳数。坏图返回 None 而不是抛。"""
    try:
        return compile_definition(workflow.definition)
    except DefinitionError:
        return None


__all__ = [
    "COMMAND",
    "Command",
    "RunResult",
    "WorkflowError",
    "delete_workflow",
    "find_paused_run",
    "get_run",
    "get_workflow",
    "list_runs",
    "list_workflows",
    "parse_command",
    "plan_of",
    "recent_runs",
    "resume_context",
    "resume",
    "run_definition",
    "save_workflow",
    "start",
]
