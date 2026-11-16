# app/services/approval_dependencies.py
from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.db.session import async_session_factory
from app.services.approval_service import ApprovalService
from app.tools.dependencies import get_tool_gateway


@lru_cache(maxsize=1)
def get_approval_service() -> ApprovalService:
    return ApprovalService(
        session_factory=async_session_factory,
        tool_gateway=get_tool_gateway(),
        default_expire_seconds=settings.approval_expire_seconds,
    )
