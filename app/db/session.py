from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.common.config import get_settings


def build_engine(database_url: str) -> AsyncEngine:
    """연결 단절을 감지할 수 있는 비동기 PostgreSQL 엔진을 생성한다."""
    return create_async_engine(database_url, pool_pre_ping=True)


engine = build_engine(get_settings().database_url)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """요청 단위 DB 세션을 열고 종료 시 항상 반환한다."""
    async with session_factory() as session:
        yield session

