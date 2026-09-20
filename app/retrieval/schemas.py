from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MarkdownDocument(BaseModel):
    """적재할 Markdown 문서의 식별 정보와 원문을 검증한다."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source_path: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)


class DocumentChunk(BaseModel):
    """분할된 문서 Chunk의 순서와 무결성 정보를 표현한다."""

    chunk_index: int = Field(ge=0)
    heading: str | None = Field(default=None, max_length=500)
    content: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)


class EmbeddedChunk(DocumentChunk):
    """DB에 저장할 Chunk와 검증된 임베딩을 함께 표현한다."""

    embedding: list[float] = Field(min_length=1)


class DocumentState(BaseModel):
    """재적재 여부 판단에 필요한 현재 문서 상태만 노출한다."""

    document_id: UUID
    content_hash: str
    current_version: int = Field(ge=1)
    chunk_count: int = Field(ge=0)


class IngestionResult(BaseModel):
    """문서 적재 결과와 실제 변경 여부를 호출자에게 반환한다."""

    document_id: UUID
    version: int = Field(ge=1)
    chunk_count: int = Field(ge=0)
    changed: bool


class SearchHit(BaseModel):
    """검색된 근거 Chunk와 코사인 유사도 점수를 표현한다."""

    chunk_id: UUID
    document_id: UUID
    source_path: str
    title: str
    document_version: int = Field(ge=1)
    chunk_index: int = Field(ge=0)
    heading: str | None
    content: str
    score: float = Field(ge=-1.0, le=1.0)


class OllamaEmbedRequest(BaseModel):
    """Ollama embed API에 보낼 모델과 입력 목록을 검증한다."""

    model: str = Field(min_length=1)
    input: list[str] = Field(min_length=1)


class OllamaEmbedResponse(BaseModel):
    """Ollama embed API의 임베딩 배열 구조를 검증한다."""

    embeddings: list[list[float]] = Field(min_length=1)

