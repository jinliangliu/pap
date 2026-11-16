# app/studio/importer.py
"""把一份上传文件变成「库里的东西」。

三条路，各自复用已有的写入实现，**不在这里重新实现一遍入库规则**：

- 知识库 → `app.knowledge.upload.ingest_upload`（与页面上传、MCP 上传同一份）
- 技能   → `skill_service.upsert_imported_skills`（与技能页导入同一份）
- 工具   → `custom_tool_service.import_tools`（与工具页导入同一份）

这个模块只做三件它们不做的事：判断「这份文件该建成什么」、把解析结果整理成
可预览的形状、按目标做权限判定。任何一条入库规则如果想在这里再写一遍，
就会出现「页面上传能过、导入不能过」这类只在一条路径上复现的差别。

`dry_run` 与真正写入走同一个函数：预览必须由真正要写的那份解析产生，
两套解析迟早会分叉——预览显示 3 个参数、实际存进去 0 个，是最难查的一类问题。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Identity
from app.core.config import settings
from app.knowledge.ingest import extract_text_from_bytes
from app.knowledge.upload import (
    UploadDenied,
    UploadRejected,
    can_write_knowledge,
    ingest_upload,
    precheck_upload,
)
from app.models.audit import AuditEventType
from app.schemas.custom_tool import CustomToolCreate, CustomToolImportRequest
from app.services import audit_service, custom_tool_service, skill_service
from app.studio import parsers
from app.studio.parsers import (
    TARGET_AUTO,
    TARGET_KNOWLEDGE,
    TARGET_SKILL,
    TARGET_TOOL,
    TARGETS,
    StudioParseError,
)
from app.tenancy.context import bind_tenant, tenant_scope, unbind_tenant
from app.tools.dependencies import get_tool_gateway

logger = logging.getLogger(__name__)

# 与 `app/api/routes/skills.py` 里的 WRITE_PERMISSION 同一枚 scope。
# 不 import 那个常量：路由模块不该被服务层反向依赖。
SKILL_WRITE_PERMISSION = "skill:write"


class ImportDenied(Exception):
    """权限不足。映射 403——调用方该去申请权限，而不是换文件。"""


class ImportRejected(Exception):
    """输入问题。映射 400——换文件或改参数就能继续。"""


def _can_write_skills(identity: Identity) -> bool:
    return identity.is_platform_admin or SKILL_WRITE_PERMISSION in identity.scopes


async def run_import(
    *,
    session: AsyncSession,
    identity: Identity,
    filename: str,
    raw: bytes,
    target: str = TARGET_AUTO,
    options: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """解析一份文件，按需写入。``dry_run`` 只读不写，返回同一份预览。"""
    opts = options or {}

    if not raw:
        raise ImportRejected("文件是空的。")

    limit = settings.knowledge_max_file_bytes

    if len(raw) > limit:
        raise ImportRejected(f"文件超过 {limit // (1024 * 1024)} MB 上限。")

    requested = (target or TARGET_AUTO).strip().lower()

    if requested != TARGET_AUTO and requested not in TARGETS:
        raise ImportRejected(
            f"未知的导入目标：{requested}。可选 {TARGET_AUTO} / {', '.join(TARGETS)}。"
        )

    detected = parsers.detect_target(filename, raw)
    resolved = detected if requested == TARGET_AUTO else requested

    payload: dict[str, Any] = {
        "filename": filename,
        "size_bytes": len(raw),
        "target": resolved,
        # 识别结果单独回传：前端要能显示「它自己认成了什么」，
        # 人改过目标之后还能看出原来的判断。
        "detected_target": detected,
        "dry_run": dry_run,
    }

    if resolved == TARGET_KNOWLEDGE:
        payload.update(
            await _knowledge(
                session=session,
                identity=identity,
                filename=filename,
                raw=raw,
                options=opts,
                dry_run=dry_run,
            )
        )
        return payload

    if resolved == TARGET_SKILL:
        payload.update(
            await _skill(
                session=session,
                identity=identity,
                filename=filename,
                raw=raw,
                options=opts,
                dry_run=dry_run,
            )
        )
        return payload

    payload.update(
        await _tool(
            session=session,
            identity=identity,
            filename=filename,
            raw=raw,
            options=opts,
            dry_run=dry_run,
        )
    )
    return payload


# --------------------------------------------------------------------------- #
# 知识库
# --------------------------------------------------------------------------- #


async def _knowledge(
    *,
    session: AsyncSession,
    identity: Identity,
    filename: str,
    raw: bytes,
    options: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    if not can_write_knowledge(identity):
        raise ImportDenied("上传文档需要 knowledge:write 权限。")

    try:
        # 先只校验请求：后缀不对不该等到把 5MB 读进内存之后才报。
        scope = precheck_upload(
            identity=identity,
            filename=filename,
            access_scope=options.get("access_scope"),
        )
    except UploadDenied as exc:
        raise ImportDenied(str(exc)) from exc
    except UploadRejected as exc:
        raise ImportRejected(str(exc)) from exc

    if dry_run:
        try:
            text = extract_text_from_bytes(raw, filename)
        except Exception as exc:  # 损坏的 PDF/Word
            raise ImportRejected(f"文件无法解析：{exc}") from exc

        title = str(options.get("title") or "").strip()

        return {
            "notes": [],
            "preview": {
                "title": title or _default_title(filename),
                "access_scope": scope,
                "chars": len(text),
                # 不预估切片数：那要真跑一遍切片器，估出来的数字
                # 与实际入库对不上时，人会以为自己传错了文件。
                "excerpt": text.strip()[:400],
            },
        }

    try:
        outcome = await ingest_upload(
            session=session,
            identity=identity,
            filename=filename,
            raw=raw,
            title=options.get("title"),
            access_scope=scope,
        )
    except UploadDenied as exc:
        raise ImportDenied(str(exc)) from exc
    except UploadRejected as exc:
        raise ImportRejected(str(exc)) from exc

    row = outcome.row

    return {
        "notes": [],
        "result": {
            "action": outcome.action,
            "document_id": row.id,
            "title": row.title,
            "chunk_count": row.chunk_count,
            "access_scope": row.access_scope,
            "message": {
                "created": f"已入库 {row.chunk_count} 片",
                "updated": f"内容有变化，已重建 {row.chunk_count} 片",
                "unchanged": "同样的内容已在库里，没有重复入库",
            }.get(outcome.action, outcome.action),
        },
    }


def _default_title(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1]

    for suffix in (".md", ".markdown", ".txt", ".pdf", ".docx", ".html", ".htm"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break

    return stem


# --------------------------------------------------------------------------- #
# 技能
# --------------------------------------------------------------------------- #


async def _skill(
    *,
    session: AsyncSession,
    identity: Identity,
    filename: str,
    raw: bytes,
    options: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    if not _can_write_skills(identity):
        raise ImportDenied(f"导入技能需要 {SKILL_WRITE_PERMISSION} 权限。")

    entries, notes, single = _skill_entries(filename, raw)

    if options.get("name") and single is not None:
        try:
            single.name = skill_service.normalize_name(str(options["name"]))
        except skill_service.SkillError as exc:
            raise ImportRejected(str(exc)) from exc
        entries = [draft_to_entry(single)]

    with tenant_scope(identity.tenant_id):
        existing = {
            str(entry.get("name")): await skill_service.get_by_name(
                session, str(entry.get("name"))
            )
            for entry in entries
        }

        if dry_run:
            preview = []

            for entry in entries:
                row = existing.get(str(entry.get("name")))
                preview.append(
                    {
                        "name": entry.get("name"),
                        "display_name": entry.get("display_name") or entry.get("name"),
                        "description": entry.get("description") or "",
                        "trigger_keywords": entry.get("trigger_keywords") or [],
                        "allowed_tools": entry.get("allowed_tools") or [],
                        "priority": entry.get("priority", 100),
                        "enabled": entry.get("enabled", True),
                        "instruction_chars": len(str(entry.get("instruction") or "")),
                        "instruction_head": str(entry.get("instruction") or "")[:600],
                        "exists": row is not None,
                    }
                )

            return {
                "notes": notes,
                "preview": {"skills": preview, "count": len(preview)},
            }

        overwrite = bool(options.get("overwrite", True))
        skipped = [
            name for name, row in existing.items() if row is not None and not overwrite
        ]

        if skipped:
            # 不覆盖时把整批都跳过的语义交给人看，而不是悄悄只建新的。
            notes = notes + [f"同名技能已存在且选择了不覆盖：{'、'.join(skipped)}"]

        to_write = [
            entry for entry in entries if overwrite or existing.get(str(entry.get("name"))) is None
        ]

        created = updated = 0

        if to_write:
            created, updated = await skill_service.upsert_imported_skills(
                session,
                to_write,
                created_by=identity.user_id,
            )

            await audit_service.write_audit_event(
                session,
                event_type=AuditEventType.ADMIN_ACTION,
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_type="skill",
                resource_id=",".join(str(entry.get("name")) for entry in to_write)[:200],
                message="import_file",
                metadata={
                    "operation": "import",
                    "actor_subject": identity.subject,
                    "filename": filename,
                    "created": created,
                    "updated": updated,
                    "skipped": len(skipped),
                    "detail": "",
                },
            )

        await session.commit()

    return {
        "notes": notes,
        "result": {
            "action": "imported" if created or updated else "skipped",
            "created": created,
            "updated": updated,
            "skipped": len(skipped),
            "message": f"新建 {created} 个，覆盖 {updated} 个"
            + (f"，跳过 {len(skipped)} 个同名" if skipped else ""),
        },
    }


def _skill_entries(
    filename: str, raw: bytes
) -> tuple[list[dict[str, Any]], list[str], parsers.SkillDraft | None]:
    """返回 (待写入条目, notes, 单文件草稿)。

    `.md` 走文件即技能；`.json` 只可能是平台自己导出的技能包——
    后者不带草稿，因为一份包里可能有好几个技能，没有「改那一个名字」的说法。
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if suffix in {"md", "markdown"}:
        draft = parsers.parse_skill_markdown(filename, raw.decode("utf-8", errors="ignore"))
        return [draft_to_entry(draft)], draft.notes, draft

    if suffix in {"json", "yaml", "yml"}:
        bundle = parsers.parse_skill_export(parsers.load_structured(raw))
        return bundle.entries, bundle.notes, None

    raise ImportRejected("技能文件应当是 .md（front matter + 正文）或技能导出 JSON。")


