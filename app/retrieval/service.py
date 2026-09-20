import hashlib
import math
from collections.abc import Sequence

from app.retrieval.chunker import MarkdownChunker
from app.retrieval.embedding import EmbeddingClient, EmbeddingResponseError
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.schemas import EmbeddedChunk, IngestionResult, MarkdownDocument, SearchHit


class DocumentIngestionService:
    """문서를 분할·임베딩한 뒤 변경된 버전만 원자적으로 저장한다."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        chunker: MarkdownChunker,
        embedding_client: EmbeddingClient,
        embedding_dimensions: int,
    ) -> None:
        """DB, 분할 규칙, 외부 임베딩 경계를 명시적으로 주입받는다."""
        self._repository = repository
        self._chunker = chunker
        self._embedding_client = embedding_client
        self._embedding_dimensions = embedding_dimensions

    async def ingest(self, document: MarkdownDocument) -> IngestionResult:
        """동일 원문은 호출을 생략하고 변경된 문서만 새 버전으로 적재한다."""
        normalized_content = document.content.replace("\r\n", "\n").replace("\r", "\n").strip()
        hash_input = f"{document.title}\0{normalized_content}"
        content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        current = await self._repository.get_document_state(document.source_path)
        if current is not None and current.content_hash == content_hash:
            return IngestionResult(
                document_id=current.document_id,
                version=current.current_version,
                chunk_count=current.chunk_count,
                changed=False,
            )

        chunks = self._chunker.split(normalized_content)
        embeddings = await self._embedding_client.embed([chunk.content for chunk in chunks])
        self._validate_embeddings(embeddings, len(chunks))
        embedded_chunks = [
            EmbeddedChunk(**chunk.model_dump(), embedding=embedding)
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
        return await self._repository.replace_document(
            document=document,
            content_hash=content_hash,
            chunks=embedded_chunks,
        )

    def _validate_embeddings(self, embeddings: Sequence[Sequence[float]], expected: int) -> None:
        """Fake Client까지 동일한 개수·차원·유한값 계약을 지키도록 검사한다."""
        if len(embeddings) != expected:
            raise EmbeddingResponseError("임베딩 개수가 Chunk 개수와 다릅니다.")
        for embedding in embeddings:
            if len(embedding) != self._embedding_dimensions:
                raise EmbeddingResponseError("임베딩 차원이 DB 설정과 다릅니다.")
            if not all(math.isfinite(value) for value in embedding):
                raise EmbeddingResponseError("임베딩에 유한하지 않은 값이 있습니다.")


class KnowledgeSearchService:
    """질문 임베딩과 pgvector 검색을 하나의 검증된 경계로 제공한다."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        embedding_client: EmbeddingClient,
        embedding_dimensions: int,
        top_k: int,
        min_score: float,
        max_chunks_per_document: int,
    ) -> None:
        """검색 후보 수와 점수·문서별 제한을 설정으로 주입받는다."""
        self._repository = repository
        self._embedding_client = embedding_client
        self._embedding_dimensions = embedding_dimensions
        self._top_k = top_k
        self._min_score = min_score
        self._max_chunks_per_document = max_chunks_per_document

    async def search(self, question: str) -> list[SearchHit]:
        """빈 질문을 거부하고 질문과 가장 가까운 현재 버전 Chunk를 반환한다."""
        normalized = " ".join(question.split())
        if not normalized:
            raise ValueError("검색 질문은 비어 있을 수 없습니다.")
        embeddings = await self._embedding_client.embed([normalized])
        if (
            len(embeddings) != 1
            or len(embeddings[0]) != self._embedding_dimensions
            or not all(math.isfinite(value) for value in embeddings[0])
        ):
            raise EmbeddingResponseError("질문 임베딩 응답이 검색 계약과 다릅니다.")
        return await self._repository.search(
            embedding=embeddings[0],
            top_k=self._top_k,
            min_score=self._min_score,
            max_chunks_per_document=self._max_chunks_per_document,
        )
