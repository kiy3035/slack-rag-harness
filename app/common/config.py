from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수에서 로컬 서비스 연결과 보안 설정을 읽는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://rag_harness:local_dev_password@localhost:5432/rag_harness"
    rabbitmq_url: str = "amqp://rag_harness:local_dev_password@localhost:5672/"
    enable_local_events: bool = False
    slack_signing_secret: SecretStr = SecretStr("")
    slack_timestamp_tolerance_seconds: int = Field(default=300, ge=1, le=900)


@lru_cache
def get_settings() -> Settings:
    """프로세스에서 재사용할 검증된 설정 객체를 반환한다."""
    return Settings()

