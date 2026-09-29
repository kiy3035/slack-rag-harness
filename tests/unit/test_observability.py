import json
import logging
from pathlib import Path

from app.common.logging import StructuredJsonFormatter


def test_structured_formatter_keeps_trace_fields_without_sensitive_body() -> None:
    """JSON 로그가 추적 식별자는 보존하고 질문·답변 같은 임의 필드는 제외하는지 검증한다."""
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="workflow_completed",
        args=(),
        exc_info=None,
    )
    record.job_id = "job-safe"  # type: ignore[attr-defined]
    record.thread_id = "thread-safe"  # type: ignore[attr-defined]
    record.question = "기록되면 안 되는 질문"  # type: ignore[attr-defined]
    record.answer = "기록되면 안 되는 답변"  # type: ignore[attr-defined]

    payload = json.loads(StructuredJsonFormatter("worker").format(record))

    assert payload["service"] == "worker"
    assert payload["event"] == "workflow_completed"
    assert payload["job_id"] == "job-safe"
    assert payload["thread_id"] == "thread-safe"
    assert "question" not in payload
    assert "answer" not in payload


def test_grafana_dashboard_contains_every_required_stage8_panel() -> None:
    """Provisioning Dashboard에 로드맵의 필수 관측 패널이 모두 있는지 검증한다."""
    dashboard = json.loads(
        Path("observability/grafana/dashboards/slack-rag-harness.json").read_text(
            encoding="utf-8"
        )
    )
    titles = {panel["title"] for panel in dashboard["panels"]}

    assert {
        "API 요청량",
        "API p95 응답시간",
        "작업 상태별 건수",
        "RabbitMQ Queue Depth",
        "전체 처리시간",
        "노드별 처리시간",
        "재시도 및 DLQ 건수",
        "검토 큐 적체량",
        "Ollama 호출시간",
        "구조화 로그",
    }.issubset(titles)


def test_observability_configs_use_only_local_services() -> None:
    """관측 설정이 유료 외부 Endpoint가 아니라 Compose 내부 서비스만 참조하는지 검증한다."""
    config_paths = (
        Path("observability/prometheus/prometheus.yml"),
        Path("observability/alloy/config.alloy"),
        Path("observability/grafana/provisioning/datasources/datasources.yml"),
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in config_paths)

    assert "grafana.com" not in combined
    assert "prometheus:9090" in combined
    assert "loki:3100" in combined
    assert "rabbitmq:15692" in combined
