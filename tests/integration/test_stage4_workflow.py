from collections.abc import Sequence
import os
from pathlib import Path
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.common.domain import EventSource, JobStatus
from app.db.models import AiJob
from app.retrieval.chunker import MarkdownChunker
from app.retrieval.embedding import EmbeddingTask
from app.retrieval.loader import load_markdown_documents
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.schemas import SearchHit
from app.retrieval.service import DocumentIngestionService, KnowledgeSearchService
from app.services.ingestion import IncomingJob, JobRepository
from app.workflow.graph import WorkflowNodes, build_workflow_graph
from app.workflow.repository import WorkflowRepository
from app.workflow.schemas import (
    AnswerOutput,
    CitationOutput,
    DocumentGradeOutput,
    IntentCategory,
    IntentOutput,
    RiskLevel,
    RewriteQueryOutput,
    WorkflowRequest,
)
from app.workflow.service import WorkflowRunner


pytestmark = pytest.mark.integration
DIMENSIONS = 768


class KeywordEmbeddingClient:
    """가상 매뉴얼 핵심어를 결정적 768차원 벡터로 변환한다."""

    @property
    def index_fingerprint(self) -> str:
        """통합 테스트 문서 버전을 안정적으로 재사용할 식별자를 반환한다."""
        return "stage4-keyword-v1"

    async def embed(
        self, texts: Sequence[str], task: EmbeddingTask
    ) -> list[list[float]]:
        """정산 관련 핵심어 수를 첫 축에 기록해 실제 pgvector 검색을 가능하게 한다."""
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * DIMENSIONS
            normalized = text.lower()
            vector[0] = float(
                sum(normalized.count(keyword) for keyword in ("정산", "원장", "마감"))
            )
            vector[1] = float(
                sum(normalized.count(keyword) for keyword in ("배포", "롤백", "릴리스"))
            )
            if not any(vector):
                vector[-1] = 1.0
            vectors.append(vector)
        return vectors


class CountingSearchService:
    """실제 pgvector 검색 횟수를 기록해 재개 시 중복 검색을 검증한다."""

    def __init__(self, delegate: KnowledgeSearchService) -> None:
        """호출 횟수를 셀 실제 검색 서비스를 주입받는다."""
        self._delegate = delegate
        self.call_count = 0

    async def search(self, question: str) -> list[SearchHit]:
        """호출 횟수를 늘린 뒤 실제 검색 결과를 그대로 반환한다."""
        self.call_count += 1
        return await self._delegate.search(question)


