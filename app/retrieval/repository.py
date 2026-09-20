from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.retrieval.schemas import (
    DocumentState,
    EmbeddedChunk,
    IngestionResult,
    MarkdownDocument,
    SearchHit,
)


class KnowledgeRepository:
    """지식 문서 버전 교체와 pgvector 유사도 검색을 원자적으로 처리한다."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """호출마다 짧은 트랜잭션을 만들 수 있는 Session Factory를 보관한다."""
        self._session_factory = session_factory

    async def get_document_state(self, source_path: str) -> DocumentState | None:
        """외부 임베딩 호출 전에 동일 문서 적재를 생략할 수 있도록 상태를 조회한다."""
        statement = text(
            """
            SELECT d.document_id, d.content_hash, d.current_version,
                   COUNT(c.chunk_id)::integer AS chunk_count
            FROM knowledge_document AS d
            LEFT JOIN knowledge_chunk AS c ON c.document_id = d.document_id
            WHERE d.source_path = :source_path
            GROUP BY d.document_id
            """
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement, {"source_path": source_path})).mappings().one_or_none()
        return DocumentState.model_validate(dict(row)) if row is not None else None

    async def replace_document(
        self,
        document: MarkdownDocument,
        content_hash: str,
        chunks: Sequence[EmbeddedChunk],
    ) -> IngestionResult:
        """같은 경로의 갱신을 직렬화하고 현재 버전 Chunk를 한 번에 교체한다."""
        async with self._session_factory.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:source_path, 0))"),
                {"source_path": document.source_path},
            )
            current = (
                await session.execute(
                    text(
                        """
                        SELECT d.document_id, d.content_hash, d.current_version,
                               (
                                   SELECT COUNT(*)::integer
                                   FROM knowledge_chunk AS c
                                   WHERE c.document_id = d.document_id
                               ) AS chunk_count
                        FROM knowledge_document AS d
                        WHERE d.source_path = :source_path
                        FOR UPDATE
                        """
                    ),
                    {"source_path": document.source_path},
                )
            ).mappings().one_or_none()

            if current is not None and current["content_hash"] == content_hash:
                return IngestionResult(
                    document_id=current["document_id"],
                    version=current["current_version"],
                    chunk_count=current["chunk_count"],
                    changed=False,
                )

            if current is None:
                document_id = uuid4()
                version = 1
                await session.execute(
                    text(
                        """
                        INSERT INTO knowledge_document (
                            document_id, source_path, title, content_hash, current_version
                        ) VALUES (
                            :document_id, :source_path, :title, :content_hash, :version
                        )
                        """
                    ),
                    {
                        "document_id": document_id,
                        "source_path": document.source_path,
                        "title": document.title,
                        "content_hash": content_hash,
                        "version": version,
                    },
                )
            else:
                document_id = current["document_id"]
                version = current["current_version"] + 1
                await session.execute(
                    text(
                        """
                        UPDATE knowledge_document
                        SET title = :title,
                            content_hash = :content_hash,
                            current_version = :version,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE document_id = :document_id
                        """
                    ),
                    {
                        "document_id": document_id,
                        "title": document.title,
                        "content_hash": content_hash,
                        "version": version,
                    },
                )
                await session.execute(
                    text("DELETE FROM knowledge_chunk WHERE document_id = :document_id"),
                    {"document_id": document_id},
                )

            await self._insert_chunks(session, document_id, version, chunks)
            return IngestionResult(
                document_id=document_id,
                version=version,
                chunk_count=len(chunks),
                changed=True,
            )

    async def _insert_chunks(
        self,
        session: AsyncSession,
        document_id: UUID,
        version: int,
        chunks: Sequence[EmbeddedChunk],
    ) -> None:
        """검증된 모든 Chunk를 현재 문서 버전에 일괄 저장한다."""
        statement = text(
            """
            INSERT INTO knowledge_chunk (
                chunk_id, document_id, document_version, chunk_index,
                heading, content, content_hash, embedding
            ) VALUES (
                :chunk_id, :document_id, :document_version, :chunk_index,
                :heading, :content, :content_hash, CAST(:embedding AS vector)
            )
            """
        )
        parameters = [
            {
                "chunk_id": uuid4(),
                "document_id": document_id,
                "document_version": version,
                "chunk_index": chunk.chunk_index,
                "heading": chunk.heading,
                "content": chunk.content,
                "content_hash": chunk.content_hash,
                "embedding": self._format_vector(chunk.embedding),
            }
            for chunk in chunks
        ]
        if parameters:
            await session.execute(statement, parameters)

    async def search(
        self,
        embedding: Sequence[float],
        top_k: int,
        min_score: float,
        max_chunks_per_document: int,
    ) -> list[SearchHit]:
        """코사인 유사도와 문서별 개수 제한을 적용해 현재 근거만 검색한다."""
        statement = text(
            """
            WITH ranked AS (
                SELECT c.chunk_id,
                       d.document_id,
                       d.source_path,
                       d.title,
                       c.document_version,
                       c.chunk_index,
                       c.heading,
                       c.content,
                       1 - (c.embedding <=> CAST(:embedding AS vector)) AS score,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.document_id
                           ORDER BY c.embedding <=> CAST(:embedding AS vector), c.chunk_index
                       ) AS document_rank
                FROM knowledge_chunk AS c
                JOIN knowledge_document AS d ON d.document_id = c.document_id
                WHERE c.document_version = d.current_version
            )
            SELECT chunk_id, document_id, source_path, title, document_version,
                   chunk_index, heading, content, score
            FROM ranked
            WHERE document_rank <= :max_chunks_per_document
              AND score >= :min_score
            ORDER BY score DESC, source_path, chunk_index
            LIMIT :top_k
            """
        )
        parameters = {
            "embedding": self._format_vector(embedding),
            "top_k": top_k,
            "min_score": min_score,
            "max_chunks_per_document": max_chunks_per_document,
        }
        async with self._session_factory() as session:
            rows = (await session.execute(statement, parameters)).mappings().all()
        return [SearchHit.model_validate(dict(row)) for row in rows]

    def _format_vector(self, values: Sequence[float]) -> str:
        """asyncpg가 pgvector에 안전하게 전달할 숫자 배열 문자열을 만든다."""
        return "[" + ",".join(format(value, ".17g") for value in values) + "]"
