from collections.abc import AsyncIterator
import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest_asyncio.fixture
async def clean_database() -> AsyncIterator[AsyncEngine]:
    """각 통합 테스트가 독립 실행되도록 작업 테이블을 비운다."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("TEST_DATABASE_URL이 필요합니다.")
    test_engine = create_async_engine(database_url, pool_pre_ping=True)
    async with test_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE job_outbox, ai_job CASCADE"))
    yield test_engine
    await test_engine.dispose()

