# app/services/reference_service.py
"""引用传递（P1）：委派时传「哪份材料」，而不是把材料正文塞进任务描述。

设计见 `docs/AGENT_CONNECT.md` §3。四条约束，每条都对应一类曾经踩过的坑：

1. **三态，不能塌成二态。** 可见 / 不可见 / 已失效必须分开返回。
   把「读不到」返回成空内容，会让 B 以为 A 那边本来就没有这份材料，
   然后编一个看起来合理的结论——错误被静默传播，且没有任何一条日志。
   这就是 RLS 那个坑的同构版本：「清干净了」与「看不见」长得一模一样。

2. **全有或全无。** A 传 5 个引用、1 个不可见 → 整单拒绝，不静默丢 1 个。
   部分成功会让 A 以为 B 看到了全部，而 B 只看到 4 个还会照样给出「完整」的结论。

3. **判定用子调用（B）的身份。** 这里接收的 `user_id` / `scopes` / `roles`
   是**传给子调用的那一组**，不是发起方自己另算一份。虽然 P0 里两者相同
   （身份继承发起人），但把它们显式当参数传进来，是为了让「在谁的上下文里判定」
   这件事在签名上就写死——改成 B 自己的服务身份时，所有人一眼能看见这里要跟着改。

4. **拒绝理由要点名缺什么**（`missing_scope=knowledge.confidential`）。
   只说「引用不可用」会让排障停在「那就换一份材料吧」，而真正要改的是授权。

`entity` 是唯一不做本地判定的类型：站点授权在业务工具那一侧，
这里既查不到也不该查。硬在这里加一层「引用时校验站点」会得到两套授权，
而它俩早晚不一致。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.db.session import async_session_factory
from app.knowledge.documents import acl_of, chunks_of
from app.knowledge.retriever import allowed_scopes_for
from app.models.artifact import Artifact
from app.models.conversation import Conversation, Message
from app.models.document import Document, DocumentStatus
from app.services import attachment_store
from app.storage.object_store import ObjectNotFound, ObjectStoreError, get_object_store
from app.tenancy.context import tenant_scope

logger = logging.getLogger(__name__)

REF_ARTIFACT = "artifact"
REF_MESSAGE = "message"
REF_KNOWLEDGE = "knowledge"
REF_ENTITY = "entity"

REF_KINDS = (REF_ARTIFACT, REF_MESSAGE, REF_KNOWLEDGE, REF_ENTITY)

# 一次最多带 10 个引用。这个数与任务描述的长度上限是配套的：
# 再多的材料已经不是「交给对方一件事」，而是让对方读一座库——
# 那件事应该由 B 自己去检索，或者由人拆成几轮。
MAX_REFS = 10

REF_ID_LIMIT = 256
SECTION_LIMIT = 64

# 摘录上限。给短摘录是为了让 B 能转述；给全文就等于绕过了「引用不是正文」。
QUOTE_LIMIT = 200
MESSAGE_LIMIT = 500

_LABELS = {
    REF_ARTIFACT: "产物",
    REF_MESSAGE: "会话消息",
    REF_KNOWLEDGE: "知识文档",
    REF_ENTITY: "业务对象",
}

# 数据分级 → 需要的 scope。拒绝时点名的是 scope 名而不是分级名：
# 排障的人手里拿着的是授权配置，不是文档元数据。
_SCOPE_FOR_LEVEL = {
    "public": "knowledge.public",
    "internal": "knowledge.internal",
    "confidential": "knowledge.confidential",
}


class ReferenceError(RuntimeError):
    """引用不可用。三个子类对应三种结局，不要合并成一个。"""

    code = "REF_INVALID"

    def __init__(
        self,
        message: str,
        *,
        kind: str = "",
        ref_id: str = "",
        missing_scope: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.ref_id = ref_id
        self.missing_scope = missing_scope

    def as_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"kind": self.kind, "id": self.ref_id}
        if self.missing_scope:
            detail["missing_scope"] = self.missing_scope
        return detail


class ReferenceInvalidError(ReferenceError):
    """入参本身不成立（类型不认识、id 为空）。**不是第四态**——
    它说的不是「这份材料怎样了」，而是「这条请求根本没法判」。"""

    code = "REF_INVALID"


class ReferenceNotVisibleError(ReferenceError):
    """材料存在，但 B 无权看。"""

    code = "REF_NOT_VISIBLE"


class ReferenceGoneError(ReferenceError):
    """材料已经不存在（行被删、文档下架、文件丢了）。"""

    code = "REF_GONE"


@dataclass(frozen=True, slots=True)
class RefSpec:
    kind: str
    ref_id: str
    section: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    kind: str
    ref_id: str
    detail: str = ""
    # 可以真的读的产物：值直接就是子调用 state 里 attachments 的元素。
    attachment: dict[str, Any] | None = field(default=None, repr=False)


# ---------------------------------------------------------------- 入参


def parse_refs(raw: Any) -> tuple[RefSpec, ...]:
    """校验模型给的 `refs`。

    重复的引用直接合并：A 可能看不出来自己把同一份材料写了两遍，
    而报错会让人以为是「引用机制坏了」。
    """
    if raw in (None, "", []):
        return ()

    if not isinstance(raw, list):
        raise ReferenceInvalidError("refs 必须是数组，每一项形如 {kind, id}。")

    if len(raw) > MAX_REFS:
        raise ReferenceInvalidError(
            f"一次最多带 {MAX_REFS} 个引用，这次给了 {len(raw)} 个。"
        )

    specs: list[RefSpec] = []
    seen: set[tuple[str, str, str]] = set()

    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ReferenceInvalidError(f"第 {index} 个引用不是对象。")

        kind = str(item.get("kind") or "").strip().lower()
        if kind not in REF_KINDS:
            raise ReferenceInvalidError(
                f"第 {index} 个引用的类型不认识：{kind or '空'}。"
                f"只支持 {' / '.join(REF_KINDS)}。"
            )

        ref_id = str(item.get("id") or "").strip()
        if not ref_id:
            raise ReferenceInvalidError(f"第 {index} 个引用缺少 id。")
        if len(ref_id) > REF_ID_LIMIT:
            raise ReferenceInvalidError(f"第 {index} 个引用的 id 过长。")

        section = str(item.get("section") or "").strip()[:SECTION_LIMIT]
        key = (kind, ref_id, section)
        if key in seen:
            continue
        seen.add(key)
        specs.append(RefSpec(kind=kind, ref_id=ref_id, section=section))

    return tuple(specs)


# ---------------------------------------------------------------- 解析


async def resolve_refs(
    specs: tuple[RefSpec, ...],
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str | None,
    scopes: tuple[str, ...] | list[str] = (),
    roles: tuple[str, ...] | list[str] = (),
) -> tuple[ResolvedRef, ...]:
    """逐个解析。任何一个不过就抛出去，由调用方拒绝整单。"""
    resolved: list[ResolvedRef] = []

    for spec in specs:
        if spec.kind == REF_ARTIFACT:
            resolved.append(
                await _resolve_artifact(
                    spec,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            )
        elif spec.kind == REF_MESSAGE:
            resolved.append(
                await _resolve_message(spec, tenant_id=tenant_id, user_id=user_id)
            )
        elif spec.kind == REF_KNOWLEDGE:
            resolved.append(
                await _resolve_knowledge(
                    spec,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    scopes=scopes,
                    roles=roles,
                )
            )
        else:
            resolved.append(_resolve_entity(spec))

    return tuple(resolved)


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


async def _resolve_artifact(
    spec: RefSpec,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str | None,
) -> ResolvedRef:
    """产物：同租户 + 同 user + 同会话。

    **会话这一条是 P3-c 加的**，不是原来就有的。原来写的是「不额外限
    conversation：A 与 B 继承同一个会话，限它挡不住任何东西」——那句话
    对委派成立（子调用继承发起方的 `conversation_id`，本来就在同一个会话
    里），但它挡住了另一件事：产物现在有了归属，绑了会话的产物不该在
    别的会话里被引用。

    `conversation_id is None` 的口子留给**历史上传的通用材料**：它们上传时
    没绑会话，收紧会让存量附件一起失效。新产物一律带会话（`save_artifact`
    拿不到会话就直接拒），所以这个口子不会越开越大。
    """
    target = _as_uuid(spec.ref_id)
    if target is None:
        raise ReferenceGoneError(
            f"产物引用无效：{spec.ref_id} 不是合法标识。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    async with async_session_factory() as session:
        with tenant_scope(tenant_id):
            row = await session.get(Artifact, target)

    if row is None or row.tenant_id != tenant_id:
        raise ReferenceGoneError(
            f"产物已不存在：{spec.ref_id}。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    if row.user_id != user_id:
        raise ReferenceNotVisibleError(
            "这份产物不属于当前用户，不能转交给别的智能体。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    # 绑了会话的产物只在那个会话里可用。同一会话的后续轮次、以及委派出去
    # 的子调用都算在内（`conversation_id` 沿委派链原样继承）。
    if (
        conversation_id
        and row.conversation_id
        and row.conversation_id != conversation_id
    ):
        raise ReferenceNotVisibleError(
            "这份产物属于另一个会话，不能在这里引用。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    size_kb = row.size_bytes / 1024
    detail = f"《{row.filename}》 {row.content_type} {size_kb:.1f}KB"

    attachment = await _materialize(row, tenant_id=tenant_id, conversation_id=conversation_id)
    if attachment is None:
        detail += "（未随任务附带文件内容）"

    return ResolvedRef(
        kind=spec.kind,
        ref_id=spec.ref_id,
        detail=detail,
        attachment=attachment,
    )


async def _materialize(
    row: Artifact,
    *,
    tenant_id: str,
    conversation_id: str | None,
) -> dict[str, Any] | None:
    """把产物复制成子调用会话里的附件，让它能被 `ocr_document` 读。

    为什么是复制而不是把对象路径交给 B：路径不是权限（`object_store.py` 开头就写着）。
    子调用的附件读取只认「本租户 + 本会话目录里的句柄」，直接把键递过去，
    B 拿到的会是一个读不了的值，而报错信息是「附件不存在」——比不给还难查。

    复制有成本：同一份 PDF 转交三次就是三份文件。但它落在会话目录下，
    会话清理会连目录一起回收，不会变成无人认领的垃圾。
    """
    if not conversation_id:
        return None

    mime = (row.content_type or "").lower().split(";")[0].strip()

    # 只有读取工具认得的类型才值得复制过去。其它类型复制了也没人能用，
    # 反而会让下游以为「有附件就一定能读」。
    # 上限用 attachment_store 的同一套判据，别在这里另抄一份 if/else：
    # 加第三种附件类型时，抄出来的分支必然漏改一处。
    if mime not in attachment_store._ALLOWED_MIME:  # noqa: SLF001 - 同一份白名单
        return None
    if row.size_bytes > attachment_store.max_bytes_for(
        mime,
        max_image_bytes=settings.ocr_max_image_bytes,
        max_pdf_bytes=settings.ocr_max_pdf_bytes,
    ):
        return None

    try:
        raw = await get_object_store().read(row.object_key)
    except ObjectNotFound:
        raise ReferenceGoneError(
            f"产物文件已丢失：{row.filename}。",
            kind=REF_ARTIFACT,
            ref_id=str(row.id),
        ) from None
    except ObjectStoreError:
        logger.exception(
            "读取产物失败，无法随任务转交。",
            extra={"event": "ref_artifact_read_failed", "artifact_id": str(row.id)},
        )
        raise ReferenceGoneError(
            f"产物文件暂时读不到：{row.filename}。",
            kind=REF_ARTIFACT,
            ref_id=str(row.id),
        ) from None

    try:
        stored = attachment_store.save_bytes(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            raw=raw,
            mime=mime,
            name=row.filename,
        )
    except attachment_store.AttachmentError:
        # 大小或类型被附件层拒了：这不影响引用本身成立，退化成「只给句柄」。
        logger.warning(
            "产物无法作为附件转交，退化为只传元数据。",
            extra={"event": "ref_artifact_attach_failed", "artifact_id": str(row.id)},
        )
        return None

    return {
        "attachment_id": stored.attachment_id,
        "kind": stored.kind,
        "mime": stored.mime,
        "size": stored.size,
        "name": stored.name,
    }


async def _resolve_message(
    spec: RefSpec,
    *,
    tenant_id: str,
    user_id: str,
) -> ResolvedRef:
    """会话消息：同租户 + 同会话 + 同 user_id。

    会话所有者取自 `conversations.user_id`，不是消息本身——`messages` 上没有
    用户列，而「这条消息属于谁」唯一可靠的答案在它所属的会话上。
    """
    target = _as_uuid(spec.ref_id)
    if target is None:
        raise ReferenceGoneError(
            f"消息引用无效：{spec.ref_id} 不是合法标识。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    async with async_session_factory() as session:
        with tenant_scope(tenant_id):
            row = await session.get(Message, target)
            conversation = (
                await session.get(Conversation, row.conversation_id) if row else None
            )

    if row is None or conversation is None:
        raise ReferenceGoneError(
            "这条消息已不存在（会话可能已被清理）。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    if conversation.user_id != user_id:
        raise ReferenceNotVisibleError(
            "这条消息属于别人的会话，不能转交。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    role = getattr(row.role, "value", row.role)
    text = (row.content or "").strip()
    excerpt = text[:MESSAGE_LIMIT]
    if len(text) > MESSAGE_LIMIT:
        excerpt += "…（已截断）"

    return ResolvedRef(
        kind=spec.kind,
        ref_id=spec.ref_id,
        detail=f"（{role}）{excerpt}",
    )


async def _resolve_knowledge(
    spec: RefSpec,
    *,
    tenant_id: str,
    user_id: str,
    scopes: tuple[str, ...] | list[str],
    roles: tuple[str, ...] | list[str],
) -> ResolvedRef:
    """知识文档：`access_scope` ⊆ B 的 scope，再叠 `visibility` 与 `allowed_roles`。

    判定顺序与 `retriever.search()` 一致：先分级、再 ACL。反过来写会让
    「角色不允许」表现为「分级不够」，排障时改错地方。
    """
    target = _as_uuid(spec.ref_id)
    if target is None:
        raise ReferenceGoneError(
            f"知识文档引用无效：{spec.ref_id} 不是合法标识。",
            kind=spec.kind,
            ref_id=spec.ref_id,
        )

    async with async_session_factory() as session:
        with tenant_scope(tenant_id):
            row = await session.get(Document, target)

        if row is None or row.tenant_id != tenant_id:
            raise ReferenceGoneError(
                f"知识文档已不存在：{spec.ref_id}。",
                kind=spec.kind,
                ref_id=spec.ref_id,
            )

        status = getattr(row.status, "value", row.status)
        if status == DocumentStatus.DELETED.value:
            raise ReferenceGoneError(
                f"知识文档已下架：{row.title}。",
                kind=spec.kind,
                ref_id=spec.ref_id,
            )
        if status != DocumentStatus.READY.value:
            raise ReferenceGoneError(
                f"知识文档还没就绪（{status}）：{row.title}。",
                kind=spec.kind,
                ref_id=spec.ref_id,
            )

        acl = acl_of(row)
        level = str(acl.get("access_scope") or "internal")
        if level not in allowed_scopes_for(scopes):
            raise ReferenceNotVisibleError(
                f"无权引用知识文档《{row.title}》：它的分级是 {level}。",
                kind=spec.kind,
                ref_id=spec.ref_id,
                missing_scope=_SCOPE_FOR_LEVEL.get(level, "knowledge.read"),
            )

        if str(acl.get("visibility") or "tenant_shared") == "user_private":
            owner = str(acl.get("owner_user_id") or "")
            if owner != user_id:
                raise ReferenceNotVisibleError(
                    f"知识文档《{row.title}》只对上传者本人可见。",
                    kind=spec.kind,
                    ref_id=spec.ref_id,
                )

        required_roles = [str(item) for item in (acl.get("allowed_roles") or [])]
        if required_roles and not set(required_roles) & set(roles):
            raise ReferenceNotVisibleError(
                f"知识文档《{row.title}》限角色访问：{'/'.join(required_roles)}。",
                kind=spec.kind,
                ref_id=spec.ref_id,
            )

        quote = ""
        if spec.section:
            quote = await _section_quote(
                session,
                tenant_id=tenant_id,
                document_id=str(row.id),
                section=spec.section,
            )

    detail = f"《{row.title}》"
    if spec.section:
        detail += f" 第 {spec.section} 节"
    if quote:
        detail += f" 摘录：{quote}"

    return ResolvedRef(kind=spec.kind, ref_id=spec.ref_id, detail=detail)


async def _section_quote(
    session: Any,
    *,
    tenant_id: str,
    document_id: str,
    section: str,
) -> str:
    """取指定小节的短摘录。

    小节号对不上（重新切片后编号变了）不算失败：引用仍然有效，
    B 自己去检索这一份文档即可。这里静默返回空串是**安全的**，
    因为它影响的是「附赠的摘录」，不是「这份材料是否可见」——
    后者已经在上面判过了，失败会抛。
    """
    for chunk in await chunks_of(session, tenant_id, document_id, limit=200):
        if not chunk.is_active:
            continue
        if section and section in (chunk.section or ""):
            text = chunk.content.strip()
            return text[:QUOTE_LIMIT] + ("…" if len(text) > QUOTE_LIMIT else "")
    return ""


def _resolve_entity(spec: RefSpec) -> ResolvedRef:
    """业务对象：本层不判可见性。

    站点授权在业务工具那一侧（`station_id` 能不能看，只有它知道）。
    在这里再判一次，会得到两套授权，而它们早晚不一致——
    一致的时候没人发现，不一致的时候表现成「引用过了、调用却被拒」。
    """
    return ResolvedRef(
        kind=spec.kind,
        ref_id=spec.ref_id,
        detail=f"{spec.ref_id}（能否查看由对应业务工具按站点授权判定）",
    )


# ---------------------------------------------------------------- 出口


def render_materials(refs: tuple[ResolvedRef, ...]) -> str:
    """给子调用的材料清单。

    最后一句必须有：模型天然会把「拿到的材料」当成「全部背景」，
    不写清楚它会把一份单据答成一份完整报告。
    """
    if not refs:
        return ""

    lines: list[str] = []
    for ref in refs:
        label = _LABELS.get(ref.kind, ref.kind)
        line = f"- {label}（{ref.kind}:{ref.ref_id}）{ref.detail}"
        if ref.attachment is not None:
            line += (
                f"；file_id={ref.attachment['attachment_id']}"
                "（要读里面的文字就调用 ocr_document）"
            )
        lines.append(line)

    return (
        "[材料]\n"
        "发起方为这次任务显式提供了下面这些材料，它们不是整个对话：\n"
        + "\n".join(lines)
        + "\n未列出的内容不要假设其存在；需要更多请直接说明。"
    )


def audit_summary(refs: tuple[ResolvedRef, ...]) -> list[dict[str, str]]:
    """审计里只留 kind 与 id。

    与 `task_sha256` 同一条理由：摘录里可能带着业务细节，而审计是长期账。
    """
    return [{"kind": ref.kind, "id": ref.ref_id} for ref in refs]


def summary_of_specs(specs: tuple[RefSpec, ...]) -> list[dict[str, str]]:
    """解析失败时也得记下「传了些什么」——被拒的那一次往往最值得查。"""
    return [{"kind": spec.kind, "id": spec.ref_id} for spec in specs]
