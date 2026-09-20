from enum import StrEnum
from typing import NotRequired, Self, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.common.domain import JobStatus
from app.retrieval.schemas import SearchHit


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


class ReviewReasonCode(StrEnum):
    """자동 완료를 중단한 검색·검증 사유를 검색 가능한 코드로 제한한다."""

    SENSITIVE_INTENT = "SENSITIVE_INTENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    DOCUMENT_CONFLICT = "DOCUMENT_CONFLICT"
    RELEVANCE_OUTPUT_INVALID = "RELEVANCE_OUTPUT_INVALID"
    OUTPUT_SCHEMA_INVALID = "OUTPUT_SCHEMA_INVALID"
    CITATION_INVALID = "CITATION_INVALID"
    SENSITIVE_OUTPUT = "SENSITIVE_OUTPUT"
    MODEL_REVIEW_REQUIRED = "MODEL_REVIEW_REQUIRED"


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


class DocumentGradeOutput(BaseModel):
    """검색 Chunk의 관련성 ID와 문서 충돌 여부를 구조화해 검증한다."""

    model_config = ConfigDict(extra="forbid")

    relevant_chunk_ids: list[UUID] = Field(max_length=20)
    conflict_detected: bool = False
    reason: str = Field(min_length=1, max_length=1_000)


class RewriteQueryOutput(BaseModel):
    """원 질문의 의미를 유지한 단일 재검색어 출력을 검증한다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=1_000)


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
    workflow_revision: int = Field(default=0, ge=0)

    def to_state(self) -> "WorkflowState":
        """검증된 요청을 LangGraph 첫 Checkpoint 입력으로 변환한다."""
        return {
            "job_id": str(self.job_id),
            "thread_id": self.thread_id,
            "question": self.question,
            "workflow_revision": self.workflow_revision,
            "retrieved_chunks": [],
            "retrieval_attempts": 0,
            "query_rewrite_attempts": 0,
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
    retrieved_chunks: list[SearchHit]
    relevant_chunks: list[SearchHit]
    review_reason_code: ReviewReasonCode | None
    validation_errors: list[str]
    workflow_revision: int


class WorkflowJob(BaseModel):
    """Worker 실행에 필요한 작업 데이터만 DB 모델에서 분리한다."""

    job_id: UUID
    question: str
    status: JobStatus
    workflow_revision: int


class WorkflowState(TypedDict):
    """Checkpoint에 저장 가능한 값만 포함하는 LangGraph 공유 상태다."""

    job_id: str
    thread_id: str
    question: str
    workflow_revision: int
    normalized_question: NotRequired[str]
    search_query: NotRequired[str]
    intent: NotRequired[str]
    risk_level: NotRequired[str]
    intent_reason: NotRequired[str]
    retrieved_chunks: list[dict[str, object]]
    relevant_chunks: NotRequired[list[dict[str, object]]]
    relevance_reason: NotRequired[str]
    relevance_passed: NotRequired[bool]
    conflict_detected: NotRequired[bool]
    retrieval_attempts: int
    query_rewrite_attempts: int
    generation_attempts: int
    draft_answer: NotRequired[dict[str, object]]
    schema_valid: NotRequired[bool]
    citations_valid: NotRequired[bool]
    validation_errors: list[str]
    review_reason_code: NotRequired[str]
    final_status: NotRequired[str]
    last_node: NotRequired[str]


WorkflowUpdate = dict[str, object]
