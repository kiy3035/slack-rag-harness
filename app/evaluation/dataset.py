import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from app.evaluation.schemas import EvaluationCase, EvaluationCategory


class EvaluationDatasetError(ValueError):
    """평가 데이터셋의 형식·중복·필수 분류 계약 위반을 나타낸다."""


def load_dataset(path: Path, *, minimum_cases: int = 50) -> list[EvaluationCase]:
    """JSONL 전체를 검증하고 중복 ID와 필수 질문 유형 누락을 거부한다."""
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise EvaluationDatasetError(f"평가 데이터셋을 읽을 수 없습니다: {path}") from error
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            case = EvaluationCase.model_validate(json.loads(raw_line))
        except (json.JSONDecodeError, ValidationError) as error:
            raise EvaluationDatasetError(
                f"평가 데이터셋 {line_number}번째 줄이 올바르지 않습니다."
            ) from error
        if case.case_id in seen_ids:
            raise EvaluationDatasetError(f"중복 case_id입니다: {case.case_id}")
        missing_paths = [
            document_path
            for document_path in case.expected_document_paths
            if not Path(document_path).is_file()
        ]
        if missing_paths:
            raise EvaluationDatasetError(
                f"존재하지 않는 기대 문서입니다: {', '.join(missing_paths)}"
            )
        seen_ids.add(case.case_id)
        cases.append(case)

    if len(cases) < minimum_cases:
        raise EvaluationDatasetError(
            f"평가 데이터셋은 최소 {minimum_cases}건이어야 합니다: {len(cases)}건"
        )
    present_categories = {case.category for case in cases}
    missing = set(EvaluationCategory) - present_categories
    if missing:
        names = ", ".join(sorted(category.value for category in missing))
        raise EvaluationDatasetError(f"필수 평가 유형이 누락됐습니다: {names}")
    return cases


def dataset_sha256(path: Path) -> str:
    """비교 실행이 같은 데이터셋을 사용했는지 확인할 SHA-256을 계산한다."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise EvaluationDatasetError(f"평가 데이터셋을 읽을 수 없습니다: {path}") from error


def category_counts(cases: list[EvaluationCase]) -> dict[str, int]:
    """검증된 데이터셋의 질문 유형별 건수를 안정적인 키로 집계한다."""
    return {
        category.value: sum(case.category == category for case in cases)
        for category in EvaluationCategory
    }
