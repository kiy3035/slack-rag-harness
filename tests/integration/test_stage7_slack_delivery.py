from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.domain import EventSource, JobStatus, OutboxStatus, ReviewStatus
from app.db.models import AiJob, KnowledgeChunk, KnowledgeDocument, ReviewQueue, SlackReplyOutbox
from app.integrations.slack.client import SlackPostResult, SlackPublishError
from app.integrations.slack.publisher import SlackReplyPublisher
from app.integrations.slack.repository import SlackReplyRepository, enqueue_slack_reply
from app.recovery.errors import RetryPolicy
from app.retrieval.schemas import SearchHit
from app.reviews.schemas import ReviewDecisionRequest
from app.reviews.service import ReviewService
from app.workflow.repository import WorkflowRepository
from app.workflow.schemas import AnswerOutput, CitationOutput, WorkflowResult


pytestmark = pytest.mark.integration
FIXED_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
DOCUMENT_ID = UUID("71000000-0000-0000-0000-000000000001")
CHUNK_ID = UUID("71000000-0000-0000-0000-000000000002")


class SequencedSlackClient:
    """Slack 발신 결과를 순서대로 반환해 재시도 경계를 재현한다."""

    def __init__(self, outcomes: list[SlackPostResult | SlackPublishError]) -> None:
        """호출별 성공 또는 실패 결과와 관찰 목록을 초기화한다."""
        self._outcomes = outcomes
        self.calls: list[dict[str, str]] = []

    async def post_thread_reply(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        text: str,
        client_message_id: str,
    ) -> SlackPostResult:
        """요청 대상을 기록하고 준비한 결과 또는 예외를 반환한다."""
        self.calls.append(
            {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "text": text,
                "client_message_id": client_message_id,
            }
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, SlackPublishError):
            raise outcome
        return outcome


async def create_slack_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: JobStatus,
) -> UUID:
    """Slack 회신 대상을 가진 테스트 작업을 지정 상태로 저장한다."""
    job_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AiJob(
                job_id=job_id,
                source=EventSource.SLACK,
                external_event_id=f"stage7-{job_id}",
                question="정산 절차는?",
                slack_channel_id="C_STAGE7",
                slack_message_ts="1710000000.000100",
                slack_thread_ts="1710000000.000100",
                status=status,
                attempt_count=1,
                workflow_revision=0,
            )
        )
    return job_id


async def create_knowledge(session_factory: async_sessionmaker[AsyncSession]) -> SearchHit:
    """완료 답변 인용이 참조할 실제 문서와 Chunk를 생성한다."""
    vector = "[" + ",".join(["0"] * 767 + ["1"]) + "]"
    async with session_factory() as session, session.begin():
        session.add(
            KnowledgeDocument(
                document_id=DOCUMENT_ID,
                source_path="knowledge/manuals/stage7.md",
                title="7단계 테스트 문서",
                content_hash="7" * 64,
                current_version=1,
            )
        )
        await session.flush()
        session.add(
            KnowledgeChunk(
                chunk_id=CHUNK_ID,
                document_id=DOCUMENT_ID,
                document_version=1,
                chunk_index=0,
                heading="정산",
                content="정산 절차 근거",
                content_hash="8" * 64,
                embedding=vector,
            )
        )
    return SearchHit(
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        source_path="knowledge/manuals/stage7.md",
        title="7단계 테스트 문서",
        document_version=1,
        chunk_index=0,
        heading="정산",
        content="정산 절차 근거",
        score=0.95,
    )


