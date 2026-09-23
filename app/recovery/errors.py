from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.retrieval.embedding import (
    EmbeddingError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from app.workflow.model import (
    WorkflowModelError,
    WorkflowModelResponseError,
    WorkflowModelTimeoutError,
)


class FailureKind(StrEnum):
    """Worker 실패가 따라야 할 복구 경로를 제한된 값으로 정의한다."""

    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """예외 원문을 노출하지 않는 오류 종류와 검색 가능한 코드를 보관한다."""

    kind: FailureKind
    error_code: str
    safe_message: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Worker 실행 재시도의 횟수와 지수 Backoff 상한을 계산한다."""

    max_attempts: int
    base_seconds: int
    max_seconds: int

    def delay_seconds(self, attempt_count: int) -> int:
        """첫 실패부터 설정 상한까지 증가하는 지수 Backoff를 반환한다."""
        exponent = max(attempt_count - 1, 0)
        return min(self.base_seconds * (2**exponent), self.max_seconds)


class ErrorClassifier:
    """외부·계약·영구 오류를 재시도, 검토, DLQ 경로로 분류한다."""

    def classify(self, error: Exception) -> FailureDecision:
        """예외 타입만 사용해 민감한 원문 없이 결정적인 복구 결정을 만든다."""
        if isinstance(error, WorkflowModelResponseError):
            return FailureDecision(
                kind=FailureKind.REVIEW_REQUIRED,
                error_code="MODEL_OUTPUT_INVALID",
                safe_message="모델 출력 계약을 검토해야 합니다.",
            )
        if isinstance(error, WorkflowModelTimeoutError):
            return FailureDecision(
                kind=FailureKind.TRANSIENT,
                error_code="OLLAMA_GENERATION_TIMEOUT",
                safe_message="로컬 생성 모델 응답 시간이 초과됐습니다.",
            )
        if isinstance(error, WorkflowModelError):
            return FailureDecision(
                kind=FailureKind.TRANSIENT,
                error_code="OLLAMA_GENERATION_UNAVAILABLE",
                safe_message="로컬 생성 모델을 일시적으로 사용할 수 없습니다.",
            )
        if isinstance(error, EmbeddingTimeoutError):
            return FailureDecision(
                kind=FailureKind.TRANSIENT,
                error_code="OLLAMA_EMBEDDING_TIMEOUT",
                safe_message="로컬 임베딩 모델 응답 시간이 초과됐습니다.",
            )
        if isinstance(error, EmbeddingResponseError):
            return FailureDecision(
                kind=FailureKind.PERMANENT,
                error_code="EMBEDDING_CONTRACT_INVALID",
                safe_message="임베딩 응답 계약이 올바르지 않습니다.",
            )
        if isinstance(error, EmbeddingError):
            return FailureDecision(
                kind=FailureKind.TRANSIENT,
                error_code="OLLAMA_EMBEDDING_UNAVAILABLE",
                safe_message="로컬 임베딩 모델을 일시적으로 사용할 수 없습니다.",
            )
        if isinstance(error, SQLAlchemyError):
            return FailureDecision(
                kind=FailureKind.TRANSIENT,
                error_code="DATABASE_TRANSIENT",
                safe_message="데이터베이스를 일시적으로 사용할 수 없습니다.",
            )
        if isinstance(error, (TimeoutError, ConnectionError, OSError)):
            return FailureDecision(
                kind=FailureKind.TRANSIENT,
                error_code="DEPENDENCY_TRANSIENT",
                safe_message="외부 의존성을 일시적으로 사용할 수 없습니다.",
            )
        if isinstance(error, LookupError):
            return FailureDecision(
                kind=FailureKind.PERMANENT,
                error_code="WORKFLOW_RESOURCE_NOT_FOUND",
                safe_message="처리에 필요한 작업 정보를 찾을 수 없습니다.",
            )
        if isinstance(error, (ValidationError, ValueError)):
            return FailureDecision(
                kind=FailureKind.PERMANENT,
                error_code="WORKFLOW_INPUT_INVALID",
                safe_message="작업 입력 또는 내부 계약이 올바르지 않습니다.",
            )
        return FailureDecision(
            kind=FailureKind.PERMANENT,
            error_code=f"WORKER_{type(error).__name__.upper()}",
            safe_message="Worker가 복구할 수 없는 오류로 작업을 중단했습니다.",
        )
