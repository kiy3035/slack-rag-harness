from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.retrieval.chunker import MarkdownChunker
from app.retrieval.loader import load_markdown_documents
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.service import DocumentIngestionService, KnowledgeSearchService


DIMENSIONS = 768
MANUAL_DIRECTORY = Path("knowledge/manuals")
KEYWORD_GROUPS = (
    ("정산", "결제", "원장", "마감"),
    ("배포", "롤백", "릴리스", "이미지"),
    ("권한", "계정", "접근", "퇴사자"),
    ("장애", "p1", "에스컬레이션", "지휘자"),
    ("고객", "공지", "알림", "영향"),
    ("백업", "복구", "스냅샷", "체크섬"),
)


class KeywordEmbeddingClient:
    """의미 범주를 고정 축에 투영해 pgvector 검색을 재현 가능하게 검증한다."""

    def __init__(self) -> None:
        """동일 문서 재적재 시 외부 호출 생략 여부를 셀 수 있게 초기화한다."""
        self.call_count = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """가상 매뉴얼의 핵심어 빈도를 768차원 로컬 벡터로 변환한다."""
        self.call_count += 1
        return [self._vectorize(value) for value in texts]

    def _vectorize(self, value: str) -> list[float]:
        """각 문서 범주의 핵심어 출현 수를 서로 다른 축에 기록한다."""
        normalized = value.lower()
        vector = [0.0] * DIMENSIONS
        for index, keywords in enumerate(KEYWORD_GROUPS):
            vector[index] = float(sum(normalized.count(keyword) for keyword in keywords))
        if not any(vector):
            vector[-1] = 1.0
        return vector


def build_services(
    engine: AsyncEngine,
    embedding_client: KeywordEmbeddingClient,
) -> tuple[DocumentIngestionService, KnowledgeSearchService]:
    """실제 PostgreSQL과 결정적 Fake 임베딩을 사용하는 서비스 묶음을 만든다."""
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    repository = KnowledgeRepository(factory)
    ingestion = DocumentIngestionService(
        repository=repository,
        chunker=MarkdownChunker(max_chars=1_200, overlap_chars=120),
        embedding_client=embedding_client,
        embedding_dimensions=DIMENSIONS,
    )
    search = KnowledgeSearchService(
        repository=repository,
        embedding_client=embedding_client,
        embedding_dimensions=DIMENSIONS,
        top_k=3,
        min_score=-1.0,
        max_chunks_per_document=2,
    )
    return ingestion, search


@pytest.mark.integration
async def test_reingestion_is_idempotent_and_replaces_changed_version(
    clean_database: AsyncEngine,
) -> None:
    """동일 원문은 Chunk를 늘리지 않고 변경 원문은 한 새 버전으로 교체되는지 검증한다."""
    embedding_client = KeywordEmbeddingClient()
    ingestion, _ = build_services(clean_database, embedding_client)
    document = load_markdown_documents(MANUAL_DIRECTORY)[0]

    first = await ingestion.ingest(document)
    second = await ingestion.ingest(document)
    changed = document.model_copy(update={"content": document.content + "\n\n추가 검증 절차."})
    third = await ingestion.ingest(changed)

    assert first.changed is True
    assert second.changed is False
    assert second.document_id == first.document_id
    assert second.chunk_count == first.chunk_count
    assert third.changed is True
    assert third.version == 2
    assert embedding_client.call_count == 2

    async with clean_database.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) AS count, MIN(document_version) AS min_version, "
                    "MAX(document_version) AS max_version FROM knowledge_chunk "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": first.document_id},
            )
        ).mappings().one()
    assert row["count"] == third.chunk_count
    assert row["min_version"] == row["max_version"] == 2


@pytest.mark.integration
async def test_representative_questions_include_expected_manual_in_top_k(
    clean_database: AsyncEngine,
) -> None:
    """대표 질문 10개에서 기대한 가상 매뉴얼이 pgvector Top-K에 포함되는지 검증한다."""
    embedding_client = KeywordEmbeddingClient()
    ingestion, search = build_services(clean_database, embedding_client)
    for document in load_markdown_documents(MANUAL_DIRECTORY):
        await ingestion.ingest(document)

    cases = (
        ("정산 배치 마감 승인 전에 무엇을 확인하나요?", "01_settlement_batch.md"),
        ("결제 원장 불일치를 어떻게 재처리하나요?", "01_settlement_batch.md"),
        ("배포 뒤 오류율이 높을 때 롤백 순서는?", "02_deployment_rollback.md"),
        ("릴리스 이전 이미지로 되돌린 뒤 무엇을 검증하나요?", "02_deployment_rollback.md"),
        ("접근 권한 신청에 필요한 항목은?", "03_access_request.md"),
        ("퇴사자 계정과 활성 세션은 언제 회수하나요?", "03_access_request.md"),
        ("P1 장애가 발생하면 누구에게 에스컬레이션하나요?", "04_incident_escalation.md"),
        ("고객 공지에 포함하면 안 되는 정보는?", "05_customer_notification.md"),
        ("백업 스냅샷 복구 훈련에서 무엇을 기록하나요?", "06_backup_restore.md"),
        ("데이터 복구 후 쓰기를 재개하는 조건은?", "06_backup_restore.md"),
    )

    for question, expected_filename in cases:
        hits = await search.search(question)
        assert any(hit.source_path.endswith(expected_filename) for hit in hits), question
