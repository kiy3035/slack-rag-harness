import json
from pathlib import Path

from app.evaluation.scoring import summarize_categories
from app.evaluation.schemas import EvaluationConfig, EvaluationReport


def _percent(value: float | None) -> str:
    """값이 없는 지표와 실제 비율을 혼동하지 않는 표시 문자열을 만든다."""
    return "N/A" if value is None else f"{value * 100:.2f}%"


def render_markdown_report(report: EvaluationReport) -> str:
    """한 실행의 설정·환경·전체·분류별 결과를 재현 가능한 Markdown으로 만든다."""
    summary = report.summary
    category_rows = [
        "| 유형 | 건수 | Recall@K | 인용 정확도 | 검토 전환 정확도 | 근거 없는 문장률 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summarize_categories(report.cases):
        category_rows.append(
            f"| {item.category.value} | {item.case_count} | "
            f"{_percent(item.retrieval_recall_at_k)} | "
            f"{_percent(item.citation_accuracy)} | "
            f"{_percent(item.review_transition_accuracy)} | "
            f"{_percent(item.unsupported_claim_rate)} |"
        )
    failed_ids = [
        item.case_id for item in report.cases if item.prediction.error_code is not None
    ]
    failures = ", ".join(failed_ids) if failed_ids else "없음"
    return "\n".join(
        [
            "# RAG 평가 결과",
            "",
            f"- 실행 ID: `{report.run_id}`",
            f"- 측정 시각: `{report.measured_at}`",
            f"- 데이터셋: `{report.dataset_path}`",
            f"- 데이터셋 SHA-256: `{report.dataset_sha256}`",
            f"- 생성 모델: `{report.environment.generation_model}`",
            f"- 임베딩 모델: `{report.environment.embedding_model}`",
            f"- 환경: `{report.environment.platform}` / `{report.environment.machine}` / CPU `{report.environment.cpu_count}`",
            "",
            "## 고정 조건",
            "",
            f"- Top-K: `{report.config.top_k}`",
            f"- 최소 유사도: `{report.config.min_score}`",
            f"- 문서별 최대 Chunk: `{report.config.max_chunks_per_document}`",
            f"- 검색어 재작성 최대 횟수: `{report.config.max_query_rewrites}`",
            f"- 동시 Worker 수: `{report.config.worker_count}`",
            "",
            "## 전체 결과",
            "",
            "| 지표 | 결과 |",
            "| --- | ---: |",
            f"| 평가 건수 | {summary.case_count} |",
            f"| Retrieval Recall@K | {_percent(summary.retrieval_recall_at_k)} |",
            f"| 인용 정확도 | {_percent(summary.citation_accuracy)} |",
            f"| 검토 전환 정확도 | {_percent(summary.review_transition_accuracy)} |",
            f"| 근거 없는 문장률 | {_percent(summary.unsupported_claim_rate)} |",
            f"| 처리량 | {summary.throughput_cases_per_second:.3f} cases/s |",
            f"| p95 지연 | {summary.p95_latency_ms:.2f} ms |",
            f"| 실패 건수 | {summary.failed_cases} |",
            f"| 총 실행시간 | {report.duration_ms:.2f} ms |",
            "",
            "## 유형별 결과",
            "",
            *category_rows,
            "",
            "## 실패 Case",
            "",
            failures,
            "",
            "## 지표 해석 주의사항",
            "",
            "- Recall@K는 기대 문서가 있는 Case만 분모에 포함합니다.",
            "- 인용 정확도는 인용된 Chunk가 이번 검색 결과이면서 기대 문서에 속하는지 계산합니다.",
            "- 근거 없는 문장 탐지는 인용 Chunk와 답변 문장의 핵심어 겹침을 사용하는 보수적 규칙이며 의미적 사실 검증을 대체하지 않습니다.",
            "- 처리량과 p95는 위 환경·모델·Worker 조건에서 이번 실행이 실제 측정한 값입니다.",
            "",
        ]
    )


def write_report(report: EvaluationReport, output_directory: Path) -> tuple[Path, Path]:
    """JSON 원본과 사람이 읽는 Markdown을 같은 실행 ID로 원자적으로 저장한다."""
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"{report.run_id}.json"
    markdown_path = output_directory / f"{report.run_id}.md"
    json_temp = json_path.with_suffix(".json.tmp")
    markdown_temp = markdown_path.with_suffix(".md.tmp")
    json_temp.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_temp.write_text(render_markdown_report(report), encoding="utf-8")
    json_temp.replace(json_path)
    markdown_temp.replace(markdown_path)
    return json_path, markdown_path


def changed_config_fields(
    baseline: EvaluationConfig,
    candidate: EvaluationConfig,
) -> list[str]:
    """비교 실험에서 달라진 조건 이름을 정렬된 목록으로 반환한다."""
    baseline_values = baseline.model_dump()
    candidate_values = candidate.model_dump()
    return sorted(
        name
        for name, value in baseline_values.items()
        if candidate_values[name] != value
    )


def render_comparison_report(reports: list[EvaluationReport]) -> str:
    """같은 데이터셋에서 한 조건씩 바꾼 실행만 비교 표로 만든다."""
    if len(reports) < 2:
        raise ValueError("비교 리포트는 기준과 후보 실행이 필요합니다.")
    baseline = reports[0]
    rows = [
        "| 실행 | 변경 조건 | Recall@K | 인용 정확도 | 검토 전환 | 근거 없음 | 처리량 | p95 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in reports:
        if report.dataset_sha256 != baseline.dataset_sha256:
            raise ValueError("서로 다른 데이터셋의 실행은 비교할 수 없습니다.")
        changed = changed_config_fields(baseline.config, report.config)
        if report is not baseline and len(changed) != 1:
            raise ValueError("후보 실행은 기준 실행에서 한 조건만 변경해야 합니다.")
        changed_label = "baseline" if report is baseline else changed[0]
        summary = report.summary
        rows.append(
            f"| {report.run_id} | {changed_label} | "
            f"{_percent(summary.retrieval_recall_at_k)} | "
            f"{_percent(summary.citation_accuracy)} | "
            f"{_percent(summary.review_transition_accuracy)} | "
            f"{_percent(summary.unsupported_claim_rate)} | "
            f"{summary.throughput_cases_per_second:.3f} | "
            f"{summary.p95_latency_ms:.2f} ms |"
        )
    return "\n".join(
        [
            "# 평가 비교 결과",
            "",
            f"데이터셋 SHA-256: `{baseline.dataset_sha256}`",
            "",
            *rows,
            "",
            "각 후보는 기준 실행에서 한 조건만 변경했습니다. 성능 비교에는 각 JSON 리포트의 환경과 모델 정보도 함께 사용해야 합니다.",
            "",
        ]
    )


def load_report(path: Path) -> EvaluationReport:
    """비교 대상 JSON 리포트를 Schema로 다시 검증해 읽는다."""
    return EvaluationReport.model_validate_json(path.read_text(encoding="utf-8"))
