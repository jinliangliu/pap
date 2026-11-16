# app/api/router.py
from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    admin,
    admin_tools,
    agents,
    approvals,
    artifacts,
    chat,
    connectors,
    conversations,
    knowledge,
    me,
    memories,
    observability,
    permissions,
    skills,
    studio,
    tools,
    workflows,
)

api_router = APIRouter()
api_router.include_router(me.router)
api_router.include_router(chat.router)
api_router.include_router(conversations.router)
api_router.include_router(approvals.router)
api_router.include_router(knowledge.router)
api_router.include_router(tools.router)
api_router.include_router(artifacts.router)
api_router.include_router(admin.router)
api_router.include_router(admin_tools.router)
api_router.include_router(permissions.router)
api_router.include_router(observability.router)
api_router.include_router(memories.router)
api_router.include_router(skills.router)
api_router.include_router(studio.router)
api_router.include_router(agents.router)
api_router.include_router(connectors.router)
api_router.include_router(workflows.router)
api_router.include_router(workflows.runs_router)

__all__ = ["api_router"]
