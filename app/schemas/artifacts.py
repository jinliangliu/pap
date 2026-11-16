# app/schemas/artifacts.py
from __future__ import annotations

from datetime import datetime

from app.schemas.common import APIModel


class ArtifactResponse(APIModel):
    artifact_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    conversation_id: str | None = None
    created_at: datetime
