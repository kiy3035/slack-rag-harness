from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.domain import EventSource, JobStatus, ReviewStatus
from app.db.models import AiJob, ReviewQueue


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class AdminJobRow:
    """질문·답변을 제외한 최근 작업의 운영 식별 정보만 담는다."""

    job_id: UUID
    source: EventSource
    status: JobStatus
    attempt_count: int
    failure_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminReviewRow:
    """초안·검토 의견을 제외한 최근 검토의 상태 정보만 담는다."""

    review_id: UUID
    job_id: UUID
    status: ReviewStatus
    reason_code: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminOverview:
    """최소 관리 화면에 필요한 집계와 안전한 최근 목록을 묶는다."""

    job_counts: dict[JobStatus, int]
    review_counts: dict[ReviewStatus, int]
    jobs: list[AdminJobRow]
    reviews: list[AdminReviewRow]


async def load_admin_overview(
    session: AsyncSession,
    *,
    limit: int = 25,
) -> AdminOverview:
    """민감 본문을 읽지 않고 상태 집계와 최근 작업·검토 목록을 조회한다."""
    job_count_rows = await session.execute(
        select(AiJob.status, func.count()).group_by(AiJob.status)
    )
    review_count_rows = await session.execute(
        select(ReviewQueue.status, func.count()).group_by(ReviewQueue.status)
    )
    job_rows = await session.execute(
        select(
            AiJob.job_id,
            AiJob.source,
            AiJob.status,
            AiJob.attempt_count,
            AiJob.failure_code,
            AiJob.created_at,
        )
        .order_by(AiJob.created_at.desc(), AiJob.job_id)
        .limit(limit)
    )
    review_rows = await session.execute(
        select(
            ReviewQueue.review_id,
            ReviewQueue.job_id,
            ReviewQueue.status,
            ReviewQueue.reason_code,
            ReviewQueue.created_at,
        )
        .order_by(ReviewQueue.created_at.desc(), ReviewQueue.review_id)
        .limit(limit)
    )
    return AdminOverview(
        job_counts={status: count for status, count in job_count_rows},
        review_counts={status: count for status, count in review_count_rows},
        jobs=[AdminJobRow(*row) for row in job_rows],
        reviews=[AdminReviewRow(*row) for row in review_rows],
    )


def _status_cards(overview: AdminOverview) -> str:
    """작업과 대기 검토의 현재 건수를 요약 카드 HTML로 만든다."""
    important_statuses = (
        JobStatus.QUEUED,
        JobStatus.PROCESSING,
        JobStatus.RETRY_WAIT,
        JobStatus.REVIEW_REQUIRED,
        JobStatus.COMPLETED,
        JobStatus.DEAD_LETTER,
    )
    cards = [
        f'<article><span>{escape(status.value)}</span><strong>{overview.job_counts.get(status, 0)}</strong></article>'
        for status in important_statuses
    ]
    cards.append(
        '<article><span>WAITING REVIEWS</span><strong>'
        f'{overview.review_counts.get(ReviewStatus.WAITING, 0)}</strong></article>'
    )
    return "".join(cards)


def _format_korea_time(value: datetime) -> str:
    """UTC 저장 시각을 관리 화면에서 식별하기 쉬운 한국 표준시로 변환한다."""
    aware_value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware_value.astimezone(KOREA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S KST")


def _job_rows(overview: AdminOverview) -> str:
    """최근 작업을 민감 본문 없이 표 행으로 렌더링한다."""
    return "".join(
        "<tr>"
        f"<td><code>{escape(str(row.job_id))}</code></td>"
        f"<td>{escape(row.source.value)}</td>"
        f"<td><span class='badge'>{escape(row.status.value)}</span></td>"
        f"<td>{row.attempt_count}</td>"
        f"<td>{escape(row.failure_code or '-')}</td>"
        f"<td>{escape(_format_korea_time(row.created_at))}</td>"
        "</tr>"
        for row in overview.jobs
    ) or "<tr><td colspan='6'>작업이 없습니다.</td></tr>"


def _review_rows(overview: AdminOverview) -> str:
    """최근 검토를 초안과 의견 없이 표 행으로 렌더링한다."""
    return "".join(
        "<tr>"
        f"<td><code>{escape(str(row.review_id))}</code></td>"
        f"<td><code>{escape(str(row.job_id))}</code></td>"
        f"<td><span class='badge'>{escape(row.status.value)}</span></td>"
        f"<td>{escape(row.reason_code)}</td>"
        f"<td>{escape(_format_korea_time(row.created_at))}</td>"
        "</tr>"
        for row in overview.reviews
    ) or "<tr><td colspan='5'>검토가 없습니다.</td></tr>"


def render_admin_page(overview: AdminOverview) -> str:
    """로컬 운영자가 작업과 검토 적체를 확인할 단일 HTML 화면을 만든다."""
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="10">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Slack RAG Harness 관리 화면</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; background: #0b1020; color: #e7ecf7; }}
    body {{ margin: 0; padding: 28px; }}
    header {{ display: flex; justify-content: space-between; align-items: end; gap: 16px; }}
    h1, h2 {{ margin: 0 0 14px; }}
    p {{ color: #a9b4ca; }}
    a {{ color: #76b7ff; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 24px 0; }}
    article, section {{ background: #151d31; border: 1px solid #273450; border-radius: 12px; padding: 16px; }}
    article span {{ display: block; color: #91a0bb; font-size: 12px; }}
    article strong {{ display: block; margin-top: 8px; font-size: 28px; }}
    section {{ margin-top: 18px; overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #273450; padding: 10px; text-align: left; white-space: nowrap; }}
    th {{ color: #91a0bb; }}
    code {{ color: #b9d6ff; }}
    .badge {{ background: #26395d; border-radius: 999px; padding: 4px 8px; }}
  </style>
</head>
<body>
  <header>
    <div><h1>Slack RAG Harness</h1><p>질문·답변 본문을 노출하지 않는 로컬 운영 화면 · 한국 표준시(KST, UTC+9) · 10초 자동 새로고침</p></div>
    <nav><a href="/metrics">Prometheus Metrics</a> · <a href="http://localhost:3000/d/slack-rag-harness">Grafana</a></nav>
  </header>
  <div class="cards">{_status_cards(overview)}</div>
  <section><h2>최근 작업</h2><table><thead><tr><th>job_id</th><th>source</th><th>status</th><th>attempts</th><th>failure_code</th><th>created_at (KST)</th></tr></thead><tbody>{_job_rows(overview)}</tbody></table></section>
  <section><h2>최근 검토</h2><table><thead><tr><th>review_id</th><th>job_id</th><th>status</th><th>reason_code</th><th>created_at (KST)</th></tr></thead><tbody>{_review_rows(overview)}</tbody></table></section>
</body>
</html>"""
