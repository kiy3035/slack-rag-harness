from collections.abc import AsyncIterator
import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest_asyncio.fixture
async def clean_database() -> AsyncIterator[AsyncEngine]:
    """각 통합 테스트 전후에 도메인 테이블을 비워 Fake 데이터를 격리한다."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("TEST_DATABASE_URL이 필요합니다.")
    test_engine = create_async_engine(database_url, pool_pre_ping=True)
    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE knowledge_chunk, knowledge_document, "
                "job_outbox, ai_job CASCADE"
            )
        )
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE knowledge_chunk, knowledge_document, "
                    "job_outbox, ai_job CASCADE"
                )
            )
        await test_engine.dispose()
