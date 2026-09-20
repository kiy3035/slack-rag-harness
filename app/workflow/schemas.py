from enum import StrEnum
from typing import NotRequired, Self, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.common.domain import JobStatus


class IntentCategory(StrEnum):
    """기본 Workflow가 다루는 질문 의도 범주를 제한한다."""

    PROCEDURE = "PROCEDURE"
    TROUBLESHOOTING = "TROUBLESHOOTING"
    INFORMATION = "INFORMATION"
    SENSITIVE_ACTION = "SENSITIVE_ACTION"


class RiskLevel(StrEnum):
    """자동 답변 가능 여부를 판단할 최소 위험 등급을 정의한다."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CitationOutput(BaseModel):
    """답변이 참조한 실제 문서와 Chunk 식별자를 검증한다."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    chunk_id: UUID


class IntentOutput(BaseModel):
    """의도 분류 모델의 범주와 위험 등급 출력을 검증한다."""

    model_config = ConfigDict(extra="forbid")

    intent: IntentCategory
    risk_level: RiskLevel
    reason: str = Field(min_length=1, max_length=500)


class AnswerOutput(BaseModel):
    """검색 근거에 한정된 구조화 답변과 검토 필요 여부를 검증한다."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=8_000)
    citations: list[CitationOutput] = Field(max_length=20)
    needs_review: bool = False
    review_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_review_contract(self) -> Self:
        """정상 답변은 인용을 요구하고 검토 답변은 사유 누락을 막는다."""
        if self.needs_review and not self.review_reason:
            raise ValueError("검토 필요 답변에는 사유가 필요합니다.")
        if not self.needs_review and not self.citations:
            raise ValueError("자동 완료 답변에는 인용이 필요합니다.")
        return self


class WorkflowRequest(BaseModel):
    """새 실행 또는 재개에 필요한 직렬화 가능한 입력만 받는다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: UUID
    thread_id: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1, max_length=20_000)

    def to_state(self) -> "WorkflowState":
        """검증된 요청을 LangGraph 첫 Checkpoint 입력으로 변환한다."""
        return {
            "job_id": str(self.job_id),
            "thread_id": self.thread_id,
            "question": self.question,
            "retrieved_chunks": [],
            "retrieval_attempts": 0,
            "generation_attempts": 0,
            "validation_errors": [],
        }


class WorkflowResult(BaseModel):
    """Workflow 종료 상태와 구조화 답변을 호출자에게 반환한다."""

    job_id: UUID
    thread_id: str
    status: JobStatus
    answer: AnswerOutput | None
    retrieved_chunk_ids: list[UUID]


class WorkflowJob(BaseModel):
    """Worker 실행에 필요한 작업 데이터만 DB 모델에서 분리한다."""

    job_id: UUID
    question: str
    status: JobStatus


class WorkflowState(TypedDict):
    """Checkpoint에 저장 가능한 값만 포함하는 LangGraph 공유 상태다."""

    job_id: str
    thread_id: str
    question: str
    normalized_question: NotRequired[str]
    intent: NotRequired[str]
    risk_level: NotRequired[str]
    intent_reason: NotRequired[str]
    retrieved_chunks: list[dict[str, object]]
    retrieval_attempts: int
    generation_attempts: int
    draft_answer: NotRequired[dict[str, object]]
    validation_errors: list[str]
    final_status: NotRequired[str]
    last_node: NotRequired[str]


WorkflowUpdate = dict[str, object]
