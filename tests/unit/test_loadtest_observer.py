from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx

from app.loadtest.observer import QueueObserverSettings, record_queue_timeline


def no_sleep(_: float) -> None:
    """단위 테스트에서 실제 대기 없이 표본 반복을 진행한다."""


def ticking_clock() -> Iterator[datetime]:
    """표본마다 1초씩 증가하는 결정적 UTC 시각을 제공한다."""
    current = datetime(2026, 10, 9, tzinfo=timezone.utc)
    while True:
        yield current
        current += timedelta(seconds=1)


def test_queue_timeline_stops_after_observed_backlog_drains(tmp_path: Path) -> None:
    """Queue 적체를 한 번 본 뒤 0으로 내려가면 관측을 즉시 완료하는지 검증한다."""
    payloads = iter(
        [
            {"name": "rag_harness.jobs", "messages_ready": 0, "messages_unacknowledged": 0, "consumers": 0},
            {"name": "rag_harness.jobs", "messages_ready": 4, "messages_unacknowledged": 1, "consumers": 1},
            {"name": "rag_harness.jobs", "messages_ready": 0, "messages_unacknowledged": 0, "consumers": 1},
        ]
    )

    def handle_request(_: httpx.Request) -> httpx.Response:
        """준비된 Queue 표본을 RabbitMQ 관리 API 응답처럼 반환한다."""
        return httpx.Response(200, json=next(payloads))

    times = ticking_clock()

    def next_time() -> datetime:
        """관측 순서가 드러나는 다음 고정 시각을 반환한다."""
        return next(times)

    output_path = tmp_path / "queue-timeline.jsonl"
    settings = QueueObserverSettings(
        management_url="http://rabbitmq.test",
        name="rag_harness.jobs",
        max_samples=10,
        interval_seconds=0.01,
        output_path=output_path,
    )
    with httpx.Client(transport=httpx.MockTransport(handle_request)) as client:
        result = record_queue_timeline(
            settings,
            client=client,
            sleeper=no_sleep,
            clock=next_time,
        )

    lines = output_path.read_text(encoding="utf-8").splitlines()
    snapshots = [json.loads(line) for line in lines]
    assert result.sample_count == 3
    assert result.observed_backlog is True
    assert result.drained is True
    assert result.max_messages_ready == 4
    assert result.max_messages_total == 5
    assert [snapshot["messages_total"] for snapshot in snapshots] == [0, 5, 0]


def test_queue_timeline_uses_sample_limit_when_no_backlog_appears(
    tmp_path: Path,
) -> None:
    """적체가 나타나지 않으면 성공으로 오인하지 않고 표본 한도까지 기록하는지 검증한다."""

    def handle_request(_: httpx.Request) -> httpx.Response:
        """항상 비어 있는 Queue 관리 API 응답을 반환한다."""
        return httpx.Response(
            200,
            json={
                "name": "rag_harness.jobs",
                "messages_ready": 0,
                "messages_unacknowledged": 0,
                "consumers": 1,
            },
        )

    settings = QueueObserverSettings(
        management_url="http://rabbitmq.test",
        name="rag_harness.jobs",
        max_samples=2,
        interval_seconds=0.01,
        output_path=tmp_path / "empty-queue.jsonl",
    )
    with httpx.Client(transport=httpx.MockTransport(handle_request)) as client:
        result = record_queue_timeline(settings, client=client, sleeper=no_sleep)

    assert result.sample_count == 2
    assert result.observed_backlog is False
    assert result.drained is False
    assert result.max_messages_total == 0
