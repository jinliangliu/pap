# app/services/conversation_purge.py
"""会话销毁：一次对话会在四处留下痕迹，只删一处不算删。

`DELETE /conversations/{id}` 原先只删 `conversations` 与 `messages`，其余三处
仍能把对话原样还原出来：

1. `agent_runs.input_summary` / `output_summary`——用户原话与回答的前 2000 字。
   `conversation_id` 外键是 `ON DELETE SET NULL`，删会话后记录还在，只是失去归属；
2. LangGraph 的 checkpoint 三张表——图状态里是完整 messages、attachments、
   tool_observations（含 OCR 识别出的正文）。线上实测：1 个会话对应 49 个线程残留；
3. 磁盘——用户上传的原始文件（附件目录）与会话产物（对象存储）。

第 1 项随业务事务一起删（可回滚）；本模块在事务提交之后处理第 2、3 项。
顺序不能反过来：文件与图状态删掉就回不来了，而业务事务仍可能失败。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.db.checkpoint import get_checkpointer
from app.services import attachment_store
from app.storage.object_store import ObjectStoreError, get_object_store

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PurgeReport:
    """清理结果。逐项独立，一项失败不阻断其余项。"""

    checkpoints: bool = False
    artifact_files: int = 0
    attachment_files: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


async def _purge_thread(thread_id: str) -> None:
    """删掉该线程的全部图状态。

    用 `get_checkpointer()` 而非 `init_checkpointer()`：应用已在 lifespan 里初始化过，
    这里不该再建一个连接池；没初始化就说明装配有误，应当报错而不是悄悄新建。
    """
    saver = get_checkpointer()
    await saver.adelete_thread(thread_id)  # type: ignore[attr-defined]


async def _purge_artifacts(keys: Sequence[str]) -> int:
    """删对象存储里的会话产物，返回成功删除的个数。"""
    store = get_object_store()
    removed = 0

    for key in keys:
        try:
            await store.delete(key)
        except (ObjectStoreError, OSError):
            # 单个对象删不掉不影响其余对象，签名无效的键更不该让整次销毁失败。
            logger.warning(
                "删除会话产物失败。",
                extra={"event": "conversation_artifact_purge_failed", "object_key": key},
            )
            continue

        removed += 1

    return removed


async def purge_conversation_traces(
    *,
    tenant_id: str,
    conversation_id: str,
    thread_id: str | None,
    artifact_keys: Sequence[str] = (),
) -> PurgeReport:
    """清理会话在图状态、对象存储、附件目录里的残留。

    `thread_id` 传 `None` 表示该线程还被别的会话引用，不能删——否则会把
    仍在使用的对话上下文一起抹掉。正常情况下一会话一线程，调用方负责判断。
    """
    report = PurgeReport()

    if thread_id:
        try:
            await _purge_thread(thread_id)
            report.checkpoints = True
        except Exception:  # noqa: BLE001  清理失败不该让用户看到 500
            logger.warning(
                "清理会话图状态失败。",
                extra={
                    "event": "conversation_checkpoint_purge_failed",
                    "conversation_id": conversation_id,
                    "thread_id": thread_id,
                },
            )
            report.failures.append("checkpoints")

    if artifact_keys:
        try:
            report.artifact_files = await _purge_artifacts(artifact_keys)
        except Exception:  # noqa: BLE001
            logger.warning(
                "清理会话产物失败。",
                extra={
                    "event": "conversation_artifact_purge_failed",
                    "conversation_id": conversation_id,
                },
            )
            report.failures.append("artifacts")

    try:
        # rmtree 是阻塞调用，PDF 多了以后同步执行会卡住事件循环。
        report.attachment_files = await asyncio.to_thread(
            attachment_store.purge_conversation,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "清理会话附件失败。",
            extra={
                "event": "conversation_attachment_purge_failed",
                "conversation_id": conversation_id,
            },
        )
        report.failures.append("attachments")

    return report
