# app/api/routes/skills.py
"""技能的读写接口。

权限：任何成员都能读（否则无法判断 `/技能名` 是不是写错了）；
写一律要 `skill:write` 或平台管理员。写操作落 ADMIN_ACTION 审计，
但只记字段名与长度，不记正文——技能正文里常有业务细节。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_current_identity
from app.auth.dependencies import Identity
from app.core.config import settings
from app.db.session import async_session_factory
from app.models.audit import AuditEventType
from app.models.skill import Skill
from app.schemas.skill import (
    SkillCreateRequest,
    SkillDetail,
    SkillHitItem,
    SkillItem,
    SkillListResponse,
    SkillRecallRequest,
    SkillRecallResponse,
    SkillTemplateItem,
    SkillUpdateRequest,
)
from app.services import audit_service, skill_service, skill_templates
from app.tenancy.context import tenant_scope
from app.tools.dependencies import get_tool_gateway
from app.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])

WRITE_PERMISSION = "skill:write"


def _can_write(identity: Identity) -> bool:
    return identity.is_platform_admin or WRITE_PERMISSION in identity.scopes


def _visible_tools(identity: Identity) -> set[str]:
    gateway = get_tool_gateway()
    return {
        view.tool_name
        for view in gateway.list_available_for_context(
            context=ToolExecutionContext(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                thread_id="",
                run_id="",
                request_id="",
                roles=identity.roles,
                scopes=identity.scopes,
            )
        )
    }


def _item(
    row: Skill,
    visible: set[str] | None = None,
    *,
    hits: int = 0,
    last_hit_at: datetime | None = None,
) -> SkillItem:
    visible = visible or set()
    return SkillItem(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        instruction=row.instruction,
        trigger_keywords=list(row.trigger_keywords or []),
        allowed_tools=None if row.allowed_tools is None else list(row.allowed_tools),
        priority=row.priority,
        enabled=row.enabled,
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
        missing_tools=(
            []
            if row.allowed_tools is None
            else [name for name in row.allowed_tools if name not in visible]
        ),
        hits_7d=hits,
        last_hit_at=last_hit_at,
    )


def _audit_fields(payload: Any) -> list[str]:
    """审计只记「改了哪些字段」，不记值。"""
    return sorted(payload.model_dump(exclude_unset=True).keys())


async def _record(
    session: Any,
    identity: Identity,
    *,
    operation: str,
    skill: Skill | None = None,
    resource_id: str = "",
    fields: list[str] | None = None,
    message: str | None = None,
) -> None:
    await audit_service.write_audit_event(
        session,
        event_type=AuditEventType.ADMIN_ACTION,
        tenant_id=identity.tenant_id,
        actor_id=identity.user_id,
        resource_type="skill",
        resource_id=resource_id or (str(skill.id) if skill else ""),
        message=message or operation,
        metadata={
            "operation": operation,
            "actor_subject": identity.subject,
            "fields": fields or [],
            "name": skill.name if skill else "",
        },
    )


@router.get("", response_model=SkillListResponse)
async def list_skills(
    identity: Identity = Depends(get_current_identity),
) -> SkillListResponse:
    visible = _visible_tools(identity)
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            rows = await skill_service.list_skills(session)
            # 命中计数一次聚合查回来：按行去查就是 N+1，技能一多列表就明显变慢。
            counts = await skill_service.counts_since(session)
            last_seen = await skill_service.last_hit_at(session, [row.id for row in rows])
            items = [
                _item(
                    row,
                    visible,
                    hits=counts.get(row.id, 0),
                    last_hit_at=last_seen.get(row.id),
                )
                for row in rows
            ]
    return SkillListResponse(
        items=items,
        total=len(items),
        enabled=bool(settings.skills_enabled),
        can_write=_can_write(identity),
        hits_window_days=skill_service.HITS_WINDOW_DAYS,
    )


@router.get("/templates", response_model=list[SkillTemplateItem])
async def list_templates(
    identity: Identity = Depends(get_current_identity),
) -> list[SkillTemplateItem]:
    """内置示例。它们在代码里，不是库里的行，所以只能复制、无从删除。"""
    visible = _visible_tools(identity)
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            existing = {row.name for row in await skill_service.list_skills(session)}

    return [
        SkillTemplateItem(
            key=template.key,
            name=template.name,
            display_name=template.display_name,
            description=template.description,
            trigger_keywords=list(template.trigger_keywords),
            allowed_tools=list(template.allowed_tools),
            priority=template.priority,
            instruction_chars=len(template.instruction),
            instruction=template.instruction,
            missing_tools=[name for name in template.allowed_tools if name not in visible],
            exists=template.name in existing,
        )
        for template in skill_templates.list_templates()
    ]


@router.post("/templates/{key}", response_model=SkillItem, status_code=201)
async def create_from_template(
    key: str,
    identity: Identity = Depends(get_current_identity),
) -> SkillItem:
    """把模板复制成本租户的一个普通技能。

    复制而不是「引用模板」：模板升级不该悄悄改掉用户线上正在跑的那份。
    """
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail="没有新建技能的权限。")

    template = skill_templates.get_template(key)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在。")

    visible = _visible_tools(identity)
    try:
        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    row = await skill_service.create_skill(
                        session,
                        name=template.name,
                        display_name=template.display_name,
                        description=template.description,
                        instruction=template.instruction,
                        trigger_keywords=list(template.trigger_keywords),
                        allowed_tools=list(template.allowed_tools),
                        priority=template.priority,
                        enabled=False,
                        source="template",
                        created_by=identity.user_id,
                    )
                    await _record(
                        session,
                        identity,
                        operation="create",
                        skill=row,
                        fields=["template"],
                        message=f"从模板创建：{key}",
                    )
                    item = _item(row, visible)
    except skill_service.SkillExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"已有同名技能「{template.name}」，请先改名或删除再复制。",
        ) from exc
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "从模板创建技能。",
        extra={"event": "skill_from_template", "skill": item.name, "template": key},
    )
    return item


@router.post("/recall", response_model=SkillRecallResponse)
async def recall_skill(
    payload: SkillRecallRequest,
    identity: Identity = Depends(get_current_identity),
) -> SkillRecallResponse:
    """召回测试：这句话现在会命中哪个技能。

    与真实链路共用 `skill_service.match_skill`，不另写一套匹配逻辑——
    测试与线上不一致的「测试」比没有测试更糟。
    """
    visible = _visible_tools(identity)
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            picked = await skill_service.pick(session, payload.message)

    if picked is None:
        return SkillRecallResponse(
            matched=False,
            note="没有技能命中：这句会走普通规划，工具目录不做收窄。",
        )

    skill, matched_by, keyword = picked
    tools = list(skill.allowed_tools or [])
    if skill.allowed_tools is None:
        tools = sorted(visible)
        note = f"命中后不限制工具，可用 {len(tools)} 个。"
    else:
        usable = [name for name in tools if name in visible]
        note = (
            f"命中后只保留 {len(usable)} 个工具"
            + (f"，{len(tools) - len(usable)} 个当前身份不可用" if len(tools) > len(usable) else "")
            + "。"
        )
    return SkillRecallResponse(
        matched=True,
        skill_id=skill.id,
        name=skill.name,
        display_name=skill.display_name,
        matched_by=matched_by,
        keyword=keyword,
        tools=[name for name in tools if name in visible],
        instruction_chars=len(skill.instruction or ""),
        note=note,
    )


@router.get("/export")
async def export_skills(
    identity: Identity = Depends(get_current_identity),
) -> dict[str, Any]:
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail="没有导出技能的权限。")
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            rows = await skill_service.list_skills(session)
            skills = [
                {
                    "name": row.name,
                    "display_name": row.display_name,
                    "description": row.description,
                    "instruction": row.instruction,
                    "trigger_keywords": list(row.trigger_keywords or []),
                    "allowed_tools": row.allowed_tools,
                    "priority": row.priority,
                    "enabled": row.enabled,
                }
                for row in rows
            ]
    return {"version": 1, "skills": skills}


@router.post("/import")
async def import_skills(
    payload: dict[str, Any],
    identity: Identity = Depends(get_current_identity),
) -> dict[str, Any]:
    """导入：同名覆盖，其余新增。

    覆盖而不是跳过，是因为「改完导出、在另一台机器导入」是最常见的用法；
    跳过会让两边悄悄不一致。
    """
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail="没有导入技能的权限。")

    entries = payload.get("skills")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="文件格式不对：缺少 skills 数组。")

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                # 写库逻辑收在服务层：Studio 的「从文件导入」也走同一份，
                # 否则同一条导入规则会在这里和那里各长出不同的分支。
                created, updated = await skill_service.upsert_imported_skills(
                    session,
                    entries,
                    created_by=identity.user_id,
                )
                await _record(
                    session,
                    identity,
                    operation="import",
                    resource_id="all",
                    fields=["skills"],
                    message=f"导入技能 {created + updated} 个",
                )
    return {"created": created, "updated": updated}


@router.post("", response_model=SkillItem, status_code=201)
async def create_skill(
    payload: SkillCreateRequest,
    identity: Identity = Depends(get_current_identity),
) -> SkillItem:
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail="没有新建技能的权限。")

    visible = _visible_tools(identity)
    try:
        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    row = await skill_service.create_skill(
                        session,
                        name=payload.name,
                        display_name=payload.display_name or payload.name,
                        description=payload.description,
                        instruction=payload.instruction,
                        trigger_keywords=payload.trigger_keywords,
                        allowed_tools=payload.allowed_tools,
                        priority=payload.priority,
                        enabled=payload.enabled,
                        created_by=identity.user_id,
                    )
                    await _record(
                        session,
                        identity,
                        operation="create",
                        skill=row,
                        fields=_audit_fields(payload),
                    )
                    # 在事务内构造响应：commit 之后 ORM 属性会过期，
                    # 再读 created_at 就是一次隐式 IO，在 async 会话里直接炸。
                    item = _item(row, visible)
    except skill_service.SkillExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("技能已创建。", extra={"event": "skill_created", "skill": item.name})
    return item


@router.get("/{skill_id}", response_model=SkillDetail)
async def get_skill(
    skill_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> SkillDetail:
    visible = _visible_tools(identity)
    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            row = await skill_service.get_skill(session, skill_id)
            if row is None:
                raise HTTPException(status_code=404, detail="技能不存在。")
            counts = await skill_service.counts_since(session)
            last_seen = await skill_service.last_hit_at(session, [row.id])
            hits = await skill_service.recent_hits(session, row.id)
            item = _item(
                row,
                visible,
                hits=counts.get(row.id, 0),
                last_hit_at=last_seen.get(row.id),
            )
            return SkillDetail(
                **item.model_dump(),
                recent_hits=[
                    SkillHitItem(
                        created_at=hit.created_at,
                        matched_by=hit.matched_by,
                        keyword=hit.keyword,
                        thread_id=hit.thread_id,
                        user_id=hit.user_id,
                    )
                    for hit in hits
                ],
            )


@router.patch("/{skill_id}", response_model=SkillItem)
async def update_skill(
    skill_id: uuid.UUID,
    payload: SkillUpdateRequest,
    identity: Identity = Depends(get_current_identity),
) -> SkillItem:
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail="没有修改技能的权限。")

    visible = _visible_tools(identity)
    try:
        with tenant_scope(identity.tenant_id):
            async with async_session_factory() as session:
                async with session.begin():
                    row = await skill_service.get_skill(session, skill_id)
                    if row is None:
                        raise HTTPException(status_code=404, detail="技能不存在。")
                    fields = _audit_fields(payload)
                    # 「没传」和「传了 null」必须分开：allowed_tools=null 表示
                    # 恢复成不收窄，是一个有效修改，不能和「字段没出现」混为一谈。
                    tools_provided = "allowed_tools" in payload.model_fields_set
                    row = await skill_service.update_skill(
                        session,
                        row,
                        display_name=payload.display_name,
                        description=payload.description,
                        instruction=payload.instruction,
                        trigger_keywords=payload.trigger_keywords,
                        allowed_tools=payload.allowed_tools,
                        tools_provided=tools_provided,
                        priority=payload.priority,
                        enabled=payload.enabled,
                    )
                    await _record(
                        session,
                        identity,
                        operation="update",
                        skill=row,
                        fields=fields,
                    )
                    # 命中统计要照实带回来：`_item` 默认给 0 是给「新建」的场景准备的，
                    # 用在这里会让保存后的响应凭空说「近 7 天 0 次」。
                    counts = await skill_service.counts_since(session)
                    last_seen = await skill_service.last_hit_at(session, [row.id])
                    item = _item(
                        row,
                        visible,
                        hits=counts.get(row.id, 0),
                        last_hit_at=last_seen.get(row.id),
                    )
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return item


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: uuid.UUID,
    identity: Identity = Depends(get_current_identity),
) -> dict[str, str]:
    if not _can_write(identity):
        raise HTTPException(status_code=403, detail="没有删除技能的权限。")

    with tenant_scope(identity.tenant_id):
        async with async_session_factory() as session:
            async with session.begin():
                row = await skill_service.get_skill(session, skill_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="技能不存在。")
                name = row.name
                await _record(session, identity, operation="delete", skill=row, fields=[])
                await skill_service.delete_skill(session, row)

    logger.info("技能已删除。", extra={"event": "skill_deleted", "skill": name})
    return {"message": "已删除", "name": name}