def draft_to_entry(draft: parsers.SkillDraft) -> dict[str, Any]:
    """把草稿摊平成与技能导出文件一致的形状。

    走导出文件那条格式而不是直接传 SkillDraft，是为了让「文件导入」和
    「导出再导入」落到同一个函数里——两条路各有一套字段映射，
    改了一边忘了另一边时，症状是「导出的技能再导回来少个触发词」。
    """
    return {
        "name": draft.name,
        "display_name": draft.display_name,
        "description": draft.description,
        "instruction": draft.instruction,
        "trigger_keywords": draft.trigger_keywords,
        "allowed_tools": draft.allowed_tools,
        "priority": draft.priority,
        "enabled": draft.enabled,
    }


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


async def _tool(
    *,
    session: AsyncSession,
    identity: Identity,
    filename: str,
    raw: bytes,
    options: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    if not identity.is_platform_admin:
        raise ImportDenied("新增工具接口是平台级操作，需要平台管理员权限。")

    drafts, notes, base_url, bundle = _tool_drafts(filename, raw, options)

    selected = options.get("operations")
    if isinstance(selected, list) and selected:
        wanted = {str(item) for item in selected}
        drafts = [draft for draft in drafts if draft.name in wanted]

        if not drafts:
            raise ImportRejected("勾选的接口一个都不在文件里。")

    if dry_run:
        return {
            "notes": notes,
            "preview": {
                "tools": [draft.preview() for draft in drafts],
                "count": len(drafts),
                "base_url": base_url,
                "bundle": bundle,
            },
        }

    payloads: list[CustomToolCreate] = []
    invalid: list[str] = []

    for draft in drafts:
        try:
            payloads.append(
                CustomToolCreate(
                    name=draft.name,
                    display_name=draft.display_name,
                    description=draft.description,
                    method=draft.method,
                    url=draft.url,
                    risk_level=draft.risk_level,
                    params=draft.params,
                )
            )
        except Exception as exc:  # pydantic 校验失败：单条失败不该拖垮整批
            invalid.append(f"{draft.name}：{exc.__class__.__name__}")

    if not payloads:
        raise ImportRejected(
            "没有可导入的接口定义"
            + (f"（{'; '.join(invalid[:3])}）" if invalid else "。")
        )

    mode = options.get("mode")
    if mode not in {"merge", "overwrite"}:
        mode = "merge"

    gateway = get_tool_gateway()

    # 自定义工具是平台级的（表本身不挂租户），写它必须以平台租户身份落事务。
    # `require_platform_admin` 在依赖里做的也是这一步：上下文必须在事务 begin
    # 之前就位，否则 RLS 拿到的是上一个租户，写进去的行谁都看不见。
    token = bind_tenant(settings.platform_tenant_id)

    try:
        result = await custom_tool_service.import_tools(
            session,
            gateway,
            payload=CustomToolImportRequest(tools=payloads, mode=mode),
            actor=identity.subject,
        )

        await audit_service.write_audit_event(
            session,
            event_type=AuditEventType.ADMIN_ACTION,
            tenant_id=settings.platform_tenant_id,
            actor_id=identity.user_id,
            resource_type="custom_tool",
            resource_id="batch",
            message="custom_tool_imported",
            metadata={
                "operation": "import_file",
                "actor_subject": identity.subject,
                "filename": filename,
                "mode": mode,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "detail": "",
            },
        )
        await session.commit()
    finally:
        unbind_tenant(token)

    return {
        "notes": notes + ([f"这几条定义不合法：{'; '.join(invalid)}"] if invalid else []),
        "result": {
            "action": "imported" if result.created or result.updated else "skipped",
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "items": [
                {"name": item.name, "action": item.action, "message": item.message}
                for item in result.items
            ],
            "message": f"新建 {result.created} 个，覆盖 {result.updated} 个，"
            f"跳过 {result.skipped} 个",
        },
    }


def _tool_drafts(
    filename: str,
    raw: bytes,
    options: dict[str, Any],
) -> tuple[list[parsers.ToolDraft], list[str], str, bool]:
    """返回 (草稿, notes, 基地址, 是否导出包)。"""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    text = raw.decode("utf-8", errors="ignore")

    if suffix in {"json", "yaml", "yml"}:
        payload = parsers.load_structured(raw)

        if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
            # 平台导出的工具定义：形状已经是目标形状，不再走 OpenAPI 映射。
            bundle = parsers.parse_tool_export(payload)
            drafts = [_bundle_entry_to_draft(entry) for entry in bundle.entries]
            return drafts, bundle.notes, str(options.get("base_url") or ""), True

        base_url = options.get("base_url")
        drafts, notes, used = parsers.parse_openapi(
            filename, text, base_url=str(base_url) if base_url else None
        )
        return drafts, notes, used, False

    raise ImportRejected("工具文件应当是 OpenAPI 3 / Swagger 2 的 .json/.yaml，或工具导出 JSON。")


def _bundle_entry_to_draft(entry: dict[str, Any]) -> parsers.ToolDraft:
    return parsers.ToolDraft(
        name=str(entry.get("name") or ""),
        display_name=str(entry.get("display_name") or entry.get("name") or ""),
        description=str(entry.get("description") or ""),
        method=str(entry.get("method") or "GET").upper(),
        url=str(entry.get("url") or ""),
        risk_level=str(entry.get("risk_level") or "read"),
        params=[
            param for param in (entry.get("params") or []) if isinstance(param, dict)
        ],
        notes=[],
    )


# 解析层的异常统一在路由里映射，这里只做一次转手，避免上层 import 一堆内部类型。
__all__ = [
    "ImportDenied",
    "ImportRejected",
    "StudioParseError",
    "run_import",
]
