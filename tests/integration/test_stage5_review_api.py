from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.config import Settings, get_settings
from app.common.domain import EventSource, JobStatus, OutboxStatus, ReviewStatus
from app.db.models import (
    AiJob,
    AnswerCitation,
    JobOutbox,
    KnowledgeChunk,
    KnowledgeDocument,
    ReviewQueue,
)
from app.db.session import get_session
from app.main import create_app


pytestmark = pytest.mark.integration
DOCUMENT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CHUNK_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def review_test_settings() -> Settings:
    """검토 API 통합 테스트에 사용할 로컬 설정을 반환한다."""
    return Settings(enable_local_events=True)


def build_review_app(engine: AsyncEngine) -> FastAPI:
    """실제 테스트 DB Session을 검토 API에 주입한 FastAPI 앱을 만든다."""
    application = create_app()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        """각 검토 HTTP 요청에 독립적인 실제 DB Session을 제공한다."""
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = review_test_settings
    return application


async def prepare_review(
    engine: AsyncEngine,
    *,
    with_draft: bool = True,
) -> tuple[async_sessionmaker[AsyncSession], UUID, UUID]:
    """검토 결정 테스트용 작업·Outbox·문서·Chunk·검토 행을 생성한다."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    job_id = uuid4()
    review_id = uuid4()
    outbox_id = uuid4()
    vector = "[" + ",".join(["0"] * 767 + ["1"]) + "]"
    async with session_factory() as session, session.begin():
        session.add(
            KnowledgeDocument(
                document_id=DOCUMENT_ID,
                source_path="knowledge/manuals/review-api.md",
                title="검토 API 근거",
                content_hash="a" * 64,
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
                heading="승인 근거",
                content="검토자가 확인할 근거",
                content_hash="b" * 64,
                embedding=vector,
            )
        )
        session.add(
            AiJob(
                job_id=job_id,
                source=EventSource.LOCAL,
                external_event_id=f"review-api-{job_id}",
                question="검토가 필요한 질문",
                status=JobStatus.REVIEW_REQUIRED,
                attempt_count=1,
                workflow_revision=0,
            )
        )
        await session.flush()
        session.add(
            JobOutbox(
                outbox_id=outbox_id,
                job_id=job_id,
                status=OutboxStatus.SENT,
                attempt_count=0,
            )
        )
        allowed = {
            "document_id": str(DOCUMENT_ID),
            "chunk_id": str(CHUNK_ID),
            "similarity_score": 0.93,
        }
        session.add(
            ReviewQueue(
                review_id=review_id,
                job_id=job_id,
                workflow_revision=0,
                reason_code="MODEL_REVIEW_REQUIRED",
                draft_answer="검토된 초안" if with_draft else None,
                draft_citations=(
                    [
                        {
                            "document_id": str(DOCUMENT_ID),
                            "chunk_id": str(CHUNK_ID),
                        }
                    ]
                    if with_draft
                    else []
                ),
                allowed_citations=[allowed],
                status=ReviewStatus.WAITING,
            )
        )
    return session_factory, job_id, review_id


@pytest.mark.asyncio
async def test_review_approval_is_idempotent_and_persists_one_citation(
    clean_database: AsyncEngine,
) -> None:
    """동일 승인 요청을 두 번 호출해도 작업 완료와 인용이 한 번만 반영되는지 검증한다."""
    session_factory, job_id, review_id = await prepare_review(clean_database)
    transport = ASGITransport(app=build_review_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/v1/reviews/{review_id}/approve",
            json={"comment": "근거 확인"},
        )
        second = await client.post(
            f"/api/v1/reviews/{review_id}/approve",
            json={"comment": "중복 호출"},
        )
        listed = await client.get("/api/v1/reviews", params={"status": "APPROVED"})

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        citation_count = await session.scalar(
            select(func.count()).select_from(AnswerCitation)
        )
    assert first.status_code == 200 and first.json()["idempotent"] is False
    assert second.status_code == 200 and second.json()["idempotent"] is True
    assert listed.status_code == 200 and listed.json()["count"] == 1
    assert job is not None and job.status == JobStatus.COMPLETED
    assert job.result_answer == "검토된 초안"
    assert citation_count == 1


@pytest.mark.asyncio
async def test_edit_approval_rejects_citation_outside_original_search(
    clean_database: AsyncEngine,
) -> None:
    """수정 승인에 원 검색 집합 밖 인용을 넣으면 작업 상태를 바꾸지 않는지 검증한다."""
    session_factory, job_id, review_id = await prepare_review(clean_database)
    transport = ASGITransport(app=build_review_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/reviews/{review_id}/edit-approve",
            json={
                "answer": "조작된 인용을 사용한 답변",
                "citations": [
                    {
                        "document_id": str(DOCUMENT_ID),
                        "chunk_id": str(uuid4()),
                    }
                ],
            },
        )

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        review = await session.get(ReviewQueue, review_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "REVIEW_CITATION_NOT_ALLOWED"
    assert job is not None and job.status == JobStatus.REVIEW_REQUIRED
    assert review is not None and review.status == ReviewStatus.WAITING


@pytest.mark.asyncio
async def test_edit_approval_persists_reviewed_answer_and_allowed_citation(
    clean_database: AsyncEngine,
) -> None:
    """수정 승인 답변과 허용된 인용이 완료 작업에 원자적으로 저장되는지 검증한다."""
    session_factory, job_id, review_id = await prepare_review(clean_database)
    transport = ASGITransport(app=build_review_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/reviews/{review_id}/edit-approve",
            json={
                "answer": "검토자가 근거에 맞게 수정한 답변",
                "citations": [
                    {
                        "document_id": str(DOCUMENT_ID),
                        "chunk_id": str(CHUNK_ID),
                    }
                ],
                "comment": "표현 수정",
            },
        )

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        review = await session.get(ReviewQueue, review_id)
        citation_count = await session.scalar(
            select(func.count()).select_from(AnswerCitation)
        )
    assert response.status_code == 200
    assert response.json()["review_status"] == ReviewStatus.EDITED.value
    assert job is not None and job.status == JobStatus.COMPLETED
    assert job.result_answer == "검토자가 근거에 맞게 수정한 답변"
    assert review is not None and review.review_comment == "표현 수정"
    assert citation_count == 1


@pytest.mark.asyncio
async def test_review_retry_is_idempotent_and_opens_outbox_once(
    clean_database: AsyncEngine,
) -> None:
    """재검색 요청 중복 호출이 Workflow 세대를 한 번만 올리고 Outbox를 READY로 만드는지 검증한다."""
    session_factory, job_id, review_id = await prepare_review(
        clean_database, with_draft=False
    )
    transport = ASGITransport(app=build_review_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/v1/reviews/{review_id}/retry",
            json={"comment": "문서 갱신 후 재검색"},
        )
        second = await client.post(
            f"/api/v1/reviews/{review_id}/retry",
            json={"comment": "중복 재검색"},
        )

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
        outbox = await session.scalar(
            select(JobOutbox).where(JobOutbox.job_id == job_id)
        )
    assert first.status_code == 200 and first.json()["idempotent"] is False
    assert second.status_code == 200 and second.json()["idempotent"] is True
    assert job is not None and job.workflow_revision == 1
    assert job.status == JobStatus.RECEIVED
    assert outbox is not None and outbox.status == OutboxStatus.READY


@pytest.mark.asyncio
async def test_review_rejection_is_idempotent_and_missing_review_is_404(
    clean_database: AsyncEngine,
) -> None:
    """반려 중복 호출은 한 결과로 수렴하고 존재하지 않는 검토는 404인지 검증한다."""
    session_factory, job_id, review_id = await prepare_review(clean_database)
    transport = ASGITransport(app=build_review_app(clean_database))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/v1/reviews/{review_id}/reject",
            json={"comment": "근거 부족"},
        )
        second = await client.post(
            f"/api/v1/reviews/{review_id}/reject",
            json={"comment": "중복 반려"},
        )
        missing = await client.get(f"/api/v1/reviews/{uuid4()}")

    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
    assert first.status_code == 200 and first.json()["idempotent"] is False
    assert second.status_code == 200 and second.json()["idempotent"] is True
    assert missing.status_code == 404
    assert job is not None and job.status == JobStatus.REJECTED
