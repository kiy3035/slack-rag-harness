from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationCategory(str, Enum):
    """로드맵에서 요구한 평가 질문 유형을 고정된 값으로 관리한다."""

    ANSWERABLE = "answerable"
    NO_DOCUMENT = "no_document"
    PARAPHRASE = "paraphrase"
    MULTI_DOCUMENT = "multi_document"
    DOCUMENT_CONFLICT = "document_conflict"
    SENSITIVE = "sensitive"


class EvaluationCase(BaseModel):
    """한 평가 질문과 기대 검색 문서·검토 전환 계약을 검증한다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    category: EvaluationCategory
    question: str = Field(min_length=3, max_length=4_000)
    expected_document_paths: list[str] = Field(default_factory=list, max_length=20)
    expected_review: bool
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_category_contract(self) -> Self:
        """문서 필요 유형과 다중 문서 유형의 정답 계약 누락을 차단한다."""
        if len(self.expected_document_paths) != len(set(self.expected_document_paths)):
            raise ValueError("기대 문서 경로는 중복될 수 없습니다.")
        if self.category in {
            EvaluationCategory.ANSWERABLE,
            EvaluationCategory.PARAPHRASE,
        } and not self.expected_document_paths:
            raise ValueError("답변 가능 질문에는 기대 문서가 필요합니다.")
        if self.category in {
            EvaluationCategory.MULTI_DOCUMENT,
            EvaluationCategory.DOCUMENT_CONFLICT,
        } and len(self.expected_document_paths) < 2:
            raise ValueError("다중 문서 질문에는 기대 문서가 두 개 이상 필요합니다.")
        if (
            self.category == EvaluationCategory.NO_DOCUMENT
            and self.expected_document_paths
        ):
            raise ValueError("문서 없음 질문에는 기대 문서를 지정할 수 없습니다.")
        if self.category in {
            EvaluationCategory.ANSWERABLE,
            EvaluationCategory.PARAPHRASE,
            EvaluationCategory.MULTI_DOCUMENT,
        } and self.expected_review:
            raise ValueError("답변 가능 질문은 자동 완료를 기대해야 합니다.")
        if self.category in {
            EvaluationCategory.NO_DOCUMENT,
            EvaluationCategory.DOCUMENT_CONFLICT,
            EvaluationCategory.SENSITIVE,
        } and not self.expected_review:
            raise ValueError("근거 없음·충돌·민감 질문은 검토 전환을 기대해야 합니다.")
        return self


class EvaluationConfig(BaseModel):
    """한 번의 평가에서 고정할 검색·재작성·동시 실행 조건을 표현한다."""

    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(default=5, ge=1, le=100)
    min_score: float = Field(default=-1.0, ge=-1.0, le=1.0)
    max_chunks_per_document: int = Field(default=2, ge=1, le=20)
    max_query_rewrites: int = Field(default=1, ge=0, le=3)
    worker_count: int = Field(default=1, ge=1, le=32)


class EvaluationChunk(BaseModel):
    """평가 결과에 필요한 검색 Chunk 식별자와 근거 본문만 보존한다."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1)
    document_path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    score: float = Field(ge=-1.0, le=1.0)


class EvaluationPrediction(BaseModel):
    """Workflow 한 건의 실제 검색·답변·검토 결과와 지연을 저장한다."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    retrieved_chunks: list[EvaluationChunk] = Field(default_factory=list)
    cited_chunk_ids: list[str] = Field(default_factory=list)
    answer: str | None = None
    review_required: bool
    review_reason_code: str | None = None
    latency_ms: float = Field(ge=0.0)
    error_code: str | None = None


class CaseEvaluation(BaseModel):
    """질문 한 건의 계산된 품질 지표와 근거 없는 문장을 묶는다."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: EvaluationCategory
    retrieval_recall_at_k: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_correct: int = Field(ge=0)
    citation_total: int = Field(ge=0)
    review_correct: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    claim_count: int = Field(ge=0)
    prediction: EvaluationPrediction


class MetricSummary(BaseModel):
    """한 평가 실행의 집계 품질·지연·실패 지표를 표현한다."""

    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=1)
    retrieval_recall_at_k: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    review_transition_accuracy: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    throughput_cases_per_second: float = Field(ge=0.0)
    p95_latency_ms: float = Field(ge=0.0)
    failed_cases: int = Field(ge=0)


class EvaluationEnvironment(BaseModel):
    """성능 수치 재현에 필요한 실행 환경과 로컬 모델 정보를 기록한다."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    machine: str
    cpu_count: int | None
    generation_model: str
    embedding_model: str


class EvaluationCheckpoint(BaseModel):
    """중단된 평가가 같은 조건에서 남은 Case만 재개하도록 진행 상태를 보존한다."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,127}$")
    dataset_path: str = Field(min_length=1)
    dataset_sha256: str = Field(min_length=64, max_length=64)
    config: EvaluationConfig
    environment: EvaluationEnvironment
    cumulative_duration_ms: float = Field(ge=0.0)
    predictions: list[EvaluationPrediction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_predictions(self) -> Self:
        """같은 Case 결과가 중복 저장되어 재개 순서가 모호해지는 것을 막는다."""
        case_ids = [prediction.case_id for prediction in self.predictions]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("체크포인트의 case_id는 중복될 수 없습니다.")
        return self


class EvaluationReport(BaseModel):
    """설정·환경·개별 결과·집계값을 함께 보존하는 평가 산출물이다."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    measured_at: str
    dataset_path: str
    dataset_sha256: str = Field(min_length=64, max_length=64)
    config: EvaluationConfig
    environment: EvaluationEnvironment
    duration_ms: float = Field(ge=0.0)
    summary: MetricSummary
    cases: list[CaseEvaluation]


class CategorySummary(BaseModel):
    """Markdown 리포트에서 질문 유형별 품질 차이를 보여 줄 집계값이다."""

    model_config = ConfigDict(extra="forbid")

    category: EvaluationCategory
    case_count: int = Field(ge=1)
    retrieval_recall_at_k: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    review_transition_accuracy: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float | None = Field(default=None, ge=0.0, le=1.0)
