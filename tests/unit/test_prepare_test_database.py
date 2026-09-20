import pytest

from app.testing.prepare_database import validate_database_name


def test_validate_database_name_accepts_local_test_name() -> None:
    """안전한 소문자 테스트 DB 이름을 변경 없이 허용하는지 검증한다."""
    assert validate_database_name("rag_harness_test") == "rag_harness_test"


@pytest.mark.parametrize("database_name", ("RAG_test", "rag-test", "rag test", "1rag"))
def test_validate_database_name_rejects_unsafe_identifier(database_name: str) -> None:
    """SQL 식별자에 안전하지 않은 테스트 DB 이름을 생성 전에 거부하는지 검증한다."""
    with pytest.raises(ValueError, match="영문 소문자"):
        validate_database_name(database_name)
