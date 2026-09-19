import os

import aio_pika
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgresql_connection(clean_database: AsyncEngine) -> None:
    """실제 PostgreSQL 컨테이너가 쿼리를 처리하는지 검증한다."""
    async with clean_database.connect() as connection:
        result = await connection.scalar(text("SELECT 1"))

    assert result == 1


@pytest.mark.asyncio
async def test_rabbitmq_connection() -> None:
    """실제 RabbitMQ 컨테이너에 AMQP 연결을 열고 닫는지 검증한다."""
    rabbitmq_url = os.environ.get("TEST_RABBITMQ_URL")
    if rabbitmq_url is None:
        raise RuntimeError("TEST_RABBITMQ_URL이 필요합니다.")
    connection = await aio_pika.connect(rabbitmq_url)
    assert not connection.is_closed
    await connection.close()
    assert connection.is_closed
