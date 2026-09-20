import math
from collections.abc import Sequence
from typing import Protocol

import httpx
from pydantic import ValidationError

from app.retrieval.schemas import OllamaEmbedRequest, OllamaEmbedResponse


class EmbeddingError(RuntimeError):
    """임베딩 생성이 안전하게 완료되지 못한 경우의 기본 오류다."""


class EmbeddingTimeoutError(EmbeddingError):
    """Ollama 제한 시간을 초과해 재시도할 수 있음을 나타낸다."""


class EmbeddingResponseError(EmbeddingError):
    """Ollama 응답이 계약이나 기대 차원을 위반했음을 나타낸다."""


class EmbeddingClient(Protocol):
    """실제 Ollama와 테스트 Fake가 공유하는 임베딩 경계다."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """입력 순서를 보존한 임베딩 목록을 생성한다."""
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
    ) -> None:
        """연결 설정과 테스트에서 교체 가능한 HTTP Client를 주입받는다."""
        self._model = model
        self._dimensions = dimensions
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Ollama 응답의 개수·차원·유한값을 검증해 임베딩만 반환한다."""
        request = OllamaEmbedRequest(model=self._model, input=list(texts))
        try:
            response = await self._client.post("/api/embed", json=request.model_dump())
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeoutError("Ollama 임베딩 호출 시간이 초과됐습니다.") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError("Ollama 임베딩 호출에 실패했습니다.") from exc

        try:
            payload = OllamaEmbedResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise EmbeddingResponseError("Ollama 임베딩 응답 형식이 올바르지 않습니다.") from exc

        if len(payload.embeddings) != len(request.input):
            raise EmbeddingResponseError("Ollama 임베딩 개수가 입력 개수와 다릅니다.")
        for embedding in payload.embeddings:
            if len(embedding) != self._dimensions:
                raise EmbeddingResponseError("Ollama 임베딩 차원이 DB 설정과 다릅니다.")
            if not all(math.isfinite(value) for value in embedding):
                raise EmbeddingResponseError("Ollama 임베딩에 유한하지 않은 값이 있습니다.")
        return payload.embeddings

    async def aclose(self) -> None:
        """내부에서 생성한 HTTP 연결 풀만 안전하게 종료한다."""
        if self._owns_client:
            await self._client.aclose()
