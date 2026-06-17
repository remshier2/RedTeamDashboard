from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.finding import FindingPhase


class ObservationCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10_000)
    phase: FindingPhase | None = None


class ObservationRead(BaseModel):
    id: UUID
    content: str
    phase: FindingPhase | None
    created_by: UUID | None
    created_at: datetime
