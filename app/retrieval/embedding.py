import math
from collections.abc import Sequence
from enum import Enum
from time import perf_counter
from typing import Protocol

import httpx
from pydantic import ValidationError

from app.observability.metrics import observe_ollama_call

from app.retrieval.schemas import OllamaEmbedRequest, OllamaEmbedResponse


class EmbeddingError(RuntimeError):
    """임베딩 생성이 안전하게 완료되지 못한 경우의 기본 오류다."""


class EmbeddingTimeoutError(EmbeddingError):
    """Ollama 제한 시간을 초과해 재시도할 수 있음을 나타낸다."""


class EmbeddingResponseError(EmbeddingError):
    """Ollama 응답이 계약이나 기대 차원을 위반했음을 나타낸다."""


class EmbeddingTask(str, Enum):
    """Nomic 모델이 문서와 질문을 서로 다른 검색 공간에 배치하도록 구분한다."""

    DOCUMENT = "search_document"
    QUERY = "search_query"


class EmbeddingClient(Protocol):
    """실제 Ollama와 테스트 Fake가 공유하는 임베딩 경계다."""

    @property
    def index_fingerprint(self) -> str:
        """모델이나 전처리 변경 시 기존 문서를 다시 적재할 식별자를 반환한다."""
        ...

    async def embed(
        self, texts: Sequence[str], task: EmbeddingTask
    ) -> list[list[float]]:
        """작업 유형을 반영하고 입력 순서를 보존한 임베딩 목록을 생성한다."""
        ...


class OllamaEmbeddingClient:
    """로컬 Ollama embed API를 호출하고 응답 계약을 엄격히 검사한다."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        document_prefix: str = "search_document: ",
        query_prefix: str = "search_query: ",
    ) -> None:
        """연결 설정과 테스트에서 교체 가능한 HTTP Client를 주입받는다."""
        self._model = model
        self._dimensions = dimensions
        self._prefixes = {
            EmbeddingTask.DOCUMENT: document_prefix,
            EmbeddingTask.QUERY: query_prefix,
        }
        self._index_fingerprint = f"{model}|{document_prefix}|{query_prefix}"
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    @property
    def index_fingerprint(self) -> str:
        """현재 모델과 Nomic 검색 접두어 조합을 재적재 식별자로 반환한다."""
        return self._index_fingerprint

    async def embed(
        self, texts: Sequence[str], task: EmbeddingTask
    ) -> list[list[float]]:
        """Ollama 응답의 개수·차원·유한값을 검증해 임베딩만 반환한다."""
        started_at = perf_counter()
        outcome = "failed"
        prefix = self._prefixes[task]
        request = OllamaEmbedRequest(
            model=self._model,
            input=[f"{prefix}{text}" for text in texts],
        )
        try:
            response = await self._client.post("/api/embed", json=request.model_dump())
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            observe_ollama_call(
                operation=f"embedding_{task.value}",
                outcome=outcome,
                duration_seconds=perf_counter() - started_at,
            )
            raise EmbeddingTimeoutError("Ollama 임베딩 호출 시간이 초과됐습니다.") from exc
        except httpx.HTTPError as exc:
            outcome = "http_error"
            observe_ollama_call(
                operation=f"embedding_{task.value}",
                outcome=outcome,
                duration_seconds=perf_counter() - started_at,
            )
            raise EmbeddingError("Ollama 임베딩 호출에 실패했습니다.") from exc
        try:
            try:
                payload = OllamaEmbedResponse.model_validate(response.json())
            except (ValueError, ValidationError) as exc:
                outcome = "invalid_response"
                raise EmbeddingResponseError("Ollama 임베딩 응답 형식이 올바르지 않습니다.") from exc

            if len(payload.embeddings) != len(request.input):
                outcome = "invalid_response"
                raise EmbeddingResponseError("Ollama 임베딩 개수가 입력 개수와 다릅니다.")
            for embedding in payload.embeddings:
                if len(embedding) != self._dimensions:
                    outcome = "invalid_response"
                    raise EmbeddingResponseError("Ollama 임베딩 차원이 DB 설정과 다릅니다.")
                if not all(math.isfinite(value) for value in embedding):
                    outcome = "invalid_response"
                    raise EmbeddingResponseError("Ollama 임베딩에 유한하지 않은 값이 있습니다.")
            outcome = "completed"
            return payload.embeddings
        finally:
            observe_ollama_call(
                operation=f"embedding_{task.value}",
                outcome=outcome,
                duration_seconds=perf_counter() - started_at,
            )

    async def aclose(self) -> None:
        """내부에서 생성한 HTTP 연결 풀만 안전하게 종료한다."""
        if self._owns_client:
            await self._client.aclose()
