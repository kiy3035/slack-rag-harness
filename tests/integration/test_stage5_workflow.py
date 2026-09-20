from collections.abc import Sequence
import os
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.domain import EventSource, JobStatus, ReviewStatus
from app.db.models import (
    AiJob,
    AnswerCitation,
    KnowledgeChunk,
    KnowledgeDocument,
    ReviewQueue,
)
from app.retrieval.schemas import SearchHit
from app.workflow.graph import WorkflowNodes, build_workflow_graph
from app.workflow.repository import WorkflowRepository
from app.workflow.schemas import (
    AnswerOutput,
    CitationOutput,
    DocumentGradeOutput,
    IntentCategory,
    IntentOutput,
    ReviewReasonCode,
    RewriteQueryOutput,
    RiskLevel,
    WorkflowRequest,
)
from app.workflow.service import WorkflowRunner


pytestmark = pytest.mark.integration
DOCUMENT_ID = UUID("77777777-7777-7777-7777-777777777777")
CHUNK_ID = UUID("88888888-8888-8888-8888-888888888888")
FORGED_CHUNK_ID = UUID("99999999-9999-9999-9999-999999999999")


class EmptyCountingSearch:
    """검색 결과가 없는 질문의 제한 재검색 횟수를 기록한다."""

    def __init__(self) -> None:
        """검색 호출 횟수를 0으로 초기화한다."""
        self.call_count = 0

    async def search(self, question: str) -> list[SearchHit]:
        """호출 횟수를 기록하고 항상 빈 검색 결과를 반환한다."""
        self.call_count += 1
        return []


class StaticSearch:
    """인용 검증 테스트에 사용할 한 개의 검색 Chunk를 반환한다."""

    async def search(self, question: str) -> list[SearchHit]:
        """질문과 관계없이 고정된 허용 Chunk를 반환한다."""
        return [build_hit()]


class Stage5Model:
    """관련성·재작성·조작 인용을 결정적으로 제어하는 모델 대역이다."""

    def __init__(
        self,
        *,
        forge_citation: bool = False,
        duplicate_citation: bool = False,
    ) -> None:
        """조작 인용 여부와 모델 호출 횟수를 초기화한다."""
        self._forge_citation = forge_citation
        self._duplicate_citation = duplicate_citation
        self.rewrite_calls = 0
        self.generation_calls = 0

    async def classify_intent(self, question: str) -> IntentOutput:
        """테스트 질문을 낮은 위험의 정보 조회로 분류한다."""
        return IntentOutput(
            intent=IntentCategory.INFORMATION,
            risk_level=RiskLevel.LOW,
            reason="읽기 전용 정보 질문",
        )

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """검색 결과가 있으면 첫 Chunk만 관련 근거로 판정한다."""
        return DocumentGradeOutput(
            relevant_chunk_ids=[chunks[0].chunk_id] if chunks else [],
            reason="직접 관련된 근거" if chunks else "관련 문서 없음",
        )

    async def rewrite_query(self, question: str, reason: str) -> RewriteQueryOutput:
        """호출 횟수를 기록하고 한 번 사용할 재검색어를 반환한다."""
        self.rewrite_calls += 1
        return RewriteQueryOutput(query=f"{question} 운영 매뉴얼")

    async def generate_answer(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> AnswerOutput:
        """정상 또는 검색 집합 밖 Chunk를 인용하는 답변을 반환한다."""
        self.generation_calls += 1
        first = chunks[0]
        citation = CitationOutput(
            document_id=first.document_id,
            chunk_id=(FORGED_CHUNK_ID if self._forge_citation else first.chunk_id),
        )
        return AnswerOutput(
            answer="문서에 따른 답변입니다.",
            citations=[citation, citation] if self._duplicate_citation else [citation],
        )


def build_hit() -> SearchHit:
    """조작 인용과 허용 인용을 비교할 고정 검색 결과를 만든다."""
    return SearchHit(
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        source_path="knowledge/manuals/stage5.md",
        title="5단계 테스트 문서",
        document_version=1,
        chunk_index=0,
        heading="근거",
        content="문서에 따른 답변입니다.",
        score=0.91,
    )


def checkpoint_url() -> str:
    """통합 테스트 DB URL을 Psycopg Checkpointer URL로 변환한다."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("TEST_DATABASE_URL이 필요합니다.")
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def create_processing_job(
    engine: AsyncEngine,
    *,
    question: str,
) -> tuple[async_sessionmaker[AsyncSession], UUID]:
    """Workflow 결과 저장에 사용할 PROCESSING 작업을 테스트 DB에 생성한다."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    job_id = uuid4()
    vector = "[" + ",".join(["0"] * 767 + ["1"]) + "]"
    async with session_factory() as session, session.begin():
        session.add(
            KnowledgeDocument(
                document_id=DOCUMENT_ID,
                source_path="knowledge/manuals/stage5.md",
                title="5단계 테스트 문서",
                content_hash="c" * 64,
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
                heading="근거",
                content="문서에 따른 답변입니다.",
                content_hash="d" * 64,
                embedding=vector,
            )
        )
        session.add(
            AiJob(
                job_id=job_id,
                source=EventSource.LOCAL,
                external_event_id=f"stage5-{job_id}",
                question=question,
                status=JobStatus.PROCESSING,
                attempt_count=1,
                workflow_revision=0,
            )
        )
    return session_factory, job_id


@pytest.mark.asyncio
async def test_missing_documents_are_rewritten_once_then_queued_for_review(
    clean_database: AsyncEngine,
) -> None:
    """문서 없는 질문이 한 번만 재검색된 뒤 검토 큐 한 건으로 이동하는지 검증한다."""
    session_factory, job_id = await create_processing_job(
        clean_database,
        question="문서에 없는 사내 우주선 절차는?",
    )
    search = EmptyCountingSearch()
    model = Stage5Model()
    repository = WorkflowRepository(session_factory)
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url()) as checkpointer:
        await checkpointer.setup()
        graph = build_workflow_graph(
            WorkflowNodes(
                search,
                model,
                question_max_chars=4_000,
                max_query_rewrites=1,
                max_generation_attempts=2,
            ),
            checkpointer,
        )
        result = await WorkflowRunner(graph, repository).run(
            WorkflowRequest(
                job_id=job_id,
                thread_id=f"{job_id}:run:0",
                question="문서에 없는 사내 우주선 절차는?",
            )
        )

    async with session_factory() as session:
        review = await session.scalar(
            select(ReviewQueue).where(ReviewQueue.job_id == job_id)
        )
    assert result.status == JobStatus.REVIEW_REQUIRED
    assert search.call_count == 2
    assert model.rewrite_calls == 1
    assert model.generation_calls == 0
    assert review is not None and review.status == ReviewStatus.WAITING
    assert review.reason_code == ReviewReasonCode.INSUFFICIENT_EVIDENCE.value


