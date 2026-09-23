from app.recovery.errors import ErrorClassifier, FailureKind, RetryPolicy
from app.retrieval.embedding import EmbeddingResponseError, EmbeddingTimeoutError
from app.workflow.model import WorkflowModelResponseError, WorkflowModelTimeoutError
from sqlalchemy.exc import OperationalError


def test_error_classifier_separates_transient_review_and_permanent_failures() -> None:
    """대표 오류가 재시도·사람 검토·영구 실패로 안정적으로 분리되는지 검증한다."""
    classifier = ErrorClassifier()

    generation_timeout = classifier.classify(
        WorkflowModelTimeoutError("민감하지 않은 테스트 오류")
    )
    embedding_timeout = classifier.classify(
        EmbeddingTimeoutError("민감하지 않은 테스트 오류")
    )
    invalid_output = classifier.classify(
        WorkflowModelResponseError("민감하지 않은 테스트 오류")
    )
    invalid_embedding = classifier.classify(
        EmbeddingResponseError("민감하지 않은 테스트 오류")
    )
    unexpected = classifier.classify(RuntimeError("내부 상세를 저장하면 안 됨"))
    database_error = classifier.classify(
        OperationalError("SELECT 1", {}, ConnectionError("DB 중단"))
    )

    assert generation_timeout.kind == FailureKind.TRANSIENT
    assert embedding_timeout.kind == FailureKind.TRANSIENT
    assert invalid_output.kind == FailureKind.REVIEW_REQUIRED
    assert invalid_embedding.kind == FailureKind.PERMANENT
    assert unexpected.kind == FailureKind.PERMANENT
    assert database_error.kind == FailureKind.TRANSIENT
    assert database_error.error_code == "DATABASE_TRANSIENT"
    assert "내부 상세" not in unexpected.safe_message


def test_retry_policy_applies_exponential_backoff_with_upper_bound() -> None:
    """실행 횟수에 따른 Backoff가 지수 증가하다 설정 상한에서 멈추는지 검증한다."""
    policy = RetryPolicy(max_attempts=5, base_seconds=3, max_seconds=10)

    assert [policy.delay_seconds(attempt) for attempt in range(1, 6)] == [
        3,
        6,
        10,
        10,
        10,
    ]