class CitingModelClient:
    """검색 결과 첫 Chunk를 인용하고 선택적으로 생성 실패를 재현한다."""

    def __init__(self, *, fail_generation: bool = False) -> None:
        """Worker 중단 재현 여부와 호출 횟수 기록을 초기화한다."""
        self._fail_generation = fail_generation
        self.classification_calls = 0
        self.generation_calls = 0

    async def classify_intent(self, question: str) -> IntentOutput:
        """정산 질문을 낮은 위험의 절차 의도로 결정적으로 분류한다."""
        self.classification_calls += 1
        return IntentOutput(
            intent=IntentCategory.PROCEDURE,
            risk_level=RiskLevel.LOW,
            reason="가상 정산 매뉴얼 절차 질문",
        )

    async def generate_answer(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> AnswerOutput:
        """첫 실행 실패 또는 실제 검색 Chunk를 인용한 답변을 반환한다."""
        self.generation_calls += 1
        if self._fail_generation:
            raise RuntimeError("SIMULATED_WORKER_INTERRUPTION")
        first = chunks[0]
        return AnswerOutput(
            answer="정산 마감 전 원장 불일치를 확인하고 승인 기록을 남깁니다.",
            citations=[
                CitationOutput(
                    document_id=first.document_id,
                    chunk_id=first.chunk_id,
                )
            ],
        )

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """실제 pgvector 첫 결과를 관련 근거로 판정한다."""
        return DocumentGradeOutput(
            relevant_chunk_ids=[chunks[0].chunk_id],
            reason="정산 질문과 직접 관련된 매뉴얼",
        )

    async def rewrite_query(self, question: str, reason: str) -> RewriteQueryOutput:
        """이 통합 시나리오에서는 사용되지 않을 결정적 재검색어를 반환한다."""
        return RewriteQueryOutput(query=question)


def checkpoint_url() -> str:
    """테스트 DB URL을 AsyncPostgresSaver용 Psycopg URL로 변환한다."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("TEST_DATABASE_URL이 필요합니다.")
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def prepare_workflow_dependencies(
    engine: AsyncEngine,
) -> tuple[
    async_sessionmaker[AsyncSession], CountingSearchService, WorkflowRepository, UUID
]:
    """실제 문서·pgvector 검색과 PROCESSING 작업을 통합 테스트 DB에 준비한다."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    knowledge_repository = KnowledgeRepository(session_factory)
    embedding_client = KeywordEmbeddingClient()
    ingestion = DocumentIngestionService(
        repository=knowledge_repository,
        chunker=MarkdownChunker(max_chars=1_200, overlap_chars=120),
        embedding_client=embedding_client,
        embedding_dimensions=DIMENSIONS,
    )
    for document in load_markdown_documents(Path("knowledge/manuals")):
        await ingestion.ingest(document)
    search = CountingSearchService(
        KnowledgeSearchService(
            repository=knowledge_repository,
            embedding_client=embedding_client,
            embedding_dimensions=DIMENSIONS,
            top_k=5,
            min_score=-1.0,
            max_chunks_per_document=2,
        )
    )
    async with session_factory() as session:
        accepted = await JobRepository(session).create_with_outbox(
            IncomingJob(
                source=EventSource.LOCAL,
                external_event_id=f"stage4-{UUID(int=0)}-{id(engine)}",
                question="정산 배치 마감 전에 무엇을 확인하나요?",
            )
        )
    async with session_factory() as session, session.begin():
        await session.execute(
            update(AiJob)
            .where(AiJob.job_id == accepted.job_id)
            .values(status=JobStatus.PROCESSING)
        )
    return session_factory, search, WorkflowRepository(session_factory), accepted.job_id


@pytest.mark.asyncio
async def test_workflow_completes_with_postgres_checkpoints_and_real_chunk(
    clean_database: AsyncEngine,
) -> None:
    """정상 질문이 모든 노드 Checkpoint와 실제 검색 Chunk 인용을 남기고 완료되는지 검증한다."""
    session_factory, search, repository, job_id = await prepare_workflow_dependencies(
        clean_database
    )
    model = CitingModelClient()
    thread_id = f"workflow-{job_id}"
    config = {"configurable": {"thread_id": thread_id}}
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url()) as checkpointer:
        await checkpointer.setup()
        graph = build_workflow_graph(
            WorkflowNodes(search, model, question_max_chars=4_000), checkpointer
        )
        result = await WorkflowRunner(graph, repository).run(
            WorkflowRequest(
                job_id=job_id,
                thread_id=thread_id,
                question="정산 배치 마감 전에 무엇을 확인하나요?",
            )
        )
        history = [snapshot async for snapshot in graph.aget_state_history(config)]

    checkpointed_nodes = {
        snapshot.values.get("last_node") for snapshot in history if snapshot.values
    }
    async with session_factory() as session:
        job = await session.get(AiJob, job_id)
    assert result.status == JobStatus.COMPLETED
    assert result.answer is not None
    assert result.answer.citations[0].chunk_id in result.retrieved_chunk_ids
    assert checkpointed_nodes >= {
        "validate_input",
        "classify_intent",
        "retrieve_documents",
        "generate_answer",
    }
    assert search.call_count == 1
    assert job is not None and job.status == JobStatus.COMPLETED
    assert job.result_answer == result.answer.answer


@pytest.mark.asyncio
async def test_new_worker_resumes_failed_generation_from_postgres_checkpoint(
    clean_database: AsyncEngine,
) -> None:
    """생성 중 종료된 Worker를 새 그래프 인스턴스가 검색 반복 없이 같은 작업으로 재개하는지 검증한다."""
    _, search, repository, job_id = await prepare_workflow_dependencies(clean_database)
    request = WorkflowRequest(
        job_id=job_id,
        thread_id=f"workflow-resume-{job_id}",
        question="정산 배치 마감 전에 무엇을 확인하나요?",
    )
    failing_model = CitingModelClient(fail_generation=True)
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url()) as first_checkpointer:
        await first_checkpointer.setup()
        first_graph = build_workflow_graph(
            WorkflowNodes(search, failing_model, question_max_chars=4_000),
            first_checkpointer,
        )
        with pytest.raises(RuntimeError, match="SIMULATED_WORKER_INTERRUPTION"):
            await WorkflowRunner(first_graph, repository).run(request)

    resumed_model = CitingModelClient()
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url()) as second_checkpointer:
        second_graph = build_workflow_graph(
            WorkflowNodes(search, resumed_model, question_max_chars=4_000),
            second_checkpointer,
        )
        result = await WorkflowRunner(second_graph, repository).run(request)

    assert result.status == JobStatus.COMPLETED
    assert search.call_count == 1
    assert failing_model.classification_calls == 1
    assert failing_model.generation_calls == 1
    assert resumed_model.classification_calls == 0
    assert resumed_model.generation_calls == 1
