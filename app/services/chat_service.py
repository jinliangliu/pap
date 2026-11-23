# app/services/chat_service.py
"""对话服务：负责持久化与调用执行图，不承担业务权限判断。

多租户相关的三件事都在这里收口：

- 内部 thread_id 由 (租户, 用户, 客户端 ID) 拼成，客户端无法指定别人的会话；
- 每日对话配额按租户扣减，单租户刷量不会拖垮其他租户；
- 所有数据库动作都显式进入租户上下文，写入自动带 tenant_id。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlalchemy import func, select

from app.agents.graph import get_agent_graph
from app.agents.interrupts import extract_interrupt
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.checkpoint import discard_thread
from app.db.session import async_session_factory
from app.models.conversation import Conversation, Message, MessageRole, RunStatus
from app.observability.callbacks import take_usage_for_run
from app.observability.context import span, trace_run
from app.orchestration import engine
from app.schemas.chat import (
    ApprovalPendingResponse,
    ChatResponse,
)
from app.services import (
    agent_service,
    attachment_store,
    context_service,
    memory_extract,
    workflow_service,
)
from app.services.agent_invoker import context_from_state
from app.services.agent_service import AgentBoundary
from app.services.rate_limit_service import RateLimitExceeded, TenantQuotaExceeded
from app.services.run_service import (
    add_message,
    ensure_conversation,
    finish_run,
    start_run,
)
from app.services.tenant_service import get_tenant_service
from app.tenancy.context import tenant_scope
from app.tenancy.threads import internal_thread_id, validate_client_thread_id

logger = logging.getLogger(__name__)

# 与 app.tools.ocr_tools.OBSERVATION_TOOL_NAMES 一致。
# 不直接导入是为了避免 services → tools 的导入环；
# 旧名 ocr_image 保留，历史会话的观察记录里还是这个名字。
OCR_OBSERVATION_TOOL_NAMES = frozenset({"ocr_document", "ocr_image"})

# 会话的 channel：表示「从哪个入口开始的」。
CHANNEL_API = "api"
CHANNEL_STUDIO = "studio"


def _channel_of(boundary: AgentBoundary | None) -> str:
    """这条会话算哪个入口的。

    构建助手在控制台里有自己的面板，它开出来的会话不该混进对话台的列表，
    所以单独记一个 channel。按「由哪个 Agent 应答」判断而不是让客户端报一个
    字段：客户端随便填的话，这个筛选值就变成了用户自己贴的标签。

    目前内置的只有构建助手一个；以后再多一个内置 Agent，这里要改成按名字映射，
    而不是继续用「是不是助手」这一个布尔。
    """
    if boundary is not None and agent_service.is_studio_agent_name(boundary.agent_name):
        return CHANNEL_STUDIO
    return CHANNEL_API


# 「本会话的材料」列进上下文的上限。
# 材料清单是给模型看的选项，不是审计账本：会话里传过几十份文件之后，
# 全列出来只会挤占上下文，而真正要处理的那份几乎总是最近的。
_MAX_MATERIALS_IN_PROMPT = 20

# 平台自己执行的斜杠指令（另一个是 `/workflow`，见 `workflow_service`）。
#
# **必须由平台执行，不能交给模型。** 模型会很配合地回一句「收到——上下文已清空」，
# 然后下一轮照样记得全部历史：它没有「清空」这个动作可做，只是在说一件
# 听起来对的话。承诺了没做的事比报错更难排查——用户要等到下一轮它又提起
# 上周那件事，才发现那句「已清空」是假的。
CLEAR_COMMAND = "/clear"


def is_clear_command(text: str) -> bool:
    """`/clear` 是否独占这一整条消息。

    只认独占：`/clear 顺便看下 xxx` 不是清空，是一句话里带着这个词。
    判定放宽到「斜杠 + 干净的命令词」，因为用户不会记大小写和尾随空格；
    但**不**做前缀匹配——`/clearall`、`/cleared` 都不该清空一个会话。
    """
    value = (text or "").strip()
    if not value.startswith("/"):
        return False
    return value[1:].strip().lower() == CLEAR_COMMAND[1:]


def _clear_reply(cleared_turns: int) -> str:
    """`/clear` 的回复。

    由平台生成，不经模型：这段文字要描述**平台做过什么**，而模型并不知道
    水位写没写、摘要作不作废。它照着写一句「已清空」，就成了一句无法核实的承诺。

    三句话分别对应三件容易被误解的事：历史还在（但不再参与）、摘要也作废了
    （不只是消息）、附件仍在（只是不再自动带上）。
    """
    if cleared_turns <= 0:
        return (
            "上下文已清空。\n\n"
            "这个会话此前没有可清的内容——这一轮不会带上任何历史。"
        )

    return (
        "上下文已清空。\n\n"
        f"此前的 {cleared_turns} 轮对话不会再进入上下文，也不会经由以前的摘要回来。\n"
        "上面的聊天记录仍然保留，只是作为可查阅的历史，不再参与推理。\n"
        "此前上传的附件仍可点开，但不会再自动带上——需要的话重新提一句。"
    )


async def _conversation_materials(
    session: Any,
    *,
    conversation_id: uuid.UUID,
    cleared_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """本会话里上传过的附件，最新的在前。

    构建助手的附件是**这个会话的材料**，不是「这一轮的一次性输入」：
    用户先传一份规程、问两句、隔一轮再说「按第三章入库」，那份文件应当还在，
    也不必让他重传。模型看不到磁盘，只能看到句柄，所以每轮都要把清单带上。

    只读消息元数据，不碰文件：某条记录对应的文件真的没了，模型读到的是
    「附件不存在」，不会因此读到别人的材料——解析仍然要过租户 + 会话目录。

    `cleared_at` 之后才带：`/clear` 的语义是「别再提之前的事」，清单也算
    「之前的事」——留着清单，模型下一轮就会开口问「你上次那份规程」。
    文件本身仍在会话里，用户点得开，只是不再自动进上下文。
    """
    stmt = select(Message.metadata_json).where(
        Message.conversation_id == conversation_id,
        Message.role == MessageRole.USER,
    )

    if cleared_at is not None:
        stmt = stmt.where(Message.created_at > cleared_at)

    rows = await session.scalars(
        stmt.order_by(Message.created_at.desc()).limit(50)
    )

    materials: list[dict[str, Any]] = []
    seen: set[str] = set()

    for metadata in rows.all():
        for item in (metadata or {}).get("attachments") or []:
            if not isinstance(item, dict):
                continue

            attachment_id = str(item.get("attachment_id") or "")
            mime = str(item.get("mime") or "")
            # 与 `_attachments_of` 同一口径：缺字段的旧记录直接跳过，不拿默认值凑。
            if not attachment_id or not mime or attachment_id in seen:
                continue

            seen.add(attachment_id)
            materials.append(
                {
                    "attachment_id": attachment_id,
                    "kind": str(item.get("kind") or attachment_store.kind_of(mime)),
                    "mime": mime,
                    "size": int(item.get("size") or 0),
                    "name": item.get("name") or None,
                }
            )
            if len(materials) >= _MAX_MATERIALS_IN_PROMPT:
                return materials

    return materials


def _prepare_attachments(attachments: list[Any]) -> list[dict[str, Any]]:
    """校验并解码附件，**不落盘**。

    放在建会话之前执行，是为了让「格式不对 / 太大 / 太多」这类拒绝
    不留下半个会话和一条孤立的 run 记录。
    """
    prepared: list[dict[str, Any]] = []
    total_bytes = 0

    for item in attachments:
        data_url = getattr(item, "data_url", None)
        if not data_url:
            # 纯 file_id 的引用还没有上传端点支撑，直接忽略。
            continue

        name = (getattr(item, "name", None) or "").strip()[:120] or None
        raw, mime = attachment_store.decode_data_url(
            data_url,
            max_image_bytes=settings.ocr_max_image_bytes,
            max_pdf_bytes=settings.ocr_max_pdf_bytes,
            # 文本附件没有文件头，类型判定只能靠后缀；浏览器给的 type
            # 对 .md 常常是空串，不传名字会被当成「无法识别的内容」。
            name=name,
        )

        total_bytes += len(raw)
        if total_bytes > settings.chat_max_attachment_bytes:
            limit_mb = settings.chat_max_attachment_bytes // 1024 // 1024
            raise attachment_store.AttachmentError(
                f"附件合计超过上限（{limit_mb} MB）。"
            )

        prepared.append({"raw": raw, "mime": mime, "name": name})

    if len(prepared) > settings.chat_max_attachments:
        raise attachment_store.AttachmentError(
            f"单条消息最多 {settings.chat_max_attachments} 个附件。"
        )

    return prepared


def _save_attachments(
    *,
    tenant_id: str,
    conversation_id: str,
    prepared: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把已校验的附件落盘，返回给状态与消息记录用的句柄列表。"""
    stored: list[dict[str, Any]] = []

    for item in prepared:
        record = attachment_store.save_bytes(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            raw=item["raw"],
            mime=item["mime"],
            name=item["name"],
        )
        stored.append(
            {
                "attachment_id": record.attachment_id,
                "kind": record.kind,
                "mime": record.mime,
                "size": record.size,
                "name": record.name,
            }
        )

    return stored