@pytest.mark.asyncio
async def test_forged_citation_is_bounded_and_persisted_for_review(
    clean_database: AsyncEngine,
) -> None:
    """검색 밖 Chunk 인용이 두 번 차단된 뒤 완료·인용 저장 없이 검토로 전환되는지 검증한다."""
    session_factory, job_id = await create_processing_job(
        clean_database,
        question="테스트 문서의 근거는?",
    )
    model = Stage5Model(forge_citation=True)
    repository = WorkflowRepository(session_factory)
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url()) as checkpointer:
        await checkpointer.setup()
        graph = build_workflow_graph(
            WorkflowNodes(
                StaticSearch(),
                model,
                question_max_chars=4_000,
                max_query_rewrites=1,
                max_generation_attempts=2,
            ),
            checkpointer,
        )
        result = await WorkflowRunner(graph, repository).run(
            WorkflowRequest(
                job_id=job_id,
                thread_id=f"{job_id}:run:0",
                question="테스트 문서의 근거는?",
            )
        )

    async with session_factory() as session:
        review = await session.scalar(
            select(ReviewQueue).where(ReviewQueue.job_id == job_id)
        )
        citation_count = await session.scalar(
            select(func.count()).select_from(AnswerCitation)
        )
    assert result.status == JobStatus.REVIEW_REQUIRED
    assert model.generation_calls == 2
    assert result.validation_errors == [ReviewReasonCode.CITATION_INVALID.value]
    assert review is not None and review.reason_code == ReviewReasonCode.CITATION_INVALID.value
    assert citation_count == 0


@pytest.mark.asyncio
async def test_duplicate_valid_citation_is_persisted_once(
    clean_database: AsyncEngine,
) -> None:
    """모델이 같은 유효 인용을 반복해도 답변과 인용 한 건만 완료 저장되는지 검증한다."""
    session_factory, job_id = await create_processing_job(
        clean_database,
        question="테스트 문서의 근거는?",
    )
    repository = WorkflowRepository(session_factory)
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url()) as checkpointer:
        await checkpointer.setup()
        graph = build_workflow_graph(
            WorkflowNodes(
                StaticSearch(),
                Stage5Model(duplicate_citation=True),
                question_max_chars=4_000,
            ),
            checkpointer,
        )
        result = await WorkflowRunner(graph, repository).run(
            WorkflowRequest(
                job_id=job_id,
                thread_id=f"{job_id}:run:0",
                question="테스트 문서의 근거는?",
            )
        )

    async with session_factory() as session:
        citation_count = await session.scalar(
            select(func.count()).select_from(AnswerCitation)
        )
    assert result.status == JobStatus.COMPLETED
    assert citation_count == 1
