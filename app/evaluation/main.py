import argparse
import asyncio
import json
from pathlib import Path

from app.common.config import get_settings
from app.evaluation.checkpoint import checkpoint_path
from app.evaluation.dataset import category_counts, dataset_sha256, load_dataset
from app.evaluation.lock import EvaluationRunLock, run_lock_path
from app.evaluation.reporting import (
    load_report,
    render_comparison_report,
    write_report,
)
from app.evaluation.runner import create_run_id, run_live_evaluation
from app.evaluation.schemas import EvaluationConfig


DEFAULT_DATASET = Path("evaluation/datasets/v1.jsonl")
DEFAULT_OUTPUT_DIRECTORY = Path("evaluation/results")


def build_parser() -> argparse.ArgumentParser:
    """데이터셋 검증·실행·단일 조건 비교 명령의 인자 계약을 만든다."""
    parser = argparse.ArgumentParser(description="로컬 RAG 평가 하네스")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="JSONL 데이터셋을 검증합니다.")
    validate.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)

    run = subparsers.add_parser("run", help="실제 로컬 검색과 Workflow를 평가합니다.")
    run.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    run.add_argument("--top-k", type=int, default=5)
    run.add_argument("--min-score", type=float, default=-1.0)
    run.add_argument("--max-chunks-per-document", type=int, default=2)
    run.add_argument("--max-query-rewrites", type=int, default=1)
    run.add_argument("--worker-count", type=int, default=1)
    run.add_argument(
        "--run-id",
        help="같은 실행 ID의 체크포인트가 있으면 완료된 Case 이후부터 재개합니다.",
    )

    compare = subparsers.add_parser(
        "compare",
        help="기준에서 한 조건씩 바뀐 JSON 리포트를 비교합니다.",
    )
    compare.add_argument("reports", type=Path, nargs="+")
    compare.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY / "comparison.md",
    )
    return parser


def _validate_dataset(path: Path) -> int:
    """데이터셋 건수·분포·해시를 출력하고 성공 종료 코드를 반환한다."""
    cases = load_dataset(path)
    print(
        json.dumps(
            {
                "dataset": path.as_posix(),
                "sha256": dataset_sha256(path),
                "case_count": len(cases),
                "categories": category_counts(cases),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_evaluation(arguments: argparse.Namespace) -> int:
    """CLI 조건으로 실제 평가를 실행하고 JSON·Markdown 경로를 출력한다."""
    config = EvaluationConfig(
        top_k=arguments.top_k,
        min_score=arguments.min_score,
        max_chunks_per_document=arguments.max_chunks_per_document,
        max_query_rewrites=arguments.max_query_rewrites,
        worker_count=arguments.worker_count,
    )
    run_id = arguments.run_id or create_run_id(config)
    current_checkpoint_path = checkpoint_path(arguments.output_dir, run_id)
    final_json_path = arguments.output_dir / f"{run_id}.json"

    def print_progress(completed: int, total: int, case_id: str | None) -> None:
        """장시간 실행의 재개 지점과 Case별 완료 진행률을 한 줄 JSON으로 출력한다."""
        event = "resumed" if case_id is None and completed else "started"
        if case_id is not None:
            event = "case_completed"
        print(
            json.dumps(
                {
                    "event": event,
                    "run_id": run_id,
                    "completed": completed,
                    "total": total,
                    "case_id": case_id,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    with EvaluationRunLock(run_lock_path(arguments.output_dir, run_id)):
        if final_json_path.exists() and not current_checkpoint_path.exists():
            raise ValueError("이미 완료된 실행 ID입니다. 새 실행 ID를 사용하세요.")
        report = asyncio.run(
            run_live_evaluation(
                arguments.dataset,
                get_settings(),
                config,
                output_directory=arguments.output_dir,
                run_id=run_id,
                progress_callback=print_progress,
            )
        )
        json_path, markdown_path = write_report(report, arguments.output_dir)
        current_checkpoint_path.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "json_report": json_path.as_posix(),
                "markdown_report": markdown_path.as_posix(),
                "summary": report.summary.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _compare_reports(report_paths: list[Path], output_path: Path) -> int:
    """검증된 실행 리포트를 비교하고 Markdown 파일로 원자적으로 저장한다."""
    reports = [load_report(path) for path in report_paths]
    content = render_comparison_report(reports)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(output_path)
    print(output_path.as_posix())
    return 0


def main() -> int:
    """선택한 평가 하네스 하위 명령을 실행하고 종료 코드를 반환한다."""
    arguments = build_parser().parse_args()
    if arguments.command == "validate":
        return _validate_dataset(arguments.dataset)
    if arguments.command == "run":
        return _run_evaluation(arguments)
    if arguments.command == "compare":
        return _compare_reports(arguments.reports, arguments.output)
    raise RuntimeError("지원하지 않는 평가 명령입니다.")


if __name__ == "__main__":
    raise SystemExit(main())
