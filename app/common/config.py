from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    embedding_dimensions: int = Field(default=768, ge=768, le=768)
    document_chunk_max_chars: int = Field(default=1_200, ge=200, le=10_000)
    document_chunk_overlap_chars: int = Field(default=120, ge=0, le=2_000)
    retrieval_top_k: int = Field(default=5, ge=1, le=100)
    retrieval_min_score: float = Field(default=-1.0, ge=-1.0, le=1.0)
    retrieval_max_chunks_per_document: int = Field(default=2, ge=1, le=20)
    enable_local_events: bool = False
    slack_signing_secret: SecretStr = SecretStr("")
    slack_timestamp_tolerance_seconds: int = Field(default=300, ge=1, le=900)


@lru_cache
def get_settings() -> Settings:
    """프로세스에서 재사용할 검증된 설정 객체를 반환한다."""
    return Settings()
