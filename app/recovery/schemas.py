from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.common.domain import JobStatus


class ManualRetryRequest(BaseModel):
    """관리자 재처리 사유와 중복 요청을 식별할 키를 검증한다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=500)


class ManualRetryResponse(BaseModel):
    """수동 재처리 결과의 작업 상태와 Workflow 세대를 반환한다."""

    job_id: UUID
    status: JobStatus
    workflow_revision: int
    idempotent: bool
