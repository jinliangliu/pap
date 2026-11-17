# app/api/routes/studio.py
"""Agent Studio 的文件导入接口。

一个入口覆盖三类目标（知识库 / 技能 / 工具）：文件本身就是判断依据，
人不需要先想「这个文件该传到哪个页面」。所有写入都转手给
`app.studio.importer`，这里只负责三件事：

- 收文件（含大小上限的流式读取）
- 把 `ImportDenied` / `ImportRejected` 翻成 403 / 400
- 把表单里的字符串字段读成结构化选项

**控制台的入口已经合并进构建助手**（加号 + 一句话 → `studio_import` 工具），
这条 HTTP 形态保留给脚本与 `dry_run` 预览；两边调的是同一个 `run_import`。

`dry_run=true` 走的是**同一个**解析函数，只是不写库——预览与实际导入
各写一套解析，迟早会出现「预览显示 3 个接口、实际存进去 1 个」。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import get_session
from app.studio import importer
from app.studio.parsers import TARGET_AUTO, StudioParseError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/studio", tags=["studio"])


@router.get("/import/targets")
async def list_targets(
    identity: Identity = Depends(get_current_identity),
) -> dict[str, Any]:
    """前端拿它渲染下拉与提示文案，别在 JS 里再抄一份枚举。"""
    return {
        "targets": [
            {
                "value": TARGET_AUTO,
                "label": "自动识别",
                "hint": "按内容判断：带 front matter 的 Markdown 是技能，OpenAPI 是工具，其余文档进知识库。",
            },
            {
                "value": "knowledge",
                "label": "知识库文档",
                "hint": "PDF / Word / Markdown / 文本 / HTML。",
                "accepts": [".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm"],
                "requires": "knowledge:write",
            },
            {
                "value": "skill",
                "label": "技能",
                "hint": "带 YAML front matter 的 .md（name/description/trigger_keywords），或技能导出 JSON。",
                "accepts": [".md", ".markdown", ".json", ".yaml", ".yml"],
                "requires": "skill:write",
            },
            {
                "value": "tool",
                "label": "工具接口",
                "hint": "OpenAPI 3 / Swagger 2 的 .json/.yaml，每个 operation 生成一个 HTTP 工具；也收工具导出 JSON。",
                "accepts": [".json", ".yaml", ".yml"],
                "requires": "平台管理员",
            },
        ],
        "max_file_bytes": settings.knowledge_max_file_bytes,
        "is_platform_admin": identity.is_platform_admin,
    }


@router.post("/import")
async def import_file(
    file: UploadFile = File(...),
    target: str = Form(default=TARGET_AUTO),
    dry_run: bool = Form(default=False),
    options: str | None = Form(default=None),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """从一份文件导入知识库文档 / 技能 / 工具。

    `options` 是一个 JSON 字符串（multipart 里没有嵌套对象）：

    - knowledge：`{"title": "...", "access_scope": "internal"}`
    - skill：`{"name": "...", "overwrite": true}`
    - tool：`{"base_url": "...", "operations": ["name1"], "mode": "merge"}`
    """
    raw = await _read_limited(file)

    try:
        return await importer.run_import(
            session=session,
            identity=identity,
            filename=file.filename or "",
            raw=raw,
            target=target,
            options=_parse_options(options),
            dry_run=dry_run,
        )
    except importer.ImportDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (importer.ImportRejected, StudioParseError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 写完之后读不回来之类的问题：不是调用方能修的输入。
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _read_limited(file: UploadFile) -> bytes:
    """按上限流式读取。

    先读完全部再判大小，等于让一个 2GB 的文件先把进程撑爆再告诉人「太大了」。
    """
    limit = settings.knowledge_max_file_bytes
    buffer = bytearray()

    while True:
        chunk = await file.read(1 << 20)

        if not chunk:
            break

        buffer.extend(chunk)

        if len(buffer) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {limit // (1024 * 1024)} MB 上限。",
            )

    if not buffer:
        raise HTTPException(status_code=400, detail="文件是空的。")

    return bytes(buffer)


def _parse_options(raw: str | None) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"options 不是合法的 JSON：{exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="options 必须是一个 JSON 对象。")

    return payload
