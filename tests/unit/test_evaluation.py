from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.evaluation.dataset import category_counts, load_dataset
from app.evaluation.reporting import (
    render_comparison_report,
    render_markdown_report,
    write_report,
)
from app.evaluation.schemas import (
    EvaluationCase,
    EvaluationCategory,
    EvaluationChunk,
    EvaluationConfig,
    EvaluationEnvironment,
    EvaluationPrediction,
    EvaluationReport,
)
from app.evaluation.scoring import (
    evaluate_case,
    find_unsupported_claims,
    summarize_cases,
)


DATASET_PATH = Path("evaluation/datasets/v1.jsonl")


def test_dataset_has_sixty_balanced_valid_cases() -> None:
    """고정 데이터셋이 60건과 여섯 필수 유형을 같은 수로 포함하는지 검증한다."""
    cases = load_dataset(DATASET_PATH)

    assert len(cases) == 60
    assert category_counts(cases) == {
        "answerable": 10,
        "no_document": 10,
        "paraphrase": 10,
        "multi_document": 10,
        "document_conflict": 10,
        "sensitive": 10,
    }


def test_case_scoring_uses_expected_documents_and_cited_evidence() -> None:
    """Recall·인용·검토·근거 문장 지표가 서로 다른 분모로 계산되는지 검증한다."""
    case = EvaluationCase(
        case_id="answerable-test",
        category=EvaluationCategory.ANSWERABLE,
        question="정산 마감 전에 무엇을 확인하나요?",
        expected_document_paths=["knowledge/manuals/01_settlement_batch.md"],
        expected_review=False,
    )
    prediction = EvaluationPrediction(
        case_id=case.case_id,
        retrieved_chunks=[
            EvaluationChunk(
                chunk_id="chunk-1",
                document_path="knowledge/manuals/01_settlement_batch.md",
                content="담당자는 입력 건수와 합계 금액을 대조하고 승인 번호를 기록한다.",
                score=0.91,
            )
        ],
        cited_chunk_ids=["chunk-1"],
        answer="담당자는 입력 건수와 합계 금액을 대조해야 합니다.",
        review_required=False,
        latency_ms=120.0,
    )

    result = evaluate_case(case, prediction)
    summary = summarize_cases([result], duration_ms=200.0)

    assert result.retrieval_recall_at_k == 1.0
    assert result.citation_correct == 1
    assert result.unsupported_claims == []
    assert summary.citation_accuracy == 1.0
    assert summary.review_transition_accuracy == 1.0
    assert summary.throughput_cases_per_second == 5.0
    assert summary.p95_latency_ms == 120.0


def test_unsupported_claim_rule_flags_uncited_or_unrelated_sentences() -> None:
    """인용이 없거나 핵심어가 겹치지 않는 사실 문장을 근거 없음으로 표시하는지 검증한다."""
    answer = (
        "백업은 체크섬과 보존 기간을 확인해야 합니다. "
        "모든 백업은 해외 서버에 영구 보관됩니다."
    )
    evidence = ["백업 작업은 스냅샷 생성뿐 아니라 체크섬과 보존 기간을 확인한다."]

    unsupported, claim_count = find_unsupported_claims(answer, evidence)

    assert claim_count == 2
    assert unsupported == ["모든 백업은 해외 서버에 영구 보관됩니다."]


def build_report(
    *,
    run_id: str,
    config: EvaluationConfig,
) -> EvaluationReport:
    """리포트 렌더링과 비교 테스트에 사용할 최소 유효 실행을 만든다."""
    case = EvaluationCase(
        case_id="no-document-test",
        category=EvaluationCategory.NO_DOCUMENT,
        question="사내 식단은 무엇인가요?",
        expected_document_paths=[],
        expected_review=True,
    )
    prediction = EvaluationPrediction(
        case_id=case.case_id,
        review_required=True,
        review_reason_code="INSUFFICIENT_EVIDENCE",
        latency_ms=50.0,
    )
    evaluated = evaluate_case(case, prediction)
    return EvaluationReport(
        run_id=run_id,
        measured_at=datetime.now(UTC).isoformat(),
        dataset_path="evaluation/datasets/v1.jsonl",
        dataset_sha256="a" * 64,
        config=config,
        environment=EvaluationEnvironment(
            platform="test-platform",
            machine="test-machine",
            cpu_count=2,
            generation_model="fake-generation",
            embedding_model="fake-embedding",
        ),
        duration_ms=100.0,
        summary=summarize_cases([evaluated], duration_ms=100.0),
        cases=[evaluated],
    )


def test_reports_are_written_as_json_and_markdown(tmp_path: Path) -> None:
    """한 실행 결과가 검증 가능한 JSON과 한국어 Markdown 두 형식으로 저장되는지 검증한다."""
    report = build_report(run_id="eval-test", config=EvaluationConfig())

    json_path, markdown_path = write_report(report, tmp_path)

    assert EvaluationReport.model_validate_json(
        json_path.read_text(encoding="utf-8")
    ) == report
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Retrieval Recall@K" in markdown
    assert "근거 없는 문장률" in markdown
    assert "fake-generation" in markdown
    assert render_markdown_report(report) == markdown


def test_comparison_allows_only_one_changed_condition() -> None:
    """비교 실험이 기준 실행에서 한 조건만 바꾸도록 강제하는지 검증한다."""
    baseline = build_report(run_id="baseline", config=EvaluationConfig())
    top_k_candidate = build_report(
        run_id="top-k-8",
        config=EvaluationConfig(top_k=8),
    )
    invalid_candidate = build_report(
        run_id="invalid",
        config=EvaluationConfig(top_k=8, worker_count=2),
    )

    comparison = render_comparison_report([baseline, top_k_candidate])

    assert "top_k" in comparison
    with pytest.raises(ValueError, match="한 조건"):
        render_comparison_report([baseline, invalid_candidate])
