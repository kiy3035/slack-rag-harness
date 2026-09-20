import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.config import get_settings
from app.db.session import build_engine
from app.messaging.rabbitmq import RabbitBroker
from app.retrieval.embedding import OllamaEmbeddingClient
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.service import KnowledgeSearchService
from app.worker.consumer import JobExecutionGate, RabbitJobConsumer
from app.workflow.graph import WorkflowNodes, build_workflow_graph
from app.workflow.model import OllamaWorkflowModelClient
from app.workflow.repository import WorkflowRepository
from app.workflow.service import WorkflowJobHandler, WorkflowRunner


async def run_worker() -> None:
    """로컬 인프라와 Ollama 경계를 연결해 RabbitMQ 작업을 계속 처리한다."""
    settings = get_settings()
    engine = build_engine(settings.database_url)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    embedding_client = OllamaEmbeddingClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    model_client = OllamaWorkflowModelClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_generation_model,
        timeout_seconds=settings.ollama_generation_timeout_seconds,
    )
    broker = RabbitBroker(settings)
    try:
        await broker.connect()
        repository = WorkflowRepository(session_factory)
        search_service = KnowledgeSearchService(
            repository=KnowledgeRepository(session_factory),
            embedding_client=embedding_client,
            embedding_dimensions=settings.embedding_dimensions,
            top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score,
            max_chunks_per_document=settings.retrieval_max_chunks_per_document,
        )
        nodes = WorkflowNodes(
            search_service=search_service,
            model_client=model_client,
            question_max_chars=settings.workflow_question_max_chars,
        )
        async with AsyncPostgresSaver.from_conn_string(
            settings.checkpoint_database_url
        ) as checkpointer:
            await checkpointer.setup()
            graph = build_workflow_graph(nodes, checkpointer)
            handler = WorkflowJobHandler(
                repository=repository,
                runner=WorkflowRunner(graph, repository),
            )
            consumer = RabbitJobConsumer(
                broker=broker,
                gate=JobExecutionGate(session_factory),
                handler=handler,
            )
            while True:
                await consumer.consume_one(timeout=5.0)
    finally:
        await broker.close()
        await embedding_client.aclose()
        await model_client.aclose()
        await engine.dispose()


def main() -> None:
    """비동기 Worker 수명 주기를 단일 이벤트 루프로 실행한다."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