@pytest.mark.asyncio
async def test_workflow_completion_enqueues_and_publishes_original_thread_reply(
    clean_database: AsyncEngine,
) -> None:
    """Workflow 완료와 Slack 예약이 원자적으로 저장되고 원본 Thread로 발행되는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    hit = await create_knowledge(session_factory)
    job_id = await create_slack_job(session_factory, status=JobStatus.PROCESSING)
    answer = AnswerOutput(
        answer="검증된 정산 답변",
        citations=[CitationOutput(document_id=DOCUMENT_ID, chunk_id=CHUNK_ID)],
    )
    result = WorkflowResult(
        job_id=job_id,
        thread_id=f"{job_id}:run:0",
        status=JobStatus.COMPLETED,
        answer=answer,
        retrieved_chunk_ids=[CHUNK_ID],
        retrieved_chunks=[hit],
        relevant_chunks=[hit],
        review_reason_code=None,
        validation_errors=[],
        workflow_revision=0,
    )
    assert await WorkflowRepository(session_factory).record_outcome(result, now=FIXED_NOW)

    fake_client = SequencedSlackClient(
        [SlackPostResult(message_ts="1710000001.000200")]
    )
    publisher = SlackReplyPublisher(
        repository=SlackReplyRepository(session_factory),
        client=fake_client,  # type: ignore[arg-type]
        retry_policy=RetryPolicy(max_attempts=3, base_seconds=2, max_seconds=60),
        lease_seconds=30,
    )
    cycle = await publisher.process_once(now=FIXED_NOW)

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        reply = await session.scalar(
            select(SlackReplyOutbox).where(SlackReplyOutbox.job_id == job_id)
        )
    assert job is not None and job.status == JobStatus.COMPLETED
    assert cycle.sent is True
    assert fake_client.calls[0]["channel_id"] == "C_STAGE7"
    assert fake_client.calls[0]["thread_ts"] == "1710000000.000100"
    assert fake_client.calls[0]["text"] == "검증된 정산 답변"
    assert reply is not None and reply.status == OutboxStatus.SENT
    assert reply.slack_message_ts == "1710000001.000200"


@pytest.mark.asyncio
async def test_rate_limit_waits_for_retry_after_and_stops_at_attempt_limit(
    clean_database: AsyncEngine,
) -> None:
    """429 Retry-After를 지키고 최대 횟수 뒤에는 Slack 발신을 FAIL로 종료하는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    job_id = await create_slack_job(session_factory, status=JobStatus.COMPLETED)
    async with session_factory() as session, session.begin():
        assert await enqueue_slack_reply(session, job_id=job_id, answer="재시도 답변")
    fake_client = SequencedSlackClient(
        [
            SlackPublishError(
                "SLACK_RATE_LIMITED", transient=True, retry_after_seconds=370
            ),
            SlackPublishError("SLACK_TIMEOUT", transient=True),
        ]
    )
    publisher = SlackReplyPublisher(
        repository=SlackReplyRepository(session_factory),
        client=fake_client,  # type: ignore[arg-type]
        retry_policy=RetryPolicy(max_attempts=2, base_seconds=2, max_seconds=60),
        lease_seconds=30,
    )

    first = await publisher.process_once(now=FIXED_NOW)
    early = await publisher.process_once(now=FIXED_NOW + timedelta(seconds=369))
    exhausted = await publisher.process_once(now=FIXED_NOW + timedelta(seconds=370))

    async with session_factory() as session:
        reply = await session.scalar(
            select(SlackReplyOutbox).where(SlackReplyOutbox.job_id == job_id)
        )
    assert first.retry_scheduled is True
    assert early.claimed is False
    assert exhausted.failed is True
    assert len(fake_client.calls) == 2
    assert reply is not None and reply.status == OutboxStatus.FAIL
    assert reply.attempt_count == 2
    assert reply.last_error == "SLACK_TIMEOUT"


@pytest.mark.asyncio
async def test_review_approval_enqueues_slack_reply_once(
    clean_database: AsyncEngine,
) -> None:
    """사람이 승인한 답변도 같은 작업의 Slack Outbox 한 건으로만 예약되는지 검증한다."""
    session_factory = async_sessionmaker(clean_database, expire_on_commit=False)
    await create_knowledge(session_factory)
    job_id = await create_slack_job(session_factory, status=JobStatus.REVIEW_REQUIRED)
    review_id = uuid4()
    citation = {"document_id": str(DOCUMENT_ID), "chunk_id": str(CHUNK_ID)}
    allowed = {**citation, "similarity_score": 0.95}
    async with session_factory() as session, session.begin():
        session.add(
            ReviewQueue(
                review_id=review_id,
                job_id=job_id,
                workflow_revision=0,
                reason_code="SENSITIVE_INTENT",
                draft_answer="검토 승인 답변",
                draft_citations=[citation],
                allowed_citations=[allowed],
                status=ReviewStatus.WAITING,
            )
        )

    async with session_factory() as session:
        service = ReviewService(session)
        first = await service.approve(review_id, ReviewDecisionRequest(comment="승인"))
        duplicate = await service.approve(
            review_id, ReviewDecisionRequest(comment="중복 승인")
        )
    async with session_factory() as session:
        replies = list(
            await session.scalars(
                select(SlackReplyOutbox).where(SlackReplyOutbox.job_id == job_id)
            )
        )
    assert first.idempotent is False
    assert duplicate.idempotent is True
    assert len(replies) == 1
    assert replies[0].message_text == "검토 승인 답변"
