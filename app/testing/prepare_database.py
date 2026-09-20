import asyncio
import os
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


DATABASE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def validate_database_name(database_name: str) -> str:
    """동적 CREATE DATABASE에 사용할 안전한 로컬 DB 이름만 허용한다."""
    if DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("테스트 DB 이름은 영문 소문자, 숫자, 밑줄만 사용할 수 있습니다.")
    return database_name


async def prepare_test_database(admin_url: str, database_name: str) -> None:
    """개발 데이터를 건드리지 않도록 독립된 테스트 DB가 없을 때만 생성한다."""
    safe_name = validate_database_name(database_name)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": safe_name},
            )
            if exists is None:
                await connection.execute(text(f'CREATE DATABASE "{safe_name}"'))
    finally:
        await engine.dispose()


def main() -> None:
    """환경변수에서 관리자 연결과 테스트 DB 이름을 읽어 준비 작업을 실행한다."""
    admin_url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    database_name = os.environ.get("TEST_DATABASE_NAME")
    if admin_url is None or database_name is None:
        raise RuntimeError("TEST_ADMIN_DATABASE_URL과 TEST_DATABASE_NAME이 필요합니다.")
    asyncio.run(prepare_test_database(admin_url, database_name))


if __name__ == "__main__":
    main()
