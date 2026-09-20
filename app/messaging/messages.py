from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class JobMessage(BaseModel):
    """Outbox와 Worker 사이에서 교환하는 최소 작업 식별자 계약이다."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    outbox_id: UUID
    job_id: UUID
    request_id: str = Field(min_length=1, max_length=255)
    thread_id: str | None = Field(default=None, max_length=100)