def _prompt_with_attachments(
    message: str,
    attachments: list[dict[str, Any]],
    materials: list[dict[str, Any]] | None = None,
) -> str:
    """在用户消息后追加附件清单。

    模型是纯文本的，看不到附件内容，只能看到句柄。
    这一步只告诉它「有哪些材料、句柄是什么、是什么类型」，是否识别由它自己决定——
    这正是 ocr_document 作为可选用工具、而不是流程节点的前提。

    `attachments` 是本条消息新增的，`materials` 是本会话更早的材料（不含本条）。
    两者分开写，是因为模型要区分「这次新给的东西」和「之前给过、现在还能用」：
    只列新增，它会以为「这轮没有材料」而请用户重传；只列全部，它又会把旧材料
    当成本次要处理的对象（2026-11-05 实测，拿上一份 PDF 的识别结果答新问题）。
    """
    materials = materials or []
    if not attachments and not materials:
        return message

    def render(items: list[dict[str, Any]]) -> str:
        return "\n".join(
            attachment_store.describe_for_prompt(
                attachment_id=item["attachment_id"],
                kind=item["kind"],
                mime=item["mime"],
                size=item["size"],
                name=item.get("name"),
            )
            for item in items
        )

    parts = [message, "", "[附件]"]

    if attachments:
        parts.append("本条消息新增：")
        parts.append(render(attachments))
    if materials:
        if attachments:
            parts.append("")
        parts.append("本会话此前上传的材料（随会话保留，仍可读取）：")
        parts.append(render(materials))
    if not attachments:
        # 不写这句，模型看到清单里没有「本条新增」会当作「这轮没材料」，
        # 于是请用户重新上传——而文件就在上面那些句柄里。
        parts.append("")
        parts.append(
            "本条消息没有新附件。用户这句话多半就是指上面的某一份材料，"
            "直接读取，不要让他重新上传。"
        )

    parts.append("")
    parts.append(
        "如需读取附件中的文字，调用 ocr_document 工具，file_id 原样使用上面的值。"
        # 材料会越攒越多，不点明优先级，模型容易拿清单里第一份（往往是最早传的）
        # 去回答这次的问题——2026-11-05 就是拿上一轮的图片结果答了新传的 PDF。
        "有新增附件时先处理本条消息新增的那几份；更早的材料只在用户明确指认时才用，"
        "不要把某一份的结果套到另一份上。"
    )

    return "\n".join(parts)


def _ocr_text_from_observations(result: dict[str, Any]) -> str | None:
    """从工具观察里取出 OCR 文本，供前端展示与审计回溯。

    一条消息可以有多个附件，模型也可能分几次识别，因此这里**全部收集**后再拼接；
    只取第一条会让用户以为后面的附件没被读到。
    """
    blocks: list[str] = []

    for item in result.get("tool_observations") or []:
        if item.get("tool_name") not in OCR_OBSERVATION_TOOL_NAMES:
            continue

        payload = item.get("result") or {}
        text = str(payload.get("text") or "").strip()

        if text:
            blocks.append(text)

    if not blocks:
        return None

    # 前端只展示摘要，超长文本没必要整段回传。
    return "\n\n".join(blocks)[:4000]


def _extract_interrupt(result: dict[str, Any]) -> dict[str, Any] | None:
    return extract_interrupt(result)


