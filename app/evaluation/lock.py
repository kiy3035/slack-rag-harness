from datetime import UTC, datetime
import json
import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from app.evaluation.checkpoint import checkpoint_path


class EvaluationRunLockedError(RuntimeError):
    """같은 실행 ID를 다른 평가 프로세스가 이미 소유하고 있음을 나타낸다."""


def run_lock_path(output_directory: Path, run_id: str) -> Path:
    """검증된 실행 ID로 프로세스 간 잠금 파일 경로를 만든다."""
    validated_path = checkpoint_path(output_directory, run_id)
    return validated_path.with_name(f"{run_id}.lock")


class EvaluationRunLock:
    """프로세스 종료 시 자동 해제되는 OS 파일 잠금으로 동일 실행의 중복 수행을 막는다."""

    def __init__(self, path: Path) -> None:
        """잠금 파일 경로를 보관하고 아직 소유하지 않은 상태로 초기화한다."""
        self._path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "EvaluationRunLock":
        """잠금을 비차단 방식으로 획득하고 진단용 소유자 정보를 기록한다."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            self._lock_handle(handle)
        except OSError as error:
            handle.close()
            raise EvaluationRunLockedError(
                f"같은 실행 ID의 평가가 이미 진행 중입니다: {self._path.stem}"
            ) from error
        self._handle = handle
        owner = {
            "pid": os.getpid(),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(owner).encode("utf-8") + b"\n")
        handle.flush()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """성공·오류·중단 여부와 무관하게 OS 잠금과 파일 핸들을 해제한다."""
        del exception_type, exception, traceback
        if self._handle is None:
            return
        try:
            self._unlock_handle(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        """현재 운영체제의 비차단 배타 잠금을 파일 첫 바이트에 설정한다."""
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        """현재 운영체제에서 획득한 배타 잠금을 명시적으로 해제한다."""
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
