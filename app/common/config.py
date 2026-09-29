from datetime import date
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


JEV_CONFIRMED_FREE_USE_NOT_AFTER = date(2026, 9, 25)


class DocumentGraderProvider(StrEnum):
    """검색 문서 관련성 판정에 사용할 구현을 제한한다."""

    OLLAMA = "ollama"
    JEV = "jev"


class Settings(BaseSettings):
    """환경변수에서 로컬 서비스 연결과 보안 설정을 읽는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://rag_harness:local_dev_password@localhost:5432/rag_harness"
    rabbitmq_url: str = "amqp://rag_harness:local_dev_password@localhost:5672/"
    rabbitmq_job_exchange: str = "rag_harness.jobs"
    rabbitmq_retry_exchange: str = "rag_harness.jobs.retry"
    rabbitmq_dead_letter_exchange: str = "rag_harness.jobs.dlx"
    rabbitmq_job_queue: str = "rag_harness.jobs"
    rabbitmq_retry_queue: str = "rag_harness.jobs.retry"
    rabbitmq_dead_letter_queue: str = "rag_harness.jobs.dlq"
    rabbitmq_job_routing_key: str = "job.execute"
    rabbitmq_retry_routing_key: str = "job.retry"
    rabbitmq_dead_letter_routing_key: str = "job.dead"
    rabbitmq_retry_delay_ms: int = Field(default=5_000, ge=100, le=3_600_000)
    outbox_batch_size: int = Field(default=20, ge=1, le=500)
    outbox_poll_interval_seconds: float = Field(default=0.5, ge=0.05, le=60.0)
    outbox_lease_seconds: int = Field(default=30, ge=1, le=3_600)
    outbox_max_attempts: int = Field(default=5, ge=1, le=100)
    outbox_retry_base_seconds: int = Field(default=1, ge=1, le=3_600)
    outbox_retry_max_seconds: int = Field(default=60, ge=1, le=86_400)
    worker_processing_lease_seconds: int = Field(default=900, ge=10, le=86_400)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_retry_base_seconds: int = Field(default=5, ge=1, le=3_600)
    worker_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    worker_recovery_batch_size: int = Field(default=20, ge=1, le=500)
    worker_recovery_poll_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    dlq_publish_retry_base_seconds: int = Field(default=5, ge=1, le=3_600)
    dlq_publish_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    enable_admin_recovery: bool = False
    enable_admin_observability: bool = False
    metrics_enabled: bool = True
    metrics_port: int = Field(default=9_100, ge=1_024, le=65_535)
    log_directory: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_generation_model: str = "qwen3:1.7b"
    ollama_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    ollama_generation_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    workflow_question_max_chars: int = Field(default=4_000, ge=100, le=20_000)
    workflow_max_query_rewrites: int = Field(default=1, ge=0, le=3)
    workflow_max_generation_attempts: int = Field(default=2, ge=1, le=5)
    workflow_document_grader: DocumentGraderProvider = DocumentGraderProvider.OLLAMA
    ai_gateway_api_key: SecretStr = SecretStr("")
    jev_base_url: str = "https://ai-gateway.vercel.sh"
    jev_model: str = "typesafe-ai/jev"
    jev_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    jev_relevance_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    jev_conflict_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    jev_fallback_to_ollama: bool = True
    jev_free_use_not_after: date = Field(
        default=JEV_CONFIRMED_FREE_USE_NOT_AFTER,
        le=JEV_CONFIRMED_FREE_USE_NOT_AFTER,
    )
    embedding_dimensions: int = Field(default=768, ge=768, le=768)
    document_chunk_max_chars: int = Field(default=1_200, ge=200, le=10_000)
    document_chunk_overlap_chars: int = Field(default=120, ge=0, le=2_000)
    retrieval_top_k: int = Field(default=5, ge=1, le=100)
    retrieval_min_score: float = Field(default=-1.0, ge=-1.0, le=1.0)
    retrieval_max_chunks_per_document: int = Field(default=2, ge=1, le=20)
    enable_local_events: bool = False
    slack_reply_enabled: bool = False
    slack_bot_token: SecretStr = SecretStr("")
    slack_api_base_url: str = "https://slack.com/api"
    slack_api_timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    slack_reply_lease_seconds: int = Field(default=30, ge=1, le=3_600)
    slack_reply_max_attempts: int = Field(default=5, ge=1, le=20)
    slack_reply_retry_base_seconds: int = Field(default=2, ge=1, le=3_600)
    slack_reply_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    slack_signing_secret: SecretStr = SecretStr("")
    slack_timestamp_tolerance_seconds: int = Field(default=300, ge=1, le=900)

    @property
    def checkpoint_database_url(self) -> str:
        """SQLAlchemy URL을 LangGraph Psycopg가 이해하는 PostgreSQL URL로 변환한다."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    """프로세스에서 재사용할 검증된 설정 객체를 반환한다."""
    return Settings()
