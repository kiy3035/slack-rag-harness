from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re


STRUCTURED_FIELDS = (
    "request_id",
    "job_id",
    "thread_id",
    "reply_id",
    "outbox_id",
    "node",
    "method",
    "path",
    "route",
    "status_code",
    "status",
    "duration_ms",
    "attempt_count",
    "error_code",
)


class StructuredJsonFormatter(logging.Formatter):
    """민감 본문을 제외한 공통 추적 필드를 한 줄 JSON 로그로 직렬화한다."""

    def __init__(self, service_name: str) -> None:
        """로그를 발생시킨 로컬 서비스 이름을 모든 레코드에 고정한다."""
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        """표준 필드와 허용된 추적 필드만 JSON 객체로 만든다."""
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self._service_name,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field_name in STRUCTURED_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = str(value)
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_structured_logging(
    service_name: str,
    log_directory: str | None = None,
) -> None:
    """서비스별 표준 출력과 선택형 공유 파일에 중복 없이 JSON 로그를 기록한다."""
    if re.fullmatch(r"[a-z0-9-]+", service_name) is None:
        raise ValueError("서비스 이름은 소문자·숫자·하이픈만 허용합니다.")
    root_logger = logging.getLogger()
    if any(
        getattr(handler, "rag_harness_service", None) == service_name
        for handler in root_logger.handlers
    ):
        return

    formatter = StructuredJsonFormatter(service_name)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.rag_harness_service = service_name  # type: ignore[attr-defined]
    root_logger.addHandler(stream_handler)
    root_logger.setLevel(logging.INFO)

    if log_directory is None or not log_directory.strip():
        return
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(
        directory / f"{service_name}.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.rag_harness_service = service_name  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)
