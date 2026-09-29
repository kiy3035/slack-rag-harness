import asyncio
from datetime import date
import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.config import DocumentGraderProvider, Settings, get_settings
from app.common.logging import configure_structured_logging
from app.db.session import build_engine
from app.integrations.slack.client import SlackWebClient
from app.integrations.slack.publisher import SlackReplyPublisher
from app.integrations.slack.repository import SlackReplyRepository
from app.messaging.rabbitmq import RabbitBroker
from app.observability.metrics import start_metrics_server
from app.recovery.errors import RetryPolicy
from app.recovery.repository import JobRecoveryRepository
from app.recovery.scheduler import RecoveryScheduler
from app.retrieval.embedding import OllamaEmbeddingClient
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.service import KnowledgeSearchService
from app.worker.consumer import JobExecutionGate, RabbitJobConsumer
from app.workflow.graph import WorkflowNodes, build_workflow_graph
from app.workflow.jev import FallbackDocumentGradeClient, JevDocumentGradeClient
from app.workflow.model import DocumentGradeClient, OllamaWorkflowModelClient
from app.workflow.repository import WorkflowRepository
from app.workflow.service import WorkflowJobHandler, WorkflowRunner


def build_document_grade_client(
    settings: Settings,
    model_client: OllamaWorkflowModelClient,
    current_date: date | None = None,
) -> tuple[DocumentGradeClient, JevDocumentGradeClient | None]:
    """설정과 무료 종료일을 확인해 안전한 관련성 판정기와 Jev 자원을 만든다."""
    if settings.workflow_document_grader != DocumentGraderProvider.JEV:
        return model_client, None
    effective_date = current_date or date.today()
    if effective_date > settings.jev_free_use_not_after:
        logging.getLogger(__name__).warning(
            "jev_disabled_free_period_ended not_after=%s",
            settings.jev_free_use_not_after.isoformat(),
        )
        return model_client, None

    jev_client = JevDocumentGradeClient(
        base_url=settings.jev_base_url,
        api_key=settings.ai_gateway_api_key,
        model=settings.jev_model,
        timeout_seconds=settings.jev_timeout_seconds,
        relevance_threshold=settings.jev_relevance_threshold,
        conflict_threshold=settings.jev_conflict_threshold,
    )
    document_grade_client: DocumentGradeClient = jev_client
    if settings.jev_fallback_to_ollama:
        document_grade_client = FallbackDocumentGradeClient(jev_client, model_client)
    return document_grade_client, jev_client


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
    document_grade_client, jev_client = build_document_grade_client(
        settings, model_client
    )
    broker = RabbitBroker(settings)
    slack_client: SlackWebClient | None = None
    slack_publisher: SlackReplyPublisher | None = None
    try:
        await broker.connect()
        if settings.slack_reply_enabled:
            slack_client = SlackWebClient(
                bot_token=settings.slack_bot_token.get_secret_value(),
                base_url=settings.slack_api_base_url,
                timeout_seconds=settings.slack_api_timeout_seconds,
            )
            slack_publisher = SlackReplyPublisher(
                repository=SlackReplyRepository(session_factory),
                client=slack_client,
                retry_policy=RetryPolicy(
                    max_attempts=settings.slack_reply_max_attempts,
                    base_seconds=settings.slack_reply_retry_base_seconds,
                    max_seconds=settings.slack_reply_retry_max_seconds,
                ),
                lease_seconds=settings.slack_reply_lease_seconds,
            )
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
            max_query_rewrites=settings.workflow_max_query_rewrites,
            max_generation_attempts=settings.workflow_max_generation_attempts,
            document_grade_client=document_grade_client,
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
            retry_policy = RetryPolicy(
                max_attempts=settings.worker_max_attempts,
                base_seconds=settings.worker_retry_base_seconds,
                max_seconds=settings.worker_retry_max_seconds,
            )
            consumer = RabbitJobConsumer(
                broker=broker,
                gate=JobExecutionGate(session_factory, retry_policy),
                handler=handler,
            )
            recovery_scheduler = RecoveryScheduler(
                repository=JobRecoveryRepository(session_factory),
                broker=broker,
                settings=settings,
            )
            while True:
                await recovery_scheduler.process_once()
                if slack_publisher is not None:
                    await slack_publisher.process_once()
                await consumer.consume_one(
                    timeout=settings.worker_recovery_poll_seconds
                )
    finally:
        await broker.close()
        await embedding_client.aclose()
        await model_client.aclose()
        if slack_client is not None:
            await slack_client.aclose()
        if jev_client is not None:
            await jev_client.aclose()
        await engine.dispose()


def main() -> None:
    """비동기 Worker 수명 주기를 단일 이벤트 루프로 실행한다."""
    settings = get_settings()
    configure_structured_logging("worker", settings.log_directory)
    if settings.metrics_enabled:
        start_metrics_server(settings.metrics_port)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
