# app/tools/builtins/studio_tools.py
"""构建助手（studio）的平台管理工具。

这组工具让「用自然语言改平台配置」走同一条治理链路：注册表、`ToolGateway`、
审批、审计、观测，一个都不绕。它们是**唯一能把 Agent 配置写进库的工具**，
所以每一条校验都要「拒绝优先」——宁可让助手说做不到，也不要它悄悄放宽一个边界。

三条红线（对应 `docs/STUDIO_ASSISTANT.md` 第 3 节）：

1. **不能碰内置智能体。** `agent_service` 已挡，这里再挡一次只为了给出可读的理由：
   直接抛到模型面前的 `AgentValidationError` 会变成一句干巴巴的报错。
2. **不能把 `studio_*` 工具授予别的 Agent。** 那等于造一个能绕过审批改配置的 Agent，
   而且它的每次修改都会「有审批」——审计上看起来完全正常。
3. **写进边界的工具必须是申请人自己可见的。** 否则配出来一条都调不动：
   运行时的 `人 ∩ 边界` 会把它全部挡掉，而用户以为已经配好了。

第 3 条不是安全需要（安全由那层交集保证），是可用性需要：让配置错误在保存时暴露。

**为什么每条 message 都要写得那么啰嗦。** 读工具的结果会经 `nodes._render_payload`
渲染成「给人看的摘要」，而那个渲染器对列表只保留标题与标识字段（它是为站点列表写的）。
于是 `status` / `can_write` / `allowed_tools` 全部被丢掉，模型只看到一串名字，
接着就会回一句「返回里没有状态字段」——线上第一次冒烟就是这么失败的。
所以这里把关键字段直接写进 `message`：它不会被渲染器裁剪。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.agent import Agent, AgentStatus
from app.models.approval import RiskLevel
from app.models.audit import AuditEventType, AuditLog
from app.models.observability import ObsSpan, ObsTrace
from app.services import agent_service, skill_service
from app.services.audit_service import write_audit_event
from app.tenancy.context import tenant_scope
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)

WRITE_PERMISSION = "agent:write"

# 单次返回的条目上限。助手把这份数据组织成人话给用户看，
# 一次塞 200 条只会让回答变成一堵墙；要更多就按名字查详情。
MAX_LIST_ITEMS = 50


def _fail(message: str, **extra: Any) -> ToolExecutionResult:
    return ToolExecutionResult(
        success=False,
        message=message,
        result=extra,
        error_code="studio_rejected",
    )


def _agent_view(row: Agent) -> dict[str, Any]:
    return {
        "name": row.name,
        "display_name": row.display_name,
        "status": row.status.value,
        "description": row.description,
        "instruction": row.instruction,
        "allowed_tools": row.allowed_tools,
        "allowed_skills": row.allowed_skills,
        "allowed_connectors": row.allowed_connectors,
        "can_write": row.can_write,
        "max_delegation_depth": row.max_delegation_depth,
        "delegation_budget": row.delegation_budget,
        "is_default": row.is_default,
        "is_builtin": row.is_builtin,
        "revision": row.revision,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _person_visible_tools(context: ToolExecutionContext) -> set[str]:
    """申请人本人可见的工具集，**不含**当前 Agent 边界。

    做法是把 context 上的边界字段清空再问一次目录：`_has_access` 里的
    scope / 角色 / 租户策略照旧生效，只有 `agent_denial` 那一步被绕开。
    直接用当前 context 问会得到「studio 自己那几个工具」，校验就没有意义了。
    """
    from app.tools.dependencies import get_tool_gateway

    base = replace(
        context,
        agent_id=None,
        agent_name="",
        agent_revision=0,
        agent_allowed_tools=None,
        agent_can_write=True,
    )
    gateway = get_tool_gateway()
    return {view.tool_name for view in gateway.list_available_for_context(context=base)}


def _check_grantable(
    tools: list[str] | None,
    context: ToolExecutionContext,
) -> str | None:
    """返回拒绝理由；None 表示可以授予。"""
    if tools is None:
        return None

    privileged = sorted(name for name in tools if name.startswith(agent_service.STUDIO_TOOL_PREFIX))
    if privileged:
        return (
            f"不能把平台管理工具授予其它智能体：{'、'.join(privileged)}。"
            "那会造出一个能绕过二次确认改配置的智能体。"
        )

    unknown = sorted(set(tools) - _person_visible_tools(context))
    if unknown:
        return (
            f"这些工具你自己也没有权限，授予它也无法调用：{'、'.join(unknown)}。"
        )
    return None


# ---------------- 读 ----------------


def _tools_text(allowed: list[str] | None) -> str:
    if allowed is None:
        return "不限"
    return f"{len(allowed)} 个" if allowed else "无"


def _state_text(row: Agent) -> str:
    """一句话说清「建出来是什么样」。

    线上冒烟见过：用户说「新建只读客服智能体」，模型把「只读」理解成
    `allowed_tools=[]`，于是造出一个任何工具都调不动的空壳。审批卡片上
    只有参数，人看不出问题；返回消息里点明工具数量，模型才会说出来。
    """
    text = f"{row.status.value} · {'可写' if row.can_write else '只读'} · 工具：{_tools_text(row.allowed_tools)}"
    if row.allowed_tools == []:
        text += "（一个都调不动，只能纯对话）"
    # 只在配过的时候说：默认值不是这个智能体的特征，占一句反而稀释了真正的收窄。
    if row.max_delegation_depth is not None:
        text += (
            " · 不转交"
            if row.max_delegation_depth == 0
            else f" · 转交≤{row.max_delegation_depth}跳"
        )
    if row.delegation_budget is not None:
        text += (
            " · 本轮不转交"
            if row.delegation_budget == 0
            else f" · 每轮转交≤{row.delegation_budget}次"
        )
    return text


def _agent_line(row: Agent) -> str:
    marks = ["内置" if row.is_builtin else ("默认" if row.is_default else "")]
    marks.append("可写" if row.can_write else "只读")
    marks = [item for item in marks if item]
    return (
        f"- {row.name}（{row.display_name}）· {row.status.value} · "
        + " · ".join(marks)
        + f" · 工具：{_tools_text(row.allowed_tools)} · v{row.revision}"
    )


def _agent_lines(rows: list[Agent]) -> str:
    """把清单压成一行一个的可读文本。

    不能只列名字：读工具的渲染器会丢掉其余字段（见模块注释），
    模型据此只会回答「返回里没有状态字段」。
    """
    if not rows:
        return "当前租户还没有任何智能体。"

    lines = [f"共 {len(rows)} 个智能体："]
    lines.extend(_agent_line(row) for row in rows[:MAX_LIST_ITEMS])
    if len(rows) > MAX_LIST_ITEMS:
        lines.append(f"…另有 {len(rows) - MAX_LIST_ITEMS} 个未列出")
    return "\n".join(lines)


# ---------------- 读：运行、Trace、知识库、变更历史 ----------------
#
# 这四类数据此前只能口头指路「去观测页看」「去知识库页看」。而助手被问得最多的
# 恰恰是「它昨天报错没」「我传的那份文档进库了吗」——答不出来，人就得离开对话
# 去点页面，助手在这个场景里的价值就没了。它们全是只读，且都受租户与身份可见性
# 约束，所以不加 scope（与 `GET /agents` 一致：看得见清单才问得出问题）。

RUN_WINDOW_DAYS = 7
MAX_RUN_ITEMS = 20
EXCERPT_CHARS = 120


def _clip(text: str | None, limit: int = EXCERPT_CHARS) -> str:
    value = " ".join((text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def _is_failed_trace(row: ObsTrace) -> bool:
    """这次运行算不算失败。

    不能写成 `status != "ok"`：`awaiting_approval` 也是非 ok，但那是「在等人
    点确认」，把它算成失败会让助手对着一条正常流程说「昨天失败了 1 次」。
    """
    return row.status == "error" or bool(row.error_category)


def _when(value: datetime | None) -> str:
    """UTC 时间压成 `11-12 09:33Z`。

    带 Z 是必须的：观测存的是 UTC，而人按本地时间读，不写时区就会出现
    「明明是下午发生的，助手说上午」这种对不上的对话。
    """
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%m-%d %H:%MZ")


def _spans_text(trace: ObsTrace, spans: list[ObsSpan]) -> str:
    lines = [
        f"Trace {trace.trace_id} · {trace.agent_name or '未知智能体'} · {trace.status} · "
        f"{trace.duration_ms or 0}ms · {trace.span_count} 个 span · {_when(trace.started_at)}"
    ]
    if trace.error_category:
        lines.append(f"错误类别：{trace.error_category}")
    for span in spans:
        line = f"- #{span.seq} {span.name}（{span.kind}）· {span.status} · {span.duration_ms or 0}ms"
        if span.error_category:
            line += f" · 错误 {span.error_category}"
        if span.content:
            line += f" · {_clip(span.content)}"
        lines.append(line)
    if not spans:
        lines.append("（这次运行没有记录 span，可能观测在采集中途关闭过。）")
    return "\n".join(lines)


async def _describe_runs(
    session: AsyncSession,
    context: ToolExecutionContext,
    ref: str,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """最近若干次运行。`ref` 传智能体标识时只看它的。"""
    try:
        limit = int(arguments.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, MAX_RUN_ITEMS))

    since = datetime.now(UTC) - timedelta(days=RUN_WINDOW_DAYS)
    statement = select(ObsTrace).where(
        # 租户条件显式写出来，不依赖 RLS：行级安全是兜底，不是唯一的一道。
        ObsTrace.tenant_id == context.tenant_id,
        ObsTrace.started_at >= since,
    )
    if ref:
        statement = statement.where(ObsTrace.agent_name == ref)
    rows = list(
        (await session.scalars(statement.order_by(ObsTrace.started_at.desc()).limit(limit))).all()
    )

    scope_text = f"智能体 {ref} 的" if ref else "全部"
    if not rows:
        return ToolExecutionResult(
            success=True,
            verified=True,
            result={"resource": "run", "items": [], "window_days": RUN_WINDOW_DAYS},
            message=(
                f"近 {RUN_WINDOW_DAYS} 天没有{scope_text}运行记录。"
                "如果确实跑过，可能是观测被关掉了（OBS_ENABLED=false），"
                "或者记录已过保留期。"
            ),
            verification={"source": "obs_traces", "count": 0},
        )

    failed = [row for row in rows if _is_failed_trace(row)]
    lines = [f"近 {RUN_WINDOW_DAYS} 天{scope_text}最近 {len(rows)} 次运行："]
    for row in rows:
        line = (
            f"- {_when(row.started_at)} {row.agent_name or '未知'} · {row.status} · "
            f"{row.duration_ms or 0}ms · 工具 {row.tool_calls} 次"
        )
        if row.error_category:
            line += f" · 错误 {row.error_category}"
        if row.intent:
            line += f" · 意图 {row.intent}"
        lines.append(line)
        if _is_failed_trace(row) and row.output_text:
            lines.append(f"  结果摘要：{_clip(row.output_text)}")

    lines.append(f"其中失败 {len(failed)} 次。要看某次的分步耗时，用 resource=trace、ref=<trace_id>。")

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "resource": "run",
            "window_days": RUN_WINDOW_DAYS,
            "items": [
                {
                    "trace_id": row.trace_id,
                    "agent_name": row.agent_name,
                    "status": row.status,
                    "duration_ms": row.duration_ms,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "error_category": row.error_category,
                    "intent": row.intent,
                    "tool_calls": row.tool_calls,
                    "llm_calls": row.llm_calls,
                }
                for row in rows
            ],
            "total": len(rows),
            "failed": len(failed),
        },
        message="\n".join(lines),
        verification={"source": "obs_traces", "count": len(rows)},
    )


async def _describe_trace(
    session: AsyncSession,
    context: ToolExecutionContext,
    ref: str,
) -> ToolExecutionResult:
    trace = await session.scalar(
        select(ObsTrace).where(
            ObsTrace.tenant_id == context.tenant_id,
            ObsTrace.trace_id == ref,
        )
    )
    if trace is None:
        return _fail(f"没有找到 trace_id 为「{ref}」的运行记录。", resource="trace")

    spans = list(
        (
            await session.scalars(
                select(ObsSpan)
                .where(
                    ObsSpan.tenant_id == context.tenant_id,
                    ObsSpan.trace_id == ref,
                )
                .order_by(ObsSpan.seq, ObsSpan.started_at)
            )
        ).all()
    )

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "resource": "trace",
            "trace": {
                "trace_id": trace.trace_id,
                "agent_name": trace.agent_name,
                "status": trace.status,
                "duration_ms": trace.duration_ms,
                "error_category": trace.error_category,
                "intent": trace.intent,
                "tool_name": trace.tool_name,
                "skill_name": trace.skill_name,
                "started_at": trace.started_at.isoformat() if trace.started_at else None,
                "span_count": trace.span_count,
                "input": _clip(trace.input_text),
                "output": _clip(trace.output_text),
            },
            "spans": [
                {
                    "seq": span.seq,
                    "name": span.name,
                    "kind": span.kind,
                    "status": span.status,
                    "duration_ms": span.duration_ms,
                    "error_category": span.error_category,
                }
                for span in spans
            ],
        },
        message=_spans_text(trace, spans),
        verification={"source": "obs_spans", "count": len(spans)},
    )


async def _describe_knowledge(
    session: AsyncSession,
    context: ToolExecutionContext,
    ref: str,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    from app.knowledge import documents
    from app.knowledge.retriever import allowed_scopes_for

    try:
        limit = int(arguments.get("limit") or MAX_LIST_ITEMS)
    except (TypeError, ValueError):
        limit = MAX_LIST_ITEMS
    limit = max(1, min(limit, MAX_LIST_ITEMS))

    rows = await documents.list_documents(session, context.tenant_id, limit=200)
    # 分级可见性与 `GET /knowledge/documents` 同一口径：平台管理员看全部，
    # 其余按 scope 算。不在这里再发明一套，否则「页面上有、助手说没有」。
    visible = {"public", "internal", "confidential"} if context.is_platform_admin else allowed_scopes_for(context.scopes)
    rows = [row for row in rows if row.access_scope in visible]

    if ref:
        keyword = ref.lower()
        rows = [row for row in rows if keyword in row.title.lower() or keyword in row.logical_id.lower()]

    if not rows:
        return ToolExecutionResult(
            success=True,
            verified=True,
            result={"resource": "knowledge", "items": [], "total": 0},
            message="知识库里没有匹配的文档。" + ("" if context.is_platform_admin else "也可能是分级不够，看不到更高等级的文档。"),
            verification={"source": "documents_table", "count": 0},
        )

    lines = [f"知识库共 {len(rows)} 份文档（按入库时间倒序）："]
    lines.extend(
        f"- {row.title} · {row.access_scope} · {row.chunk_count} 片 · {row.status} · "
        f"更新于 {_when(row.updated_at)}"
        for row in rows[:limit]
    )
    if len(rows) > limit:
        lines.append(f"…另有 {len(rows) - limit} 份未列出")

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "resource": "knowledge",
            "items": [
                {
                    "id": row.id,
                    "title": row.title,
                    "filename": row.filename,
                    "access_scope": row.access_scope,
                    "chunk_count": row.chunk_count,
                    "status": row.status,
                    "source_type": row.source_type,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows[:limit]
            ],
            "total": len(rows),
        },
        message="\n".join(lines),
        verification={"source": "documents_table", "count": len(rows)},
    )


async def _boundary_history(
    session: AsyncSession,
    tenant_id: str,
    agent_id: str,
) -> list[dict[str, Any]]:
    """按版本号回放某个智能体的边界快照。

    数据来源只有审计：平台没有「边界版本表」，`revision` 只是个递增号，
    真正改成了什么是靠每次写操作留下的 `before` / `after` 记下来的。
    因此这里能恢复多远，取决于审计保留多久——恢复不到时要如实说，
    不能猜一个值填回去。
    """
    rows = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.resource_id == agent_id,
                    AuditLog.event_type.in_(
                        [AuditEventType.AGENT_CREATED, AuditEventType.AGENT_UPDATED]
                    ),
                )
                .order_by(AuditLog.created_at)
            )
        ).all()
    )

    snapshots: dict[int, dict[str, Any]] = {}
    for row in rows:
        meta = row.metadata_json or {}
        if row.event_type is AuditEventType.AGENT_CREATED:
            # 早于「创建时记 revision」的旧行没有这个字段，那时只有 v1。
            revision = int(meta.get("revision") or 1)
            boundary = {
                "allowed_tools": meta.get("allowed_tools"),
                "allowed_skills": meta.get("allowed_skills"),
                "can_write": meta.get("can_write"),
            }
        else:
            after = meta.get("after") or {}
            if not after.get("revision"):
                continue
            revision = int(after["revision"])
            boundary = {
                "allowed_tools": after.get("allowed_tools"),
                "allowed_skills": after.get("allowed_skills"),
                "can_write": after.get("can_write"),
            }

        snapshots[revision] = {
            **boundary,
            "revision": revision,
            "at": row.created_at,
            "message": row.message,
        }

    return [snapshots[revision] for revision in sorted(snapshots)]


def _boundary_text(item: dict[str, Any]) -> str:
    tools = item.get("allowed_tools")
    tools_text = "不限" if tools is None else f"{len(tools)} 个"
    skills = item.get("allowed_skills")
    skills_text = "不限" if skills is None else f"{len(skills)} 个"
    can_write = "可写" if item.get("can_write") else "只读"
    return f"{can_write} · 工具 {tools_text} · 技能 {skills_text}"


async def _describe_history(
    session: AsyncSession,
    context: ToolExecutionContext,
    ref: str,
) -> ToolExecutionResult:
    if not ref:
        return _fail("查变更历史需要指定智能体标识（ref）。", resource="history")

    try:
        agent = await agent_service.get_agent(session, ref=ref)
    except agent_service.AgentNotFound:
        return _fail(f"没有找到标识为「{ref}」的智能体。", resource="history")

    history = await _boundary_history(session, context.tenant_id, str(agent.id))
    if not history:
        return ToolExecutionResult(
            success=True,
            verified=True,
            result={"resource": "history", "agent": agent.name, "items": []},
            message=(
                f"「{agent.display_name}」没有可回放的边界变更记录（当前第 {agent.revision} 版）。"
                "审计里没有找到它的创建或修改事件，可能早于审计上线。"
            ),
            verification={"source": "audit_logs", "count": 0},
        )

    lines = [f"「{agent.display_name}」的边界版本（当前第 {agent.revision} 版）："]
    for item in history:
        mark = " ← 当前" if item["revision"] == agent.revision else ""
        lines.append(
            f"- v{item['revision']} · {_boundary_text(item)} · 记于 {_when(item['at'])}{mark}"
        )

    current = next((item for item in history if item["revision"] == agent.revision), None)
    if current is None:
        lines.append("注意：当前版本不在审计里（可能是审计清理过），只能恢复到上面列出的版本。")
    lines.append("要回到某一版，用 studio_agent_restore 指定 revision；只恢复边界字段，不动名称与状态。")

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "resource": "history",
            "agent": agent.name,
            "current_revision": agent.revision,
            "items": [
                {
                    "revision": item["revision"],
                    "allowed_tools": item["allowed_tools"],
                    "allowed_skills": item["allowed_skills"],
                    "can_write": item["can_write"],
                    "at": item["at"].isoformat() if item["at"] else None,
                    "message": item["message"],
                }
                for item in history
            ],
        },
        message="\n".join(lines),
        verification={"source": "audit_logs", "count": len(history)},
    )


async def studio_describe(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """聚合读：智能体、技能、工具、运行、Trace、知识库、变更历史，或平台用法摘要。

    `query` 只提示「想重点看哪部分」，**不参与检索**：让它参与就会出现
    「它说没有这个 Agent，其实只是关键词没匹配上」这种最难解释的错。
    """
    resource = str(arguments.get("resource") or "agent")
    ref = str(arguments.get("ref") or "").strip()

    if resource == "platform":
        return ToolExecutionResult(
            success=True,
            verified=True,
            result={"resource": "platform", "guide": _PLATFORM_GUIDE},
            message="平台用法摘要。",
            verification={"source": "builtin_guide"},
        )

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            if resource == "agent":
                if ref:
                    try:
                        row = await agent_service.get_agent(session, ref=ref)
                    except agent_service.AgentNotFound:
                        return _fail(f"没有找到标识为「{ref}」的智能体。", resource="agent")
                    return ToolExecutionResult(
                        success=True,
                        verified=True,
                        result={"resource": "agent", "agent": _agent_view(row)},
                        message=(
                            f"{row.name}（{row.display_name}）· {row.status.value} · "
                            + ("可写" if row.can_write else "只读")
                            + f" · 工具：{_tools_text(row.allowed_tools)} · v{row.revision}"
                            + (f"\n职责：{row.description}" if row.description else "")
                        ),
                        verification={"source": "agents_table", "ref": row.name},
                    )

                rows = list(await agent_service.list_agents(session))
                items = [_agent_view(row) for row in rows[:MAX_LIST_ITEMS]]
                return ToolExecutionResult(
                    success=True,
                    verified=True,
                    result={
                        "resource": "agent",
                        "items": items,
                        "total": len(rows),
                        "truncated": len(rows) > MAX_LIST_ITEMS,
                    },
                    message=_agent_lines(list(rows)),
                    verification={"source": "agents_table", "count": len(rows)},
                )

            if resource == "skill":
                if ref:
                    row = await skill_service.get_by_name(session, ref)
                    if row is None:
                        return _fail(f"没有找到名为「{ref}」的技能。", resource="skill")
                    return ToolExecutionResult(
                        success=True,
                        verified=True,
                        result={
                            "resource": "skill",
                            "skill": {
                                "name": row.name,
                                "display_name": row.display_name,
                                "description": row.description,
                                "instruction": row.instruction,
                                "enabled": row.enabled,
                                "allowed_tools": row.allowed_tools,
                                "trigger_keywords": row.trigger_keywords,
                                "priority": row.priority,
                            },
                        },
                        message="已读取技能配置。",
                        verification={"source": "skills_table", "ref": row.name},
                    )

                rows = await skill_service.list_skills(session)
                items = [
                    {
                        "name": row.name,
                        "display_name": row.display_name,
                        "description": row.description,
                        "enabled": row.enabled,
                        "allowed_tools": row.allowed_tools,
                    }
                    for row in rows[:MAX_LIST_ITEMS]
                ]
                skill_lines = [f"共 {len(rows)} 个技能："]
                skill_lines.extend(
                    f"- {row.name}（{row.display_name}）· "
                    + ("启用" if row.enabled else "停用")
                    + (f" · {row.description}" if row.description else "")
                    for row in rows[:MAX_LIST_ITEMS]
                )
                return ToolExecutionResult(
                    success=True,
                    verified=True,
                    result={
                        "resource": "skill",
                        "items": items,
                        "total": len(rows),
                        "truncated": len(rows) > MAX_LIST_ITEMS,
                    },
                    message="\n".join(skill_lines) if rows else "当前租户还没有任何技能。",
                    verification={"source": "skills_table", "count": len(rows)},
                )

            if resource == "run":
                return await _describe_runs(session, context, ref, arguments)

            if resource == "trace":
                if not ref:
                    return _fail("查 Trace 需要给出 trace_id（ref）。", resource="trace")
                return await _describe_trace(session, context, ref)

            if resource == "knowledge":
                return await _describe_knowledge(session, context, ref, arguments)

            if resource == "history":
                return await _describe_history(session, context, ref)

    if resource == "tool":
        from app.tools.dependencies import get_tool_gateway

        gateway = get_tool_gateway()
        visible = set(_person_visible_tools(context))
        items = [
            {
                "name": tool.name,
                "display_name": tool.display_name,
                "risk_level": tool.risk_level.value,
                "requires_approval": tool.requires_approval,
                "source": tool.source,
            }
            for tool in gateway.list_tools(include_disabled=True)
            if tool.name in visible
        ]
        items.sort(key=lambda item: (item["source"], item["name"]))
        tool_lines = [f"你有权使用 {len(items)} 个工具："]
        tool_lines.extend(
            f"- {item['name']}（{item['display_name']}）· {item['risk_level']} · {item['source']}"
            for item in items[:MAX_LIST_ITEMS]
        )
        return ToolExecutionResult(
            success=True,
            verified=True,
            result={
                "resource": "tool",
                "items": items[:MAX_LIST_ITEMS],
                "total": len(items),
                "truncated": len(items) > MAX_LIST_ITEMS,
            },
            message="\n".join(tool_lines),
            verification={"source": "tool_registry", "count": len(items)},
        )

    return _fail(
        f"不支持的查询对象「{resource}」。可以查 agent、skill、tool、run、trace、"
        "knowledge、history 或 platform。",
        resource=resource,
    )


# ---------------- 写 ----------------


async def _audit_mutation(
    session: AsyncSession,
    context: ToolExecutionContext,
    *,
    tool_name: str,
    event_type: AuditEventType,
    resource_id: str,
    message: str,
    metadata: dict[str, Any],
    resource_type: str = "agent",
) -> None:
    """给写操作补一条**资源级**审计，事件类型与 REST 路由完全一致。

    网关已经为每次工具调用记了一条 `tool_succeeded`，但那条的 resource 是工具
    本身（`tool/studio_agent_save`）：审计页上只能读到「某个工具跑过」，读不出
    「改的是哪个智能体、改成了什么」。构建助手是唯一能用自然语言改配置的入口，
    用户甚至看不到它的会话（对话台只列 `channel=api`），审计就是唯一的追溯来源，
    所以这里按 `agent_created` / `agent_updated` / `agent_status_changed` /
    `agent_deleted` 再记一条，resource 是那个智能体本身。

    两条事件的 `approval_id` 相同，于是「人点的确认」与「助手做的变更」能对上：
    只有一条 `tool_succeeded` 而没有资源事件时，说明那次调用没真正落地。

    **审计失败不回滚业务。** 调用方在进这里之前已经 commit，变更已经生效；
    这里失败只留日志，不能反过来把生效的变更说成没发生——那才是更坏的结果。
    """
    try:
        await write_audit_event(
            session,
            event_type=event_type,
            tenant_id=context.tenant_id,
            actor_id=context.user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            thread_id=context.thread_id,
            run_id=context.run_id,
            message=message,
            metadata={
                # 区分「控制台点的」与「对话里让助手做的」，两者事件类型相同。
                "via": "studio_assistant",
                "tool_name": tool_name,
                "agent_name": context.agent_name,
                "agent_revision": context.agent_revision,
                "approval_id": context.approval_id,
                **metadata,
            },
        )
        await session.commit()
    except Exception:
        logger.exception(
            "构建助手的写操作审计写入失败。",
            extra={"event": "studio_audit_failed", "resource_id": resource_id},
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - 连接已断时 rollback 也会抛
            logger.exception("审计失败后回滚会话也失败。", extra={"event": "studio_audit_rollback_failed"})


async def studio_agent_save(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """新建或修改一个智能体。

    `mode` 必填，不做「存在就改、不存在就建」的自动判断：名字打错一个字符时，
    自动判断会静默建出一个新 Agent，而审批卡片上只写着「新建 ops2」——
    人要能一眼看出这是新建还是修改，否则二次确认就白做了。
    """
    mode = str(arguments.get("mode") or "").strip()
    if mode not in {"create", "update"}:
        return _fail("mode 必须是 create 或 update。")

    name = str(arguments.get("name") or "").strip()
    if not name:
        return _fail("需要指出要操作的智能体标识（name）。")

    sent_tools = "allowed_tools" in arguments
    sent_skills = "allowed_skills" in arguments
    sent_connectors = "allowed_connectors" in arguments
    # 预算同样要区分「没传」与「传了 null」：后者是回到平台默认，0 是「不许转交」。
    sent_depth = "max_delegation_depth" in arguments
    sent_budget = "delegation_budget" in arguments
    tools = arguments.get("allowed_tools")
    skills = arguments.get("allowed_skills")
    connectors = arguments.get("allowed_connectors")

    if sent_tools:
        reason = _check_grantable(tools, context)
        if reason:
            return _fail(reason)

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            existing = await session.scalar(select(Agent).where(Agent.name == name))

            if mode == "create":
                if existing is not None:
                    return _fail(f"标识「{name}」已被占用，要修改它请用 mode=update。")

                try:
                    row = await agent_service.create_agent(
                        session,
                        tenant_id=context.tenant_id,
                        name=name,
                        display_name=str(arguments.get("display_name") or name),
                        description=str(arguments.get("description") or ""),
                        instruction=str(arguments.get("instruction") or ""),
                        allowed_tools=tools if sent_tools else None,
                        allowed_skills=skills if sent_skills else None,
                        allowed_connectors=connectors if sent_connectors else None,
                        can_write=bool(arguments.get("can_write", False)),
                        max_delegation_depth=arguments.get("max_delegation_depth"),
                        delegation_budget=arguments.get("delegation_budget"),
                        status=AgentStatus(str(arguments.get("status") or AgentStatus.DRAFT.value)),
                        actor_user_id=context.user_id,
                    )
                except agent_service.AgentValidationError as exc:
                    return _fail(str(exc))

                await session.commit()
                await session.refresh(row)

                agent_id = str(row.id)
                view = _agent_view(row)
                # 先把返回值全部构造好，再写审计：审计失败会 rollback，
                # 之后再读 ORM 属性会触发隐式 IO（async 会话里直接 MissingGreenlet），
                # 一次成功的创建就会被报成失败。
                result = ToolExecutionResult(
                    success=True,
                    verified=True,
                    result={"agent": view},
                    message=f"已新建智能体「{row.display_name}」：{_state_text(row)}。",
                    verification={"source": "agents_table", "ref": view["name"], "revision": view["revision"]},
                )

                await _audit_mutation(
                    session,
                    context,
                    tool_name="studio_agent_save",
                    event_type=AuditEventType.AGENT_CREATED,
                    resource_id=agent_id,
                    message=(
                        f"构建助手新建智能体「{view['display_name']}」（{view['name']}），"
                        f"状态 {view['status']}、{'可写' if view['can_write'] else '只读'}"
                    ),
                    metadata={
                        "name": view["name"],
                        "status": view["status"],
                        "can_write": view["can_write"],
                        "allowed_tools": view["allowed_tools"],
                        "allowed_skills": view["allowed_skills"],
                        # 记版本号是为了让「恢复到第 N 版」能找回 v1 的边界：
                        # 创建事件是 v1 唯一的快照来源，缺了它就只能恢复到 v2 以后。
                        "revision": view["revision"],
                    },
                )
                return result

            if existing is None:
                return _fail(f"没有找到标识为「{name}」的智能体，要新建请用 mode=create。")

            if existing.is_builtin:
                return _fail("内置智能体的能力边界不可修改。")

            # 改之前的边界，写审计要用它算差异。与 REST 路由同一套口径：
            # 只有 revision 变了才算「边界变了」，反复保存同一份配置不该记一条。
            before = {
                "allowed_tools": existing.allowed_tools,
                "allowed_skills": existing.allowed_skills,
                "allowed_connectors": existing.allowed_connectors,
                "can_write": existing.can_write,
                "revision": existing.revision,
            }

            try:
                row = await agent_service.update_agent(
                    session,
                    agent=existing,
                    display_name=arguments.get("display_name"),
                    description=arguments.get("description"),
                    instruction=arguments.get("instruction"),
                    allowed_tools=tools if sent_tools else agent_service.UNSET,
                    allowed_skills=skills if sent_skills else agent_service.UNSET,
                    allowed_connectors=(
                        connectors if sent_connectors else agent_service.UNSET
                    ),
                    can_write=arguments.get("can_write"),
                    max_delegation_depth=(
                        arguments.get("max_delegation_depth")
                        if sent_depth
                        else agent_service.UNSET
                    ),
                    delegation_budget=(
                        arguments.get("delegation_budget")
                        if sent_budget
                        else agent_service.UNSET
                    ),
                    actor_user_id=context.user_id,
                )
            except agent_service.AgentValidationError as exc:
                return _fail(str(exc))

            await session.commit()
            await session.refresh(row)

            # 回读一次再报「已核验」：commit 成功不等于目标状态就是想要的，
            # 尤其是并发下另一个人可能刚把它改回去。
            fresh = await session.scalar(select(Agent).where(Agent.name == row.name))
            view = _agent_view(fresh or row)
            agent_id = str((fresh or row).id)
            result = ToolExecutionResult(
                success=True,
                verified=True,
                result={"agent": view},
                message=(
                    f"已更新智能体「{view['display_name']}」：{_state_text(fresh or row)}"
                    f"（第 {view['revision']} 版边界）。"
                ),
                verification={
                    "source": "agents_table",
                    "ref": view["name"],
                    "revision": view["revision"],
                },
            )

            if view["revision"] != before["revision"]:
                await _audit_mutation(
                    session,
                    context,
                    tool_name="studio_agent_save",
                    event_type=AuditEventType.AGENT_UPDATED,
                    resource_id=agent_id,
                    message=(
                        f"构建助手修改智能体「{view['display_name']}」（{view['name']}）"
                        f"的边界：第 {before['revision']} 版 → 第 {view['revision']} 版"
                    ),
                    metadata={
                        "name": view["name"],
                        "revision": view["revision"],
                        # 只记边界字段与版本号，不记指令正文：可能很长，也可能含业务细节。
                        "before": before,
                        "after": {
                            "allowed_tools": view["allowed_tools"],
                            "allowed_skills": view["allowed_skills"],
                            "can_write": view["can_write"],
                            "revision": view["revision"],
                        },
                    },
                )

            return result


async def studio_agent_set_status(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """启用或停用。与保存分开，因为它不该让 `revision` 变化。"""
    name = str(arguments.get("name") or "").strip()
    raw = str(arguments.get("status") or "").strip()

    if not name:
        return _fail("需要指出要操作的智能体标识（name）。")

    try:
        target = AgentStatus(raw)
    except ValueError:
        return _fail("status 只能是 draft、active 或 disabled。")

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            row = await session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                return _fail(f"没有找到标识为「{name}」的智能体。")

            previous = row.status.value

            try:
                await agent_service.set_status(
                    session,
                    agent=row,
                    status=target,
                    actor_user_id=context.user_id,
                )
            except agent_service.AgentValidationError as exc:
                return _fail(str(exc))

            await session.commit()
            await session.refresh(row)

            agent_id = str(row.id)
            view = _agent_view(row)
            result = ToolExecutionResult(
                success=True,
                verified=True,
                result={"agent": view},
                message=f"已把「{view['display_name']}」设为 {view['status']}。",
                verification={"source": "agents_table", "ref": view["name"], "status": view["status"]},
            )

            if view["status"] != previous:
                await _audit_mutation(
                    session,
                    context,
                    tool_name="studio_agent_set_status",
                    event_type=AuditEventType.AGENT_STATUS_CHANGED,
                    resource_id=agent_id,
                    message=(
                        f"构建助手把智能体「{view['display_name']}」（{view['name']}）"
                        f"从 {previous} 改为 {view['status']}"
                    ),
                    metadata={"name": view["name"], "from": previous, "to": view["status"]},
                )

            return result


# ---------------- 审批前预检 ----------------


async def _load_by_name(context: ToolExecutionContext, name: str) -> Agent | None:
    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            return await session.scalar(select(Agent).where(Agent.name == name))


async def _preflight_mutation(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> str | None:
    """修改类操作在弹确认卡之前先看一眼目标。

    内置智能体是平台自带的构建助手，边界写死在代码里。不在这里拦的话，
    流程会变成：弹一张「删除 studio」的确认卡 → 人点了确认 → 报错说不能删。
    那张卡本身就是错的：它让人以为这件事有可能成功。
    """
    name = str(arguments.get("name") or "").strip()
    if not name:
        # 名字缺失交给 handler 报具体错误，这里不抢话。
        return None

    row = await _load_by_name(context, name)
    if row is not None and row.is_builtin:
        return "内置智能体是平台自带的构建助手，能力边界固定在平台上，不能修改、停用或删除。"
    return None


async def _preflight_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> str | None:
    """删除比修改多一道：必须先停用。

    先停用再删除不是形式主义——直接删掉一个可能在服务中的智能体，
    历史会话会指向一个不存在的身份，排查时找不到对手是谁。
    """
    reason = await _preflight_mutation(context, arguments)
    if reason:
        return reason

    name = str(arguments.get("name") or "").strip()
    if not name:
        return None

    row = await _load_by_name(context, name)
    if row is not None and row.status is not AgentStatus.DISABLED:
        return f"要删除「{row.display_name}」请先把它停用（当前状态：{row.status.value}）。"
    return None


async def studio_agent_delete(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """删除。

    `confirm_name` 必须在工具内部与目标名逐一比对——**不能靠模型自觉**。
    模型完全可能热心地替用户把名字填好，那样这道闸门等于不存在。
    """
    name = str(arguments.get("name") or "").strip()
    confirm = str(arguments.get("confirm_name") or "").strip()

    if not name:
        return _fail("需要指出要删除的智能体标识（name）。")

    if confirm != name:
        return _fail(
            f"删除需要再次确认名称：请让用户回复完整的标识「{name}」后再试一次。"
        )

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            row = await session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                return _fail(f"没有找到标识为「{name}」的智能体。")

            display = row.display_name
            # 删掉之后这一行就没了，id 要在删除前取：审计的 resource_id 得能
            # 对上曾经存在过的那一行，否则「删了哪个」只能靠名字猜。
            agent_id = str(row.id)
            try:
                await agent_service.delete_agent(session, agent=row)
            except agent_service.AgentValidationError as exc:
                return _fail(str(exc))

            await session.commit()

            gone = await session.scalar(select(Agent).where(Agent.name == name))
            if gone is not None:
                return ToolExecutionResult(
                    success=False,
                    message="删除未生效，请稍后重试或到「智能体」页处理。",
                    error_code="studio_delete_unverified",
                )

            logger.info(
                "构建助手删除了智能体。",
                extra={"event": "studio_agent_deleted", "agent": name},
            )
            result = ToolExecutionResult(
                success=True,
                verified=True,
                result={"deleted": name, "display_name": display},
                message=f"已删除智能体「{display}」。",
                verification={"source": "agents_table", "absent": name},
            )

            # 只有回读确认「真的没了」才记删除审计：写出去了但还在的记录，
            # 审计里应该留的是网关那条 tool_failed，不是一条「已删除」。
            await _audit_mutation(
                session,
                context,
                tool_name="studio_agent_delete",
                event_type=AuditEventType.AGENT_DELETED,
                resource_id=agent_id,
                message=f"构建助手删除智能体「{display}」（{name}）",
                metadata={"name": name, "display_name": display},
            )
            return result


async def _preflight_restore(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> str | None:
    """恢复前先确认「这一版真的能找回来」。

    与删除同一个道理：一张注定失败的确认卡不只是没用，它会训练人闭眼点确认。
    审计有保留期，越老的版本越可能已经被清掉，所以在弹卡之前就查。
    """
    name = str(arguments.get("name") or "").strip()
    if not name:
        return None

    try:
        revision = int(arguments.get("revision"))
    except (TypeError, ValueError):
        # 版本号不合法交给 handler 报具体错误，这里不抢话。
        return None

    row = await _load_by_name(context, name)
    if row is None:
        return f"没有找到标识为「{name}」的智能体。"

    if row.is_builtin:
        return "内置智能体是平台自带的构建助手，能力边界固定在平台上，不能修改、停用或删除。"

    if row.revision == revision:
        return f"「{row.display_name}」现在就是第 {revision} 版，不需要恢复。"

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            history = await _boundary_history(session, context.tenant_id, str(row.id))

    if not any(item["revision"] == revision for item in history):
        known = "、".join(f"v{item['revision']}" for item in history) or "无"
        return (
            f"审计里没有「{row.display_name}」第 {revision} 版的边界快照，无法恢复。"
            f"现在能回放的版本：{known}。"
        )

    return None


async def studio_agent_restore(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """把边界恢复到历史某一版。

    只恢复三个边界字段（工具、技能、可写），不动显示名、职责、补充指令与状态：
    「配置改坏了」通常坏在边界上，而名字与状态是另一类意图，混在一次操作里
    会让审批卡读不出到底会发生什么。

    快照来自审计的 `before` / `after`，所以能恢复多远取决于审计保留多久——
    查不到就明确说查不到，不拿近似的版本凑数。
    """
    name = str(arguments.get("name") or "").strip()
    if not name:
        return _fail("需要指出要恢复的智能体标识（name）。")

    try:
        revision = int(arguments.get("revision"))
    except (TypeError, ValueError):
        return _fail("需要给出要恢复到的边界版本号（revision，整数）。")

    with tenant_scope(context.tenant_id):
        async with async_session_factory() as session:
            row = await session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                return _fail(f"没有找到标识为「{name}」的智能体。")

            if row.is_builtin:
                return _fail("内置智能体的能力边界不可修改。")

            if row.revision == revision:
                return _fail(f"「{row.display_name}」现在就是第 {revision} 版，不需要恢复。")

            history = await _boundary_history(session, context.tenant_id, str(row.id))
            snapshot = next((item for item in history if item["revision"] == revision), None)
            if snapshot is None:
                known = "、".join(f"v{item['revision']}" for item in history) or "无"
                return _fail(
                    f"审计里没有第 {revision} 版的边界快照，无法恢复。"
                    f"现在能回放的版本：{known}。"
                )

            # 与保存同一条可用性口径：恢复回来的工具中如果有申请人现在也看不见的，
            # 恢复成功也调不动，属于「配好了但用不了」，必须在保存时暴露。
            reason = _check_grantable(snapshot["allowed_tools"], context)
            if reason:
                return _fail(f"第 {revision} 版里有现在不可用的工具，未执行恢复。{reason}")

            before = {
                "allowed_tools": row.allowed_tools,
                "allowed_skills": row.allowed_skills,
                "can_write": row.can_write,
                "revision": row.revision,
            }

            try:
                row = await agent_service.update_agent(
                    session,
                    agent=row,
                    allowed_tools=snapshot["allowed_tools"],
                    allowed_skills=snapshot["allowed_skills"],
                    can_write=snapshot["can_write"],
                    actor_user_id=context.user_id,
                )
            except agent_service.AgentValidationError as exc:
                return _fail(str(exc))

            await session.commit()
            await session.refresh(row)

            fresh = await session.scalar(select(Agent).where(Agent.name == name))
            view = _agent_view(fresh or row)
            agent_id = str((fresh or row).id)
            restored = _boundary_text(snapshot)
            result = ToolExecutionResult(
                success=True,
                verified=True,
                result={"agent": view, "restored_from": revision},
                message=(
                    f"已把「{view['display_name']}」的边界恢复到第 {revision} 版：{restored}。"
                    f"恢复本身是一次新的修改，所以版本号变成第 {view['revision']} 版。"
                ),
                verification={
                    "source": "agents_table",
                    "ref": view["name"],
                    "revision": view["revision"],
                    "restored_from": revision,
                },
            )

            await _audit_mutation(
                session,
                context,
                tool_name="studio_agent_restore",
                event_type=AuditEventType.AGENT_UPDATED,
                resource_id=agent_id,
                message=(
                    f"构建助手把智能体「{view['display_name']}」（{view['name']}）"
                    f"的边界恢复到第 {revision} 版：第 {before['revision']} 版 → 第 {view['revision']} 版"
                ),
                metadata={
                    "name": view["name"],
                    "revision": view["revision"],
                    "restored_from": revision,
                    "before": before,
                    "after": {
                        "allowed_tools": view["allowed_tools"],
                        "allowed_skills": view["allowed_skills"],
                        "can_write": view["can_write"],
                        "revision": view["revision"],
                    },
                },
            )
            return result


_PLATFORM_GUIDE = {
    "agent_boundary": (
        "智能体的边界只会收窄：有效权限 = 人的权限 ∩ 智能体边界。"
        "allowed_tools 为空表示不额外收窄，空数组表示一个都不给；"
        "can_write 关闭后写类工具连目录都不出现，打开也不是审批豁免。"
    ),
    "approval": (
        "写类工具一律先出预览、等人确认，再执行。审批单会冻结当时的边界，"
        "执行时按「审批时快照 ∩ 当前边界」判定：期间收窄立即生效，放宽不生效。"
    ),
    "observability": (
        "控制台「观测」页按智能体给出轮次、错误率、被拒次数、耗时与成本；"
        "被拒次数来自审计而不是调用链，因为拒绝发生在执行之前。"
    ),
    "lifecycle": "新建的智能体默认是草稿且只读，启用后才能被对话选中；删除要求先停用。",
    "credentials": (
        "本助手不接收、不读取、也不转存任何密钥：Token、密码、API Key、Cookie、"
        "Authorization 头一律不由对话传递，助手的写工具收到这类字段会直接拒绝。"
        "需要鉴权的 HTTP / MCP 端点，请到控制台对应的配置页用安全输入填写，"
        "或改用租户凭据引用（credential_ref）。"
        "用户如果把密钥贴进对话，要提醒他去平台改，并当作已泄露处理。"
    ),
    "restore": (
        "平台没有「边界版本表」，每个版本的边界是靠审计里的 before/after 记下来的，"
        "所以能恢复到多远取决于审计保留期。恢复只覆盖边界字段，会生成一个新版本。"
    ),
    "runtime": (
        "运行状态与日志来自观测（obs_traces / obs_spans），默认只看近 7 天；"
        "观测为空可能是被 OBS_ENABLED 关掉或已过保留期，不代表没跑过。"
    ),
    "out_of_scope": (
        "平台不提供：改平台代码、直接读写数据库、执行系统命令、获取主机权限。"
        "这些走部署与运维流程。"
    ),
}


def build_studio_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="studio_describe",
            display_name="查看平台配置",
            description=(
                "查看平台配置与运行情况。resource 取："
                "agent（智能体，带 ref 看单个）/ skill（技能）/ tool（可用工具）/ "
                "run（最近运行，带 ref 按智能体过滤）/ trace（单次运行的 span，ref 传 trace_id）/ "
                "knowledge（知识库文档清单）/ history（某个智能体的边界版本，ref 必填）/ "
                "platform（平台用法摘要）。"
            ),
            kind="python",
            risk_level=RiskLevel.READ,
            # 读不加 scope：查看智能体清单对所有成员开放（与 GET /agents 一致），
            # 否则普通成员连「有哪些角色可选」都问不出来。
            required_scopes=(),
            input_schema={
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "enum": [
                            "agent",
                            "skill",
                            "tool",
                            "run",
                            "trace",
                            "knowledge",
                            "history",
                            "platform",
                        ],
                    },
                    "ref": {"type": "string", "maxLength": 128},
                    "query": {"type": "string", "maxLength": 200},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["resource"],
                "additionalProperties": False,
            },
            handler=studio_describe,
            verification_hint="返回库里的实际配置，不是推测。",
        ),
        ToolDefinition(
            name="studio_agent_save",
            display_name="新建或修改智能体",
            description=(
                "新建或修改一个智能体。mode=create 时新建（默认草稿且只读），"
                "mode=update 时只改传入的字段。"
                "allowed_tools 不传 = 不额外限制（该智能体按平台基线可用工具）；"
                "传 null 表示取消已有收窄；传空数组表示一个工具都不给（纯对话）。"
                "**「只读」不等于「不给工具」**：只读请设 can_write=false，"
                "并且不要传 allowed_tools，否则它会一个工具都调不动。"
                "用户没点名要哪些工具时，不要传 allowed_tools。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(WRITE_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["create", "update"]},
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "display_name": {"type": "string", "maxLength": 64},
                    "description": {"type": "string", "maxLength": 512},
                    "instruction": {"type": "string", "maxLength": 4000},
                    "allowed_tools": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "maxItems": 64,
                    },
                    "allowed_connectors": {
                        "type": ["array", "null"],
                        "items": {"type": "string", "maxLength": 64},
                        "maxItems": 32,
                    },
                    "allowed_skills": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "maxItems": 64,
                    },
                    "can_write": {"type": "boolean"},
                    "max_delegation_depth": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 5,
                    },
                    "delegation_budget": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "status": {"type": "string", "enum": ["draft", "active", "disabled"]},
                },
                "required": ["mode", "name"],
                "additionalProperties": False,
            },
            handler=studio_agent_save,
            preflight=_preflight_mutation,
            verification_hint="保存后回读并给出新的边界版本号。",
        ),
        ToolDefinition(
            name="studio_agent_set_status",
            display_name="启用或停用智能体",
            description="把智能体设为 draft / active / disabled。不改变边界版本号。",
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(WRITE_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "status": {"type": "string", "enum": ["draft", "active", "disabled"]},
                },
                "required": ["name", "status"],
                "additionalProperties": False,
            },
            handler=studio_agent_set_status,
            preflight=_preflight_mutation,
            verification_hint="返回目标智能体的实际状态。",
        ),
        ToolDefinition(
            name="studio_agent_delete",
            display_name="删除智能体",
            description=(
                "删除一个已停用的智能体。必须同时传入 confirm_name，且与 name 完全一致；"
                "不一致会被拒绝，用来防止删错对象。"
            ),
            kind="python",
            risk_level=RiskLevel.DESTRUCTIVE,
            required_scopes=(WRITE_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "confirm_name": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "required": ["name", "confirm_name"],
                "additionalProperties": False,
            },
            handler=studio_agent_delete,
            preflight=_preflight_delete,
            verification_hint="删除后回读，确认已不存在。",
        ),
        ToolDefinition(
            name="studio_agent_restore",
            display_name="把边界恢复到历史版本",
            description=(
                "把智能体的边界（可用工具 / 可用技能 / 是否可写）恢复到历史某一版。"
                "版本号用 studio_describe resource=history 查到；"
                "只恢复边界字段，不动显示名、职责、补充指令与启停状态。"
                "恢复会生成一个新版本，原版本号不会「变回去」。"
            ),
            kind="python",
            risk_level=RiskLevel.WRITE,
            required_scopes=(WRITE_PERMISSION,),
            requires_approval=True,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "revision": {"type": "integer", "minimum": 1},
                },
                "required": ["name", "revision"],
                "additionalProperties": False,
            },
            handler=studio_agent_restore,
            preflight=_preflight_restore,
            verification_hint="恢复后回读版本号与边界，确认与目标版本一致。",
        ),
    ]
