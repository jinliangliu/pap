# app/tools/builtins/artifact_tools.py
"""把对话里生成的东西存成产物。

为什么需要这个工具：`artifacts` 表原来只有用户上传会写，于是「上一轮生成
的那张表」根本没有实体——它只是回答里的一段文本，下一轮既引用不了，
也下载不到。这个工具补的是「生成方也写 artifacts」这一半。

产物**一律绑当前会话**（`conversation_id`）：

- 同一会话的后续轮次能引用它。换话题也一样，这正是它存在的理由；
- 委派出去的子调用也看得到：`conversation_id` 沿委派链原样继承
  （`delegate_tools` 构造子 state 时传的就是发起方的那个），所以不用
  再为「子会话的产物算谁的」单独设计；
- 会话删除时对象随元数据一起回收，不留孤儿。

跨会话取用要另设保留期与清理，那是另一个产品决策，现在不做。

只收文本产物、且按扩展名固定 MIME：让模型自己填 `content_type` 等于让
它决定下载时怎么被浏览器解释，而上传 HTML/SVG 这条路线上游已经封死
（`routes/artifacts.py` 的 `_NEVER_INLINE`），工具这边不能开个新口子。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.approval import RiskLevel
from app.models.artifact import Artifact
from app.models.audit import AuditEventType
from app.services.audit_service import write_audit_event
from app.storage.object_store import (
    ObjectStoreError,
    ObjectTooLarge,
    get_object_store,
    safe_filename,
    safe_suffix,
)
from app.tenancy.context import tenant_scope
from app.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionResult,
)

# 扩展名 → MIME。只收能当文本读的几种：产物是给人看的结论与数据，
# 不是可执行内容。表里没有的后缀一律拒，而不是猜一个通用的
# `application/octet-stream`——那会让「保存成功」变成「下载下来打不开」。
_TEXT_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
}

_ALLOWED_SUFFIXES = "、".join(sorted(_TEXT_TYPES))


def _fail(code: str, message: str, **extra: Any) -> ToolExecutionResult:
    return ToolExecutionResult(
        success=False,
        error_code=code,
        message=message,
        result={"error_code": code, **extra},
    )


async def save_artifact(
    context: ToolExecutionContext,
    arguments: dict[str, Any],
) -> ToolExecutionResult:
    """保存一份文本产物，返回它的 `artifact_id`。"""
    # 没有会话就没有「按会话保留」可言。宁可拒绝，也不落一条无主的产物——
    # 无主的产物在列表里分不清是历史上传还是谁的临时文件，清理时也没依据。
    if not context.conversation_id:
        return _fail(
            "ARTIFACT_NO_CONVERSATION",
            "当前调用没有归属会话，无法保存产物。",
        )

    raw_name = str(arguments.get("filename") or "").strip()
    content = arguments.get("content")

    if not isinstance(content, str) or not content.strip():
        return _fail("ARTIFACT_EMPTY", "产物内容为空，未保存。")

    name = safe_filename(raw_name, fallback="artifact.md")
    mime = _TEXT_TYPES.get(Path(name).suffix.lower())

    if mime is None:
        return _fail(
            "ARTIFACT_TYPE_UNSUPPORTED",
            f"只能保存文本类产物（{_ALLOWED_SUFFIXES}）。文件名后缀需要是其中之一。",
            filename=name,
        )

    raw = content.encode("utf-8")

    # 先自己比一次上限，才能给出「差多少」这种可操作的话；
    # 对象存储那一层还会再拦一次（并发与配置变化时以它为准）。
    if len(raw) > settings.artifact_max_bytes:
        return _fail(
            "ARTIFACT_TOO_LARGE",
            f"产物 {len(raw) / 1024:.0f}KB，超过单份上限 "
            f"{settings.artifact_max_bytes / 1024:.0f}KB。请拆分后分别保存。",
            filename=name,
            size_bytes=len(raw),
        )

    artifact_id = uuid.uuid4()
    store = get_object_store()
    key = store.build_key(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        artifact_id=artifact_id,
        suffix=safe_suffix(name),
    )

    try:
        size, digest = await store.put(key, raw)
    except ObjectTooLarge:
        return _fail(
            "ARTIFACT_TOO_LARGE",
            f"产物超过单份上限 {settings.artifact_max_bytes / 1024:.0f}KB。",
            filename=name,
        )
    except ObjectStoreError:
        return _fail("ARTIFACT_STORE_ERROR", "产物写入失败，未保存。", filename=name)

    try:
        with tenant_scope(context.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    session.add(
                        Artifact(
                            id=artifact_id,
                            tenant_id=context.tenant_id,
                            user_id=context.user_id,
                            # 绑会话：这条是「按会话保留」的全部实现，
                            # 其余（可见范围、删除回收）都以它为前提。
                            conversation_id=context.conversation_id,
                            object_key=key,
                            filename=name,
                            content_type=mime,
                            size_bytes=size,
                            sha256=digest,
                        )
                    )

                    await write_audit_event(
                        session,
                        event_type=AuditEventType.ARTIFACT_CREATED,
                        tenant_id=context.tenant_id,
                        actor_id=context.user_id,
                        resource_type="artifact",
                        resource_id=str(artifact_id),
                        thread_id=context.thread_id,
                        run_id=context.run_id,
                        success=True,
                        message=f"智能体保存了产物《{name}》",
                        metadata={
                            "via": "save_artifact",
                            "filename": name,
                            "content_type": mime,
                            "size_bytes": size,
                            "conversation_id": context.conversation_id,
                            "agent_name": context.agent_name,
                        },
                    )
    except Exception:
        # 元数据没落住就把对象删掉，否则留下一个「不可达但占空间」的文件。
        # 与上传接口同一处理，理由也一样。
        await store.delete(key)
        raise

    return ToolExecutionResult(
        success=True,
        verified=True,
        result={
            "artifact_id": str(artifact_id),
            "filename": name,
            "content_type": mime,
            "size_bytes": size,
            "conversation_id": context.conversation_id,
        },
        message=f"已保存产物《{name}》。后续可用 artifact_id 引用它。",
        verification={"source": "artifact_store", "sha256": digest},
    )


def build_artifact_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="save_artifact",
            display_name="保存产物",
            description=(
                "把生成好的报告、表格或数据存成一份文件，返回 artifact_id。"
                f"支持 {_ALLOWED_SUFFIXES}。同一会话后续可用这个 id 引用它。"
            ),
            kind="python",
            risk_level=RiskLevel.NORMAL,
            required_scopes=("tools:read",),
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 120,
                        "description": f"带后缀的文件名，如 巡检报告.md。后缀限 {_ALLOWED_SUFFIXES}。",
                    },
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "完整文件内容，按 UTF-8 保存。",
                    },
                },
                "required": ["filename", "content"],
                "additionalProperties": False,
            },
            handler=save_artifact,
            verification_hint="返回 artifact_id 与 sha256；会话内可用该 id 引用。",
        ),
    ]
