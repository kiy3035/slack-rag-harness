from threading import Thread

from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST, start_http_server
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.domain import JobStatus, ReviewStatus
from app.db.models import AiJob, ReviewQueue


API_REQUESTS = Counter(
    "rag_harness_api_requests_total",
    "API 요청 건수",
    ("method", "route", "status_code"),
)
API_REQUEST_DURATION = Histogram(
    "rag_harness_api_request_duration_seconds",
    "API 요청 처리시간",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
JOB_STATUS_COUNT = Gauge(
    "rag_harness_jobs",
    "현재 작업 상태별 건수",
    ("status",),
)
REVIEW_STATUS_COUNT = Gauge(
    "rag_harness_review_queue",
    "현재 검토 상태별 건수",
    ("status",),
)
WORKFLOW_DURATION = Histogram(
    "rag_harness_workflow_duration_seconds",
    "전체 Workflow 처리시간",
    ("status",),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
WORKFLOW_NODE_DURATION = Histogram(
    "rag_harness_workflow_node_duration_seconds",
    "Workflow 노드별 처리시간",
    ("node", "outcome"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 60),
)
OLLAMA_CALL_DURATION = Histogram(
    "rag_harness_ollama_call_duration_seconds",
    "Ollama 작업별 호출시간",
    ("operation", "outcome"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
OUTBOX_EVENTS = Counter(
    "rag_harness_outbox_events_total",
    "Outbox 발행 주기 결과 건수",
    ("outcome",),
)
RECOVERY_EVENTS = Counter(
    "rag_harness_recovery_events_total",
    "Worker 재시도와 Lease 복구 건수",
    ("outcome",),
)
DLQ_EVENTS = Counter(
    "rag_harness_dlq_events_total",
    "DLQ 발행 결과 건수",
    ("outcome",),
)


def record_api_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    """낮은 카디널리티 Route 기준으로 API 건수와 지연시간을 기록한다."""
    API_REQUESTS.labels(method, route, str(status_code)).inc()
    API_REQUEST_DURATION.labels(method, route).observe(duration_seconds)


def observe_workflow(*, status: str, duration_seconds: float) -> None:
    """Workflow 전체 종료 상태와 처리시간을 기록한다."""
    WORKFLOW_DURATION.labels(status).observe(duration_seconds)


def observe_workflow_node(
    *, node: str, outcome: str, duration_seconds: float
) -> None:
    """Workflow 노드별 성공·실패 처리시간을 기록한다."""
    WORKFLOW_NODE_DURATION.labels(node, outcome).observe(duration_seconds)


def observe_ollama_call(
    *, operation: str, outcome: str, duration_seconds: float
) -> None:
    """로컬 Ollama 호출 종류와 결과별 처리시간을 기록한다."""
    OLLAMA_CALL_DURATION.labels(operation, outcome).observe(duration_seconds)


def record_outbox_cycle(
    *, sent: int, failed: int, exhausted: int, recovered: int
) -> None:
    """Outbox 발행 성공·실패·소진·Lease 복구 건수를 누적한다."""
    OUTBOX_EVENTS.labels("sent").inc(sent)
    OUTBOX_EVENTS.labels("failed").inc(failed)
    OUTBOX_EVENTS.labels("exhausted").inc(exhausted)
    OUTBOX_EVENTS.labels("recovered").inc(recovered)


def record_recovery_cycle(
    *,
    retries_released: int,
    stale_retried: int,
    stale_exhausted: int,
    dlq_published: int,
    dlq_failed: int,
) -> None:
    """재시도 해제·만료 Lease·DLQ 결과를 관측 카운터에 반영한다."""
    RECOVERY_EVENTS.labels("retry_released").inc(retries_released)
    RECOVERY_EVENTS.labels("stale_retried").inc(stale_retried)
    RECOVERY_EVENTS.labels("stale_exhausted").inc(stale_exhausted)
    DLQ_EVENTS.labels("published").inc(dlq_published)
    DLQ_EVENTS.labels("failed").inc(dlq_failed)


async def refresh_database_metrics(session: AsyncSession) -> None:
    """DB 원장을 집계해 작업 상태와 검토 큐 Gauge를 최신값으로 교체한다."""
    job_rows = await session.execute(
        select(AiJob.status, func.count()).group_by(AiJob.status)
    )
    review_rows = await session.execute(
        select(ReviewQueue.status, func.count()).group_by(ReviewQueue.status)
    )
    jobs = {status: count for status, count in job_rows}
    reviews = {status: count for status, count in review_rows}
    for status in JobStatus:
        JOB_STATUS_COUNT.labels(status.value).set(jobs.get(status, 0))
    for status in ReviewStatus:
        REVIEW_STATUS_COUNT.labels(status.value).set(reviews.get(status, 0))


def render_metrics() -> tuple[bytes, str]:
    """Prometheus 텍스트 포맷 본문과 정확한 Content-Type을 반환한다."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def start_metrics_server(port: int) -> tuple[object, Thread]:
    """API 외 프로세스가 독립적으로 Scrape될 HTTP 서버를 백그라운드에 연다."""
    return start_http_server(port=port, registry=REGISTRY)
