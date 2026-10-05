import json
from pathlib import Path
import re

from pydantic import ValidationError

from app.evaluation.schemas import (
    EvaluationCase,
    EvaluationCheckpoint,
    EvaluationConfig,
    EvaluationEnvironment,
)


class EvaluationCheckpointError(ValueError):
    """평가 체크포인트가 없거나 현재 실행 계약과 다를 때 발생한다."""


def checkpoint_path(output_directory: Path, run_id: str) -> Path:
    """실행 ID별 체크포인트가 최종 결과와 같은 디렉터리에 놓이도록 경로를 만든다."""
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,127}", run_id) is None:
        raise EvaluationCheckpointError("실행 ID 형식이 올바르지 않습니다.")
    return output_directory / f"{run_id}.checkpoint.json"


def write_checkpoint(checkpoint: EvaluationCheckpoint, path: Path) -> None:
    """완료된 Case 스냅샷을 임시 파일 교체 방식으로 원자적으로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def load_checkpoint(path: Path) -> EvaluationCheckpoint:
    """중단된 실행의 체크포인트를 Pydantic Schema로 검증해 읽는다."""
    try:
        return EvaluationCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EvaluationCheckpointError(
            f"평가 체크포인트를 읽을 수 없습니다: {path}"
        ) from error
    except ValidationError as error:
        raise EvaluationCheckpointError(
            f"평가 체크포인트 형식이 올바르지 않습니다: {path}"
        ) from error


def validate_checkpoint_context(
    checkpoint: EvaluationCheckpoint,
    *,
    dataset_hash: str,
    config: EvaluationConfig,
    environment: EvaluationEnvironment,
    cases: list[EvaluationCase],
) -> None:
    """데이터·설정·모델 환경이 같은 실행만 재개하고 알 수 없는 Case를 거부한다."""
    if checkpoint.dataset_sha256 != dataset_hash:
        raise EvaluationCheckpointError(
            "체크포인트와 현재 평가 데이터셋 SHA-256이 다릅니다."
        )
    if checkpoint.config != config:
        raise EvaluationCheckpointError(
            "체크포인트와 현재 평가 설정이 다릅니다."
        )
    if checkpoint.environment != environment:
        raise EvaluationCheckpointError(
            "체크포인트와 현재 실행 환경 또는 모델이 다릅니다."
        )
    case_ids = {case.case_id for case in cases}
    unknown_case_ids = sorted(
        prediction.case_id
        for prediction in checkpoint.predictions
        if prediction.case_id not in case_ids
    )
    if unknown_case_ids:
        raise EvaluationCheckpointError(
            "체크포인트에 현재 데이터셋에 없는 Case가 있습니다: "
            + ", ".join(unknown_case_ids)
        )