class ChatService:
    async def handle_message(
        self,
        *,
        identity: Identity,
        thread_id: str,
        message: str,
        attachments: list | None = None,
        boundary: AgentBoundary | None = None,
        workflow: str | None = None,
    ) -> ChatResponse:
        client_id = validate_client_thread_id(thread_id)

        with tenant_scope(identity.tenant_id):
            return await self._handle_message(
                identity=identity,
                client_thread=client_id,
                message=message,
                attachments=attachments,
                boundary=boundary or agent_service.BASELINE,
                workflow=workflow,
            )

    async def _handle_message(
        self,
        *,
        identity: Identity,
        client_thread: str,
        message: str,
        attachments: list | None = None,
        boundary: AgentBoundary | None = None,
        workflow: str | None = None,
    ) -> ChatResponse:
        boundary = boundary or agent_service.BASELINE

        # `/clear` 先于附件校验与配额：它既不消耗一次对话额度，也不该把附件
        # 当成一次输入（`/clear` 之后那些附件本来就不再进上下文）。
        # 放在这里而不是给模型一支「清空工具」，是因为这件事没有需要判断的地方——
        # 让模型参与只会多一种「它以为清了、其实没清」的失败方式。
        if is_clear_command(message):
            return await self._clear_context(
                identity=identity,
                client_thread=client_thread,
                boundary=boundary,
            )

        # 附件先校验、后扣配额：格式不对的请求不该消耗租户额度，
        # 也不该留下会话记录（否则用户会看到一条只有问题没有回答的空会话）。
        try:
            prepared = _prepare_attachments(attachments or [])
        except attachment_store.AttachmentError as exc:
            return ChatResponse(
                thread_id=client_thread,
                run_id="",
                status="rejected",
                answer=f"附件无法接收：{exc}",
                metadata={"reason": "invalid_attachment"},
                error_category="invalid_attachment",
            )

        # 配额在落库之前扣：额度用尽时不应该留下半条会话记录。
        try:
            await get_tenant_service().consume_chat_quota(identity.tenant_id)
        except TenantQuotaExceeded as exc:
            return ChatResponse(
                thread_id=client_thread,
                run_id="",
                status="rejected",
                answer=str(exc),
                metadata={"reason": "tenant_quota_exceeded"},
                error_category="tenant_quota_exceeded",
            )
        except RateLimitExceeded as exc:
            return ChatResponse(
                thread_id=client_thread,
                run_id="",
                status="rejected",
                answer=str(exc),
                metadata={"reason": "rate_limited"},
                error_category="rate_limited",
            )

        internal_thread = internal_thread_id(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            client_id=client_thread,
        )

        # 观测的根 trace 从这里开始：上面那几步（校验附件、扣配额）被拒绝时
        # 不该产生 trace，否则列表页会混进一堆「没有消息的失败」。
        async with trace_run(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            thread_id=internal_thread,
            # 观测从第一行就知道是谁在干活，不必等执行图后续再补。
            agent_name=boundary.agent_name or None,
            input_text=message,
        ) as trace:
            response = await self._run_turn(
                identity=identity,
                client_thread=client_thread,
                message=message,
                prepared=prepared,
                internal_thread=internal_thread,
                trace=trace,
                boundary=boundary,
                workflow=workflow,
            )

        # 记忆抽取排在回答落地之后：它要读完整的一问一答，
        # 而且抽取失败不能影响这一轮（后台任务，异常自己吞掉）。
        # 只对正常结束的一轮生效：等审批、超时、模型报错都没有「结论」可提炼。
        # `needs_input` 要算进来：反问这一轮没有结论，但用户那句话本身是真实的
        # 输入（「他问过 3 号调压箱」），漏掉会让下一轮又问一遍同样的事。
        if response.status in ("completed", "needs_input"):
            memory_extract.schedule_extraction(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                thread_id=internal_thread,
                user_message=message,
                answer=response.answer,
            )

        return response

    async def _clear_context(
        self,
        *,
        identity: Identity,
        client_thread: str,
        boundary: AgentBoundary | None = None,
    ) -> ChatResponse:
        """执行 `/clear`：写一条水位，历史留在原地。

        不这么做的两种后果都真实存在过：

        - 只回一句「已清空」而不写水位 → 下一轮照旧带上全部历史
          （线上就是如此：模型很配合，平台什么都没做）；
        - 真把消息删掉 → 界面干净了，但「他当时问过什么」再也查不到，
          审批单、审计、附件句柄指向的消息全成孤儿。

        所以清的是「这一轮还看得见哪些轮次」，不是数据本身。
        """
        internal_thread = internal_thread_id(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            client_id=client_thread,
        )

        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    conversation = await ensure_conversation(
                        session,
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        thread_id=internal_thread,
                        # 标题只在创建时写一次。`/clear` 作为标题读起来像一句命令，
                        # 而会话列表里需要的是「这条会话是什么」。
                        title="上下文清空",
                        channel=_channel_of(boundary),
                    )

                    # 先数「此前有多少轮」，再写这一条：本轮这两条不属于「此前」。
                    # 口径与人眼一致（数用户气泡），不复用 `turns_covered`——
                    # 那是「被挤出窗口的轮次」，与用户看到的行数不是一回事。
                    previous_from = conversation.context_cleared_at
                    counter = (
                        select(func.count())
                        .select_from(Message)
                        .where(
                            Message.conversation_id == conversation.id,
                            Message.role == MessageRole.USER,
                        )
                    )
                    if previous_from is not None:
                        # 二次清空只数上一次清空之后的：否则文案里的数字越清越大，
                        # 而用户眼里那段早就看不见了。
                        counter = counter.where(Message.created_at > previous_from)

                    cleared_turns = int(await session.scalar(counter) or 0)

                    await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=conversation.id,
                        role=MessageRole.USER,
                        content=CLEAR_COMMAND,
                    )
                    reply = await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=conversation.id,
                        role=MessageRole.ASSISTANT,
                        content=_clear_reply(cleared_turns),
                        # 刷新后要能认出「这一条是清空点」，前端据此插分隔条。
                        metadata={"context_cleared": True},
                    )

                    # 水位取这条回复的时刻，而不是 `now()`：`add_message` 显式写带微秒
                    # 的时间，两者不同，取它才能把 `/clear` 本身也严格排除在外。
                    # 取服务器当前时间则会因为同事务内 now() 相同而漏掉这两条。
                    conversation.context_cleared_at = reply.created_at
                    conversation.context_cleared_through_id = reply.id
                    conversation.last_active_at = reply.created_at

                    reply_id = reply.id
                    reply_at = reply.created_at

        return ChatResponse(
            thread_id=client_thread,
            # 没有 run：这不是一次「运行」。建一条 run 会让观测页多出一条
            # 没有模型调用的轮次，把「今天跑了多少轮」这个数污染掉。
            run_id="",
            status="completed",
            answer=_clear_reply(cleared_turns),
            metadata={
                "context_cleared": True,
                "cleared_turns": cleared_turns,
                "cleared_through_message_id": str(reply_id),
                "cleared_at": reply_at.isoformat(),
            },
            assistant_message_at=reply_at,
        )

    async def _run_turn(
        self,
        *,
        identity: Identity,
        client_thread: str,
        message: str,
        prepared: list[dict[str, Any]],
        internal_thread: str,
        trace: Any,
        boundary: AgentBoundary | None = None,
        workflow: str | None = None,
    ) -> ChatResponse:
        """一轮对话的实体：落库 → 跑图 → 收尾。

        单独成方法是为了让 `trace_run` 的 with 体包住全部三条出口
        （超时、等审批、正常完成），而不是在每处 return 前手写收尾。
        """
        async with span("persist.user_message", "db", attrs={"table": "messages"}) as db_span:
            async with async_session_factory() as session:
                async with session.begin():
                    conversation = await ensure_conversation(
                        session,
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        thread_id=internal_thread,
                        title=message[:60],
                        channel=_channel_of(boundary),
                    )

                    # 本条之前上传过的材料：附件是会话资产，跨轮可读，
                    # 所以每轮都要把清单重新带上（模型只看得到句柄，看不到磁盘）。
                    # 必须在写入本条消息之前查，否则本条会被算进「此前」。
                    # `/clear` 之后从水位算起：清空点是「别再提之前的事」，
                    # 材料清单也算「之前的事」，否则模型下一轮就会问「你上次那份规程」。
                    cleared_at = conversation.context_cleared_at
                    earlier = await _conversation_materials(
                        session,
                        conversation_id=conversation.id,
                        cleared_at=cleared_at,
                    )

                    # 落盘与会话绑定：句柄目录按 (租户, 会话) 隔离，
                    # 别人的 file_id 拿到也读不到。
                    stored = _save_attachments(
                        tenant_id=identity.tenant_id,
                        conversation_id=str(conversation.id),
                        prepared=prepared,
                    )

                    user_message = await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=conversation.id,
                        role=MessageRole.USER,
                        content=message,
                        # 附件句柄写进消息记录：文件随会话保留，历史里要能点开原文。
                        # 不写的话刷新后只剩一段没有出处的问题，用户也无从确认
                        # 自己当时到底传的是哪一版。
                        metadata={"attachments": stored} if stored else None,
                    )
                    run = await start_run(
                        session,
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        thread_id=internal_thread,
                        conversation_id=conversation.id,
                        input_summary=message,
                    )
                    run_id = str(run.id)
                    conversation_id = str(conversation.id)
                    # 记下落库时刻，随响应回给前端标注气泡时间。
                    user_message_at = user_message.created_at
                    user_message_id = user_message.id
                    # 上一轮实测的 prompt token：决定这一轮要不要压缩。
                    last_prompt_tokens = int(conversation.last_prompt_tokens or 0)

            # trace 开始时这两个 ID 还不存在，落库后补上，才能「从会话跳到 trace」。
            if trace is not None:
                trace.run_id = run_id
                trace.conversation_id = conversation_id
            db_span.add(attachments=len(stored) + len(earlier))

        # 更早的对话：算窗口、必要时压缩。放在落库之后、执行图之前——
        # 让当前这条用户消息先落地，压缩才可能把它排除在外（它永不进摘要）。
        # 失败只让注入变少，不影响这一轮（见 context_service.prepare）。
        context_block = await context_service.prepare(
            conversation_id=uuid.UUID(conversation_id),
            user_message=message,
            measured_tokens=last_prompt_tokens,
            exclude_message_id=user_message_id,
            # 水位在落库那个事务里已经读出来了，这里直接用：再查一次会拿到
            # 一个新快照，与刚刚写下去的那条对不上。
            cleared_at=cleared_at,
        )

        # 附件已落盘成句柄：state 里只留 attachment_id，不把 base64 写进 checkpoint。
        prompt_message = _prompt_with_attachments(message, stored, materials=earlier)

        state = {
            "messages": [HumanMessage(content=prompt_message)],
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "thread_id": internal_thread,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "request_id": _request_id(),
            "roles": list(identity.roles),
            "scopes": list(identity.scopes),
            # 与路由层 `_can_manage` 同一口径：管理员不该出现「按钮能点、对话里没权限」。
            "is_platform_admin": bool(identity.is_platform_admin),
            # 本条新增 + 本会话此前的材料：都算这个会话可读的材料，
            # 读取工具的句柄校验与委派下传都按这一份清单走。
            "attachments": [*earlier, *stored],
            # 更早的对话（摘要 + 最近几轮原文）。空摘要与空轮次都表示「没有」。
            "context_block": {"summary": context_block.summary, "turns": context_block.turns},
            # 委派链每轮归零。不能交给 `begin_turn`：子调用会跑一遍同一个图，
            # 它在子图里执行时会把深度与环的记录清掉，委派链当场失效。
            "delegation_path": [],
            "delegation_count": 0,
            **_agent_state(boundary or agent_service.BASELINE),
        }

        # 编排是显式触发的。不给规划器增加「workflow 意图」：让模型自己挑一张图，
        # 等于把刚买到的确定性又交回给模型（ORCHESTRATION.md §4 D5）。
        # 界面上选了一条流程时走同一个入口：`triggered_command` 与
        # `parse_command` 共用名字校验，两种触发方式不可能长出两套行为。
        # 消息正文本身以 `/workflow` 开头时以正文为准——那是更明确的那句话。
        command = workflow_service.parse_command(message)
        if command is None and workflow:
            command = workflow_service.triggered_command(workflow, message)
        if command is not None:
            return await self._run_workflow_turn(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                state=state,
                command=command,
            )

        graph = get_agent_graph()
        try:
            async with span("graph.invoke", "graph"):
                async with asyncio.timeout(settings.chat_timeout_seconds):
                    result = await graph.ainvoke(
                        state,
                        config={"configurable": {"thread_id": internal_thread}},
                    )
        except TimeoutError:
            # 总时限是防「客户端断开但服务端继续空转」的最后一道闸。
            # 超时后没人会替这条会话写结论，所以这里自己收尾：
            # 关掉 run、补一条 assistant 消息，用户刷新会话能看到发生了什么。
            timeout_seconds = settings.chat_timeout_seconds
            answer = (
                f"这次请求超过 {timeout_seconds} 秒仍未完成，已中止。"
                "可以直接重试；若反复出现请联系管理员。"
            )
            logger.warning(
                "对话请求超时，已中止。",
                extra={
                    "event": "chat_timeout",
                    "run_id": run_id,
                    "timeout_seconds": timeout_seconds,
                },
            )
            await finish_run(
                run_id=run_id,
                status=RunStatus.FAILED,
                error_category="chat_timeout",
                output_summary=answer,
            )
            if trace is not None:
                trace.mark(
                    status="timeout",
                    error_category="chat_timeout",
                    output_text=answer,
                )
            async with async_session_factory() as session:
                async with session.begin():
                    timeout_message = await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=uuid.UUID(conversation_id),
                        role=MessageRole.ASSISTANT,
                        content=answer,
                        metadata={"intent": "error", "error_category": "chat_timeout"},
                    )
            return ChatResponse(
                thread_id=client_thread,
                run_id=run_id,
                status="error",
                answer=answer,
                metadata={
                    "reason": "chat_timeout",
                    "timeout_seconds": timeout_seconds,
                },
                error_category="chat_timeout",
                user_message_at=user_message_at,
                assistant_message_at=timeout_message.created_at,
            )

        interrupt = _extract_interrupt(result)

        # 这一轮模型实际看到的 prompt 有多大。落库供下一轮判断要不要压缩——
        # 只在观测里记是不够的：观测默认关闭，而压缩的依据不能随之消失。
        usage = take_usage_for_run(run_id)
        await context_service.remember_prompt_tokens(
            conversation_id=uuid.UUID(conversation_id),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
        )

        if interrupt and interrupt.get("kind") == "approval":
            await finish_run(
                run_id=run_id,
                status=RunStatus.WAITING_FOR_APPROVAL,
                intent="write_tool",
                approval_id=str(interrupt.get("approval_id")),
                output_summary="等待用户确认写操作预览。",
            )
            if trace is not None:
                trace.mark(
                    status="awaiting_approval",
                    intent="write_tool",
                    tool_name=str(interrupt.get("tool_name") or "") or None,
                    output_text=interrupt.get("message"),
                )

            # 这句「请确认」也要落库。只放进响应的话，一刷新会话这条就没了，
            # 历史里只剩一句没有下文的提问——用户会以为操作丢了，再发一遍，
            # 于是多出一张重复的审批单。审批号写进 metadata，前端靠它把卡拼回来。
            pending_text = str(interrupt.get("message") or "该操作等待确认，尚未执行。")
            async with async_session_factory() as session:
                async with session.begin():
                    pending_message = await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=uuid.UUID(conversation_id),
                        role=MessageRole.ASSISTANT,
                        content=pending_text,
                        metadata={
                            "intent": "write_tool",
                            "tool_name": str(interrupt.get("tool_name") or "") or None,
                            "approval_id": str(interrupt.get("approval_id") or "") or None,
                        },
                    )

            return ChatResponse(
                thread_id=client_thread,
                run_id=run_id,
                status="awaiting_approval",
                answer=interrupt.get("message"),
                approval=ApprovalPendingResponse(
                    approval_id=str(interrupt["approval_id"]),
                    preview=interrupt.get("preview") or {},
                    expires_at=interrupt.get("expires_at"),
                    message=str(interrupt.get("message") or "该操作等待确认，尚未执行。"),
                ),
                metadata={"approval_id": str(interrupt.get("approval_id"))},
                user_message_at=user_message_at,
                assistant_message_at=pending_message.created_at,
            )

        delegated = result.get("pending_delegated_approval") or {}

        if delegated.get("approval_id"):
            return await self._awaiting_delegated_approval(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                user_message_at=user_message_at,
                trace=trace,
                answer=result.get("final_response"),
                delegated=delegated,
            )

        answer = result.get("final_response") or "当前无法生成有效回复。"
        error_category = result.get("error_category")
        ocr_text = _ocr_text_from_observations(result)

        if trace is not None:
            trace.mark(
                status="error" if result.get("intent") == "error" else "ok",
                error_category=error_category,
                intent=result.get("intent"),
                tool_name=result.get("planned_tool_name"),
                output_text=answer,
            )

        # 追问项跟着消息一起入库：刷新会话后卡片要能原样回来。
        # 只放在响应里的话，用户一刷新就只剩「请在下面选择…」这句没有选项的话。
        clarify = (result.get("response_metadata") or {}).get("clarify")
        # 这次真正送进检索的那句话。规划器做的最小改写用户看不见，
        # 而「对话命中 A、检索验证页命中 B」正是从这里开始分叉的——
        # 落库后验证页才能拿它复跑，不必去翻 checkpoint。
        knowledge_query = (result.get("response_metadata") or {}).get("knowledge_query")
        # 这条答复是归纳出来的还是原样摘的。同一句「切断阀有何作用」在
        # 开关切换前后措辞不同，没有这个字段就只能猜；它同时是
        # 「归纳失败回落了几次」的唯一留存（日志会轮转）。
        knowledge_mode = (result.get("response_metadata") or {}).get(
            "knowledge_answer_mode"
        )
        knowledge_fallback = (result.get("response_metadata") or {}).get(
            "knowledge_answer_fallback"
        )
        # 结果气泡里的结构图（编排流程这类「结果本身有形状」的工具给的）。
        # 与确认卡上那张同源，但走的是两条路：卡从审批单重新取，气泡从这条消息取
        # ——不落库的话，刷新一次图就没了，只剩「步骤：a → b」那行字。
        result_graph = (result.get("response_metadata") or {}).get("graph")

        async with span("persist.assistant_message", "db", attrs={"table": "messages"}):
            async with async_session_factory() as session:
                async with session.begin():
                    assistant_message = await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=uuid.UUID(conversation_id),
                        role=MessageRole.ASSISTANT,
                        content=answer,
                        metadata={
                            "intent": result.get("intent"),
                            "tool_name": result.get("planned_tool_name"),
                            # 识别原文一并入库：刷新会话后仍能展开核对。
                            # 只放在响应里的话，这一轮过去就再也找不回来了。
                            **({"ocr_text": ocr_text} if ocr_text else {}),
                            **({"clarify": clarify} if clarify else {}),
                            **({"knowledge_query": knowledge_query} if knowledge_query else {}),
                            **({"knowledge_answer_mode": knowledge_mode} if knowledge_mode else {}),
                            **(
                                {"knowledge_answer_fallback": knowledge_fallback}
                                if knowledge_fallback
                                else {}
                            ),
                            **({"graph": result_graph} if result_graph else {}),
                        },
                    )

        return ChatResponse(
            thread_id=client_thread,
            run_id=run_id,
            status=_response_status(result),
            answer=answer,
            citations=result.get("citations") or [],
            metadata=result.get("response_metadata") or {},
            ocr_text=ocr_text,
            error_category=error_category,
            user_message_at=user_message_at,
            assistant_message_at=assistant_message.created_at,
        )

    async def _run_workflow_turn(
        self,
        *,
        identity: Identity,
        client_thread: str,
        run_id: str,
        conversation_id: str,
        internal_thread: str,
        user_message_at: Any,
        trace: Any,
        state: dict[str, Any],
        command: workflow_service.Command,
    ) -> ChatResponse:
        """会话内触发一次编排。

        三种结局与普通一轮对话对齐：跑完 → `completed`；停审批 → `awaiting_approval`
        且给同一张确认卡；失败 → `error`，原因来自引擎（预算、深度、节点失败）。
        """
        if command.invalid:
            return await self._workflow_reply(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                answer=(
                    "流程名不合法：只能用小写字母开头，后接小写字母、数字、"
                    "短横线或下划线，最长 64 个字符。"
                ),
                error_category="workflow_bad_name",
            )

        if not command.name:
            rows = await workflow_service.list_workflows(identity.tenant_id)
            if not rows:
                answer = (
                    "当前租户还没有配置任何流程。"
                    "可以在控制台的「编排」页新建一张，或让管理员替你配置。"
                )
            else:
                lines = ["当前可用的流程："]
                for row in rows:
                    plan = workflow_service.plan_of(row)
                    shape = (
                        f"{plan.size} 个节点 · 最长 {plan.depth_hops} 跳"
                        if plan
                        else "图不成立，需要重新保存"
                    )
                    suffix = "" if row.enabled else "（已停用）"
                    lines.append(f"- `{row.name}`{suffix}：{row.display_name}（{shape}）")
                lines.append("")
                lines.append("用 `/workflow <名字> <要说的事>` 触发。")
                answer = "\n".join(lines)

            return await self._workflow_reply(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                answer=answer,
            )

        workflow = await workflow_service.get_workflow(identity.tenant_id, command.name)
        if workflow is None:
            return await self._workflow_reply(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                answer=f"没有名为「{command.name}」的流程。发 `/workflow` 看看有哪些。",
                error_category="workflow_not_found",
            )

        if not workflow.enabled:
            return await self._workflow_reply(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                answer=f"流程「{workflow.display_name}」已停用。",
                error_category="workflow_disabled",
            )

        if not command.input_text:
            return await self._workflow_reply(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                answer=(
                    f"这条流程需要一句要处理的事：`/workflow {command.name} <要说的事>`。"
                ),
                error_category="workflow_no_input",
            )

        result = await workflow_service.start(
            identity=identity,
            context=context_from_state(state),
            workflow=workflow,
            input_text=command.input_text,
            conversation_id=conversation_id,
            thread_id=internal_thread,
            run_id=run_id,
        )
        outcome = result.outcome

        if outcome.status == engine.STATUS_PAUSED:
            return await self._workflow_pending(
                identity=identity,
                client_thread=client_thread,
                run_id=run_id,
                conversation_id=conversation_id,
                internal_thread=internal_thread,
                user_message_at=user_message_at,
                trace=trace,
                workflow=workflow.name,
                workflow_run_id=result.run_id,
                approval=outcome.approval or {},
                text=outcome.message or "流程停在写操作确认上，确认后才会继续。",
            )

        answer = outcome.answer or outcome.message or "流程没有产出结论。"
        failed = outcome.status == engine.STATUS_FAILED

        return await self._workflow_reply(
            identity=identity,
            client_thread=client_thread,
            run_id=run_id,
            conversation_id=conversation_id,
            internal_thread=internal_thread,
            user_message_at=user_message_at,
            trace=trace,
            answer=answer,
            error_category=outcome.error_code if failed else None,
            metadata={
                "workflow": workflow.name,
                "workflow_run_id": result.run_id,
                "nodes_used": outcome.nodes_used,
                "nodes": [item.as_dict() for item in outcome.trace],
            },
        )

    async def _workflow_reply(
        self,
        *,
        identity: Identity,
        client_thread: str,
        run_id: str,
        conversation_id: str,
        internal_thread: str,
        user_message_at: Any,
        trace: Any,
        answer: str,
        error_category: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """把编排的结论落成一条助手消息，并收口这一轮。

        为什么复用 `add_message` 而不是另开一张表：用户看到的应该是一条普通回答。
        「这是流程产出的」放在 metadata 里，界面要展示时再取。
        """
        failed = bool(error_category)
        await finish_run(
            run_id=run_id,
            status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
            intent="workflow",
            error_category=error_category,
            output_summary=answer,
        )

        if trace is not None:
            trace.mark(
                status="error" if failed else "ok",
                intent="workflow",
                error_category=error_category,
                output_text=answer,
            )

        async with async_session_factory() as session:
            async with session.begin():
                message = await add_message(
                    session,
                    tenant_id=identity.tenant_id,
                    conversation_id=uuid.UUID(conversation_id),
                    role=MessageRole.ASSISTANT,
                    content=answer,
                    metadata={"intent": "workflow", **(metadata or {})},
                )

        return ChatResponse(
            thread_id=client_thread,
            run_id=run_id,
            status="error" if failed else "completed",
            answer=answer,
            metadata={"intent": "workflow", **(metadata or {})},
            error_category=error_category,
            user_message_at=user_message_at,
            assistant_message_at=message.created_at,
        )

    async def _workflow_pending(
        self,
        *,
        identity: Identity,
        client_thread: str,
        run_id: str,
        conversation_id: str,
        internal_thread: str,
        user_message_at: Any,
        trace: Any,
        workflow: str,
        workflow_run_id: str,
        approval: dict[str, Any],
        text: str,
    ) -> ChatResponse:
        """编排停在写操作上：与「自己发起写操作」给同一个形态的卡。

        用户不需要知道这张单子是流程里第几步开的——他只看见一件事：
        有个写操作在等他点。步骤信息放在 metadata，回答之后再展示。
        """
        approval_id = str(approval.get("approval_id") or "")

        await finish_run(
            run_id=run_id,
            status=RunStatus.WAITING_FOR_APPROVAL,
            intent="workflow",
            approval_id=approval_id,
            output_summary="编排停在写操作确认。",
        )

        if trace is not None:
            trace.mark(
                status="awaiting_approval",
                intent="workflow",
                tool_name=str((approval.get("preview") or {}).get("tool_name") or "") or None,
                output_text=text,
            )

        async with async_session_factory() as session:
            async with session.begin():
                message = await add_message(
                    session,
                    tenant_id=identity.tenant_id,
                    conversation_id=uuid.UUID(conversation_id),
                    role=MessageRole.ASSISTANT,
                    content=text,
                    metadata={
                        "intent": "workflow",
                        "workflow": workflow,
                        "workflow_run_id": workflow_run_id,
                        "approval_id": approval_id,
                    },
                )

        return ChatResponse(
            thread_id=client_thread,
            run_id=run_id,
            status="awaiting_approval",
            answer=text,
            approval=ApprovalPendingResponse(
                approval_id=approval_id,
                preview=approval.get("preview") or {},
                expires_at=approval.get("expires_at"),
                message=text,
            ),
            metadata={
                "intent": "workflow",
                "workflow": workflow,
                "workflow_run_id": workflow_run_id,
                "approval_id": approval_id,
            },
            user_message_at=user_message_at,
            assistant_message_at=message.created_at,
        )

    async def _resume_workflow_if_any(
        self,
        *,
        identity: Identity,
        approval_id: str,
        action: str,
        execution: str,
        node_output: str,
        internal_thread: str,
        run_id: str,
    ) -> str | None:
        """这张审批单如果是某条编排停下来的原因，就把剩下的节点跑完。

        不续跑的话，流程会永远停在 `paused`，而用户以为点完就结束了——
        这在界面上表现为「流程卡住」，比报错更难理解。
        """
        started = await workflow_service.find_paused_run(
            tenant_id=identity.tenant_id,
            approval_id=approval_id,
        )
        if started is None:
            return None

        if action != "confirm":
            outcome_status = "cancelled"
        elif execution in {"EXECUTED", "VERIFIED"}:
            outcome_status = "ok"
        else:
            outcome_status = "failed"

        try:
            result = await workflow_service.resume(
                identity=identity,
                root_context=workflow_service.resume_context(
                    identity,
                    started,
                    thread_id=internal_thread,
                    run_id=run_id,
                ),
                run=started,
                node_output=node_output,
                outcome_status=outcome_status,
            )
        except Exception:
            logger.exception(
                "编排续跑失败。",
                extra={"event": "workflow_resume_failed", "approval_id": approval_id},
            )
            return "这条流程在继续执行时出错了，后续节点没有跑完。可以查看「编排」页的运行记录。"

        outcome = result.outcome

        if outcome.status == engine.STATUS_PAUSED:
            next_approval = str((outcome.approval or {}).get("approval_id") or "")
            return (
                "流程还需要你确认下一项写操作"
                + (f"（单号 {next_approval}）" if next_approval else "")
                + "，确认后才会继续。可以到「审批」列表处理。"
            )

        if outcome.status == engine.STATUS_CANCELLED:
            return "这次操作已取消，流程停在原处，后续节点没有执行。"

        if outcome.status == engine.STATUS_FAILED:
            return f"流程在后续节点停下：{outcome.message or outcome.error_code}"

        tail = outcome.answer or "流程已跑完。"
        return f"流程继续执行完毕。\n\n{tail}"

    async def _awaiting_delegated_approval(
        self,
        *,
        identity: Identity,
        client_thread: str,
        run_id: str,
        conversation_id: str,
        user_message_at: Any,
        trace: Any,
        answer: str | None,
        delegated: dict[str, Any],
    ) -> ChatResponse:
        """被转交方开出的审批单：这一轮同样停在「等人确认」。

        为什么不复用上面那个 interrupt 分支：那个分支是本图真的中断了
        （`create_and_wait_approval` 抛出了 `interrupt()`），恢复时靠的是
        checkpointer 里的断点。这里本图没有中断——它跑完了，只是拿到一个
        「对方在等审批」的结果。两者对用户的形态应当一致（同一张卡），
        但对执行图的意义完全不同，所以分成两条路。

        `answer` 里保留发起方自己的话，**并且**一定追加一句服务端的结论：
        模型很容易把「已经转交给它了」说成「已经办好了」。状态由服务端说了算，
        不能让一句转述把一张还没点过的确认卡变成既成事实。
        """
        approval_id = str(delegated.get("approval_id"))
        display_name = str(delegated.get("display_name") or delegated.get("agent") or "")
        canonical = str(delegated.get("message") or "该操作等待确认，尚未执行。")

        text = str(answer or "").strip()
        if text and canonical not in text:
            text = f"{text}\n\n{canonical}"
        elif not text:
            text = canonical

        await finish_run(
            run_id=run_id,
            status=RunStatus.WAITING_FOR_APPROVAL,
            intent="write_tool",
            approval_id=approval_id,
            output_summary="等待用户确认被转交智能体的写操作。",
        )

        if trace is not None:
            trace.mark(
                status="awaiting_approval",
                intent="write_tool",
                tool_name=str((delegated.get("preview") or {}).get("tool_name") or "") or None,
                output_text=text,
            )

        # 审批号进 metadata：刷新会话后前端靠它把卡拼回来，
        # 而审批单本身记着 parent_thread_id，确认后的结论会回到这条会话。
        async with async_session_factory() as session:
            async with session.begin():
                message = await add_message(
                    session,
                    tenant_id=identity.tenant_id,
                    conversation_id=uuid.UUID(conversation_id),
                    role=MessageRole.ASSISTANT,
                    content=text,
                    metadata={
                        "intent": "write_tool",
                        "tool_name": (delegated.get("preview") or {}).get("tool_name"),
                        "approval_id": approval_id,
                        "delegated_to": delegated.get("agent"),
                    },
                )

        return ChatResponse(
            thread_id=client_thread,
            run_id=run_id,
            status="awaiting_approval",
            answer=text,
            approval=ApprovalPendingResponse(
                approval_id=approval_id,
                preview=delegated.get("preview") or {},
                expires_at=delegated.get("expires_at"),
                message=canonical,
            ),
            metadata={
                "approval_id": approval_id,
                "delegated_to": delegated.get("agent"),
                "operation": delegated.get("operation"),
                "display_name": display_name,
            },
            user_message_at=user_message_at,
            assistant_message_at=message.created_at,
        )

    async def _append_assistant(
        self,
        *,
        identity: Identity,
        internal_thread: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        """把一句助手回复补进这条线程的会话记录。

        找不到会话就记日志后跳过：调用方此时外部操作已经做完了，
        为了补记录而抛异常，会把「执行成功」变成「报错」。

        定位用 thread_id + 租户 + 用户三条件，不用客户端传来的会话 ID——
        resume 路径上唯一可信的依据就是审批记录里冻结的 thread_id。
        """
        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    conversation = await session.scalar(
                        select(Conversation).where(
                            Conversation.thread_id == internal_thread,
                            Conversation.tenant_id == identity.tenant_id,
                            Conversation.user_id == identity.user_id,
                        )
                    )

                    if conversation is None:
                        logger.warning(
                            "审批恢复定位不到会话，回复未落库。",
                            extra={
                                "event": "approval_reply_unpersisted",
                                "thread_id": internal_thread,
                            },
                        )
                        return

                    await add_message(
                        session,
                        tenant_id=identity.tenant_id,
                        conversation_id=conversation.id,
                        role=MessageRole.ASSISTANT,
                        content=content,
                        metadata=metadata,
                    )

    async def resume_approval(
        self,
        *,
        identity: Identity,
        thread_id: str,
        approval_id: str,
        action: str,
        persist_thread_id: str | None = None,
        delegated: bool = False,
        operation_id: str | None = None,
        agent_name: str | None = None,
    ) -> ChatResponse:
        """恢复被 interrupt 的图。thread_id 必须来自服务端审批记录。

        委派来的审批单有两处不同（`delegated=True`）：

        - **恢复的是子线程**（`thread_id` 是子调用的），结论却要落到发起方的
          会话（`persist_thread_id`）。落在子线程上，用户永远看不到——
          那条线程不会出现在任何会话列表里；
        - 恢复完就把子线程的检查点丢掉：它的一次性用途已经结束。

        委派**不做第二次确认**，所以这里没有「转交给 A 再让 A 转述一遍」的
        回合。A 在发起那一轮已经把话说完了，这一轮只有一个新事实：B 执行完的
        结论是什么。

        **这一轮本身也是一次运行，同样要有 trace。** 它可能真的写进了外部系统，
        也可能因为批准期间上游收窄而整单作废——两种都必须留下证据。
        `operation_id` 从审批单继承，于是「发起」与「执行」在运行视图里是同一次
        运行，而不是两条互不相干的记录。
        """
        graph = get_agent_graph()
        run_id = str(uuid.uuid4())

        async with trace_run(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            thread_id=persist_thread_id or thread_id,
            run_id=run_id,
            agent_name=agent_name,
            operation_id=operation_id,
            # 这一轮的输入不是用户的话，是「点了哪个按钮」。原文位置留给它，
            # 否则运行视图里这一段会显示成一个没有输入的空轮。
            input_text=(
                f"[确认审批 {approval_id}]"
                if action == "confirm"
                else f"[取消审批 {approval_id}]"
            ),
        ) as trace:
            try:
                with tenant_scope(identity.tenant_id):
                    result = await graph.ainvoke(
                        Command(resume={"action": action, "approval_id": approval_id}),
                        config={"configurable": {"thread_id": thread_id}},
                    )
            finally:
                if delegated:
                    await discard_thread(thread_id)

            answer = result.get("final_response") or "无法确认操作结果。"

            if delegated:
                # 点名是谁做的：这句话会出现在发起方的会话里，而那个会话里
                # 从头到尾只有 A 在说话，不点名就成了「A 自己办的」。
                who = str(
                    result.get("agent_display_name")
                    or result.get("agent_name")
                    or "被转交的智能体"
                )
                # 前缀必须跟着**执行结果**走，不能跟着「点了确认」走。
                # 实机上踩过：批准期间上游被收窄 → 单子作废、什么都没执行，
                # 而前缀照样写着「已完成你确认的操作」，紧接着下一句是「操作未执行」。
                # 同一段话里自相矛盾，读者只会记住前一句。
                status = str(result.get("execution_status") or "")
                if action != "confirm":
                    lead = f"「{who}」的这次操作已取消。"
                elif status in {"EXECUTED", "VERIFIED"}:
                    lead = f"「{who}」已完成你确认的操作。"
                else:
                    lead = f"「{who}」这次操作没有执行成功。"
                answer = f"{lead}\n\n{answer}"

            execution = str(result.get("execution_status") or "")

            # 结论同样要落库。不写的话，确认完再刷新会话，历史停在「请确认」那一句，
            # 人会以为没执行成功，再点一次——虽然幂等键挡住了重复执行，
            # 但「历史里看不出发生过什么」本身就不该出现。
            # 结论这条消息也要带结构图：确认完之后人看的是这一段（卡片已经
            # 变成「已处理」），而图正是他核对自己批了什么的东西。不带的话，
            # 刷新之后结论还在、图没了，同一轮对话长成两个样子。
            result_graph = (result.get("response_metadata") or {}).get("graph")

            await self._append_assistant(
                identity=identity,
                internal_thread=persist_thread_id or thread_id,
                content=answer,
                metadata={
                    "intent": result.get("intent"),
                    "approval_id": approval_id,
                    "approval_action": action,
                    **({"delegated_to": result.get("agent_name")} if delegated else {}),
                    **({"graph": result_graph} if result_graph else {}),
                },
            )

            # 这张单子如果是某条编排停下来的原因，接着把剩下的节点跑完。
            # 挂在审批恢复这一跳而不是另起一个轮询器：人点完确认的那一刻，
            # 正好是「流程可以继续了」的那一刻，两者之间没有需要等待的空档。
            extra = await self._resume_workflow_if_any(
                identity=identity,
                approval_id=approval_id,
                action=action,
                execution=execution,
                node_output=str(result.get("final_response") or answer),
                internal_thread=persist_thread_id or thread_id,
                run_id=run_id,
            )
            if extra:
                answer = f"{answer}\n\n{extra}"
                await self._append_assistant(
                    identity=identity,
                    internal_thread=persist_thread_id or thread_id,
                    content=extra,
                    metadata={"intent": "workflow", "approval_id": approval_id},
                )

            if trace is not None:
                # 取消不是失败，是另一种终态；「执行成功但模型最后报了错」也不是
                # 执行失败。三种分开记，否则运行视图会把「用户改主意」统计成错误。
                if action != "confirm":
                    trace_status = "cancelled"
                elif result.get("intent") == "error" or execution in {
                    "FAILED",
                    "EXECUTION_UNKNOWN",
                    "RECOVERY_REQUIRED",
                }:
                    trace_status = "error"
                else:
                    trace_status = "ok"
                trace.mark(
                    status=trace_status,
                    error_category=result.get("error_category"),
                    tool_name=result.get("planned_tool_name"),
                )

            return ChatResponse(
                thread_id=persist_thread_id or thread_id,
                run_id=run_id,
                status=_response_status(result),
                answer=answer,
                metadata=result.get("response_metadata") or {},
                error_category=result.get("error_category"),
            )


def _response_status(result: dict[str, Any]) -> str:
    """把执行图的 intent 折成对外状态。

    三种结果不能合并：`error` 是没办成且原因在系统侧，`needs_input`
    是没办成且原因在信息不全，`completed` 是办成了。前两种混进 `completed`
    之后，程序化调用方（MCP 委派、编排节点）会把一句反问当成结论转述出去。
    """
    intent = result.get("intent")

    if intent == "error":
        return "error"
    if intent == "clarify":
        return "needs_input"

    return "completed"


def _agent_state(boundary: AgentBoundary) -> dict[str, Any]:
    """把边界摊平进 state。

    state 会进 checkpoint，所以只放执行图真正要用的东西：
    提示词块在 `plan_request` 里按需渲染，不把整段文本存进去。
    """
    return {
        "agent_id": boundary.agent_id,
        "agent_name": boundary.agent_name,
        "agent_display_name": boundary.display_name,
        # 默认 Agent 也带名字，但它没有职责块（instruction 为空），
        # 所以提示词与「没有这套机制」时逐字相同。
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
        "agent_can_write": boundary.can_write,
        # 委派预算随边界一起进 state：本轮内改了配置也不影响已经进来的这一轮，
        # 否则同一个会话里前后两次转交会适用两套限值。
        "agent_max_delegation_depth": boundary.max_delegation_depth,
        "agent_delegation_budget": boundary.delegation_budget,
    }


def _request_id() -> str:
    from app.core.logging import request_id_context

    return request_id_context.get() or ""
