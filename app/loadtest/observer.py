import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import time
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RabbitQueuePayload(BaseModel):
    """RabbitMQ 관리 API에서 관측에 필요한 Queue 필드만 검증한다."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    messages_ready: int = Field(ge=0)
    messages_unacknowledged: int = Field(ge=0)
    consumers: int = Field(ge=0)


class QueueSnapshot(BaseModel):
    """한 시점의 Queue 대기·처리 중 메시지와 Consumer 수를 나타낸다."""

    observed_at: datetime
    queue_name: str
    messages_ready: int = Field(ge=0)
    messages_unacknowledged: int = Field(ge=0)
    messages_total: int = Field(ge=0)
    consumers: int = Field(ge=0)


class QueueTimelineResult(BaseModel):
    """관측 종료 이유와 최대 적체량을 요약한다."""

    sample_count: int = Field(ge=1)
    observed_backlog: bool
    drained: bool
    max_messages_ready: int = Field(ge=0)
    max_messages_total: int = Field(ge=0)
    output_path: Path


class QueueObserverSettings(BaseSettings):
    """환경변수로 주입되는 로컬 Queue 관측 설정을 검증한다."""

    model_config = SettingsConfigDict(
        env_prefix="LOADTEST_QUEUE_",
        extra="ignore",
    )

    management_url: str = "http://rabbitmq:15672"
    name: str = "rag_harness.jobs"
    username: str = "rag_harness"
    password: SecretStr = SecretStr("local_dev_password")
    interval_seconds: float = Field(default=1.0, gt=0)
    max_samples: int = Field(default=900, ge=1, le=86_400)
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    stop_when_drained: bool = True
    output_path: Path = Path("load-tests/results/queue-timeline.jsonl")


def utc_now() -> datetime:
    """관측 시각을 비교 가능한 UTC 값으로 반환한다."""
    return datetime.now(timezone.utc)


def fetch_queue_snapshot(
    client: httpx.Client,
    settings: QueueObserverSettings,
    clock: Callable[[], datetime] = utc_now,
) -> QueueSnapshot:
    """RabbitMQ 관리 API 응답을 검증해 민감 정보 없는 Queue 표본으로 바꾼다."""
    base_url = settings.management_url.rstrip("/")
    encoded_vhost = quote("/", safe="")
    encoded_queue = quote(settings.name, safe="")
    response = client.get(
        f"{base_url}/api/queues/{encoded_vhost}/{encoded_queue}",
        auth=(settings.username, settings.password.get_secret_value()),
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    payload = RabbitQueuePayload.model_validate(response.json())
    return QueueSnapshot(
        observed_at=clock(),
        queue_name=payload.name,
        messages_ready=payload.messages_ready,
        messages_unacknowledged=payload.messages_unacknowledged,
        messages_total=payload.messages_ready + payload.messages_unacknowledged,
        consumers=payload.consumers,
    )


def record_queue_timeline(
    settings: QueueObserverSettings,
    *,
    client: httpx.Client | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = utc_now,
) -> QueueTimelineResult:
    """적체가 나타난 뒤 해소되거나 표본 한도에 도달할 때까지 JSONL로 기록한다."""
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    active_client = client or httpx.Client()
    observed_backlog = False
    drained = False
    max_messages_ready = 0
    max_messages_total = 0
    sample_count = 0

    try:
        with settings.output_path.open("w", encoding="utf-8", newline="\n") as output:
            for sample_index in range(settings.max_samples):
                snapshot = fetch_queue_snapshot(active_client, settings, clock)
                serialized = snapshot.model_dump_json()
                output.write(serialized + "\n")
                output.flush()
                print(serialized, flush=True)

                sample_count += 1
                max_messages_ready = max(max_messages_ready, snapshot.messages_ready)
                max_messages_total = max(max_messages_total, snapshot.messages_total)
                if snapshot.messages_total > 0:
                    observed_backlog = True
                elif observed_backlog and settings.stop_when_drained:
                    drained = True
                    break

                if sample_index + 1 < settings.max_samples:
                    sleeper(settings.interval_seconds)
    finally:
        if owns_client:
            active_client.close()

    return QueueTimelineResult(
        sample_count=sample_count,
        observed_backlog=observed_backlog,
        drained=drained,
        max_messages_ready=max_messages_ready,
        max_messages_total=max_messages_total,
        output_path=settings.output_path,
    )


def main() -> None:
    """검증된 환경 설정으로 Queue 관측을 실행하고 종료 요약을 출력한다."""
    result = record_queue_timeline(QueueObserverSettings())
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
