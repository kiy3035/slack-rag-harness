from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.evaluation.checkpoint import (
    checkpoint_path,
    load_checkpoint,
    validate_checkpoint_context,
    write_checkpoint,
)
from app.evaluation.dataset import category_counts, load_dataset
from app.evaluation.lock import (
    EvaluationRunLock,
    EvaluationRunLockedError,
    run_lock_path,
)
from app.evaluation.reporting import (
    render_comparison_report,
    render_markdown_report,
    write_report,
)
from app.evaluation.schemas import (
    EvaluationCase,
    EvaluationCategory,
    EvaluationCheckpoint,
    EvaluationChunk,
    EvaluationConfig,
    EvaluationEnvironment,
    EvaluationPrediction,
    EvaluationReport,
)
from app.evaluation.runner import LiveEvaluationHarness
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


def test_checkpoint_round_trip_and_context_validation(tmp_path: Path) -> None:
    """체크포인트가 원자 저장되고 같은 데이터·설정·환경에서만 재개되는지 검증한다."""
    case = EvaluationCase(
        case_id="resume-case",
        category=EvaluationCategory.NO_DOCUMENT,
        question="재개할 질문입니다.",
        expected_review=True,
    )
    environment = EvaluationEnvironment(
        platform="test-platform",
        machine="test-machine",
        cpu_count=2,
        generation_model="fake-generation",
        embedding_model="fake-embedding",
    )
    prediction = EvaluationPrediction(
        case_id=case.case_id,
        review_required=True,
        latency_ms=125.0,
    )
    checkpoint = EvaluationCheckpoint(
        run_id="eval-resume-test",
        dataset_path="evaluation/datasets/v1.jsonl",
        dataset_sha256="b" * 64,
        config=EvaluationConfig(),
        environment=environment,
        cumulative_duration_ms=250.0,
        predictions=[prediction],
    )
    path = checkpoint_path(tmp_path, checkpoint.run_id)

    write_checkpoint(checkpoint, path)
    loaded = load_checkpoint(path)
    validate_checkpoint_context(
        loaded,
        dataset_hash="b" * 64,
        config=EvaluationConfig(),
        environment=environment,
        cases=[case],
    )

    assert loaded == checkpoint
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    with pytest.raises(ValueError, match="설정"):
        validate_checkpoint_context(
            loaded,
            dataset_hash="b" * 64,
            config=EvaluationConfig(top_k=8),
            environment=environment,
            cases=[case],
        )


@pytest.mark.asyncio
async def test_run_cases_skips_checkpointed_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """재개 실행이 이미 저장된 Case를 다시 호출하지 않고 입력 순서를 보존하는지 검증한다."""
    cases = [
        EvaluationCase(
            case_id="resume-first",
            category=EvaluationCategory.NO_DOCUMENT,
            question="첫 번째 질문입니다.",
            expected_review=True,
        ),
        EvaluationCase(
            case_id="resume-second",
            category=EvaluationCategory.NO_DOCUMENT,
            question="두 번째 질문입니다.",
            expected_review=True,
        ),
    ]
    existing = EvaluationPrediction(
        case_id="resume-first",
        review_required=True,
        latency_ms=100.0,
    )
    harness = object.__new__(LiveEvaluationHarness)
    harness._config = EvaluationConfig(worker_count=2)
    executed_case_ids: list[str] = []
    snapshots: list[tuple[list[str], str]] = []

    async def fake_run_case(
        case: EvaluationCase,
        run_id: str,
    ) -> EvaluationPrediction:
        """실제 Ollama 없이 새 Case 호출 여부를 기록한다."""
        executed_case_ids.append(case.case_id)
        assert run_id == "eval-resume-test"
        return EvaluationPrediction(
            case_id=case.case_id,
            review_required=True,
            latency_ms=200.0,
        )

    def record_progress(
        predictions: list[EvaluationPrediction],
        case_id: str,
    ) -> None:
        """체크포인트 콜백의 입력 순서와 마지막 완료 Case를 기록한다."""
        snapshots.append(([item.case_id for item in predictions], case_id))

    monkeypatch.setattr(harness, "_run_case", fake_run_case)

    predictions = await harness.run_cases(
        cases,
        "eval-resume-test",
        existing_predictions=[existing],
        on_progress=record_progress,
    )

    assert executed_case_ids == ["resume-second"]
    assert [item.case_id for item in predictions] == [
        "resume-first",
        "resume-second",
    ]
    assert snapshots == [
        (["resume-first", "resume-second"], "resume-second")
    ]


def test_run_lock_rejects_same_run_id_until_owner_exits(tmp_path: Path) -> None:
    """같은 실행 ID의 두 프로세스가 Checkpoint와 최종 결과를 덮어쓰지 못하게 한다."""
    path = run_lock_path(tmp_path, "eval-lock-test")

    with EvaluationRunLock(path):
        with pytest.raises(EvaluationRunLockedError, match="이미 진행 중"):
            with EvaluationRunLock(path):
                pass

    with EvaluationRunLock(path):
        assert path.exists()
