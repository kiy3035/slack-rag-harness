import httpx
import pytest

from app.retrieval.embedding import (
    EmbeddingResponseError,
    EmbeddingTimeoutError,
    OllamaEmbeddingClient,
)


def valid_embedding_response(request: httpx.Request) -> httpx.Response:
    """정상 Ollama 계약을 재현하는 3차원 응답을 반환한다."""
    return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]}, request=request)


def timeout_embedding_response(request: httpx.Request) -> httpx.Response:
    """네트워크 제한 시간을 재현해 일시 오류 분류를 검증하게 한다."""
    raise httpx.ReadTimeout("느린 Ollama", request=request)


def malformed_embedding_response(request: httpx.Request) -> httpx.Response:
    """필수 embeddings 필드가 빠진 영구 계약 오류 응답을 반환한다."""
    return httpx.Response(200, json={"model": "fake"}, request=request)


async def test_ollama_embedding_client_returns_validated_vectors() -> None:
    """정상 응답이 입력 순서의 숫자 벡터로 반환되는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(valid_embedding_response),
        base_url="http://ollama.test",
    ) as http_client:
        client = OllamaEmbeddingClient(
            base_url="http://ollama.test",
            model="local-test",
            dimensions=3,
            timeout_seconds=1.0,
            client=http_client,
        )

        assert await client.embed(["질문"]) == [[0.1, 0.2, 0.3]]


async def test_ollama_embedding_client_classifies_timeout() -> None:
    """Ollama timeout이 재시도 가능한 전용 오류로 변환되는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_embedding_response),
        base_url="http://ollama.test",
    ) as http_client:
        client = OllamaEmbeddingClient(
            base_url="http://ollama.test",
            model="local-test",
            dimensions=3,
            timeout_seconds=1.0,
            client=http_client,
        )

        with pytest.raises(EmbeddingTimeoutError, match="초과"):
            await client.embed(["질문"])


async def test_ollama_embedding_client_rejects_malformed_response() -> None:
    """Ollama의 잘못된 JSON 계약이 저장 단계 전에 차단되는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(malformed_embedding_response),
        base_url="http://ollama.test",
    ) as http_client:
        client = OllamaEmbeddingClient(
            base_url="http://ollama.test",
            model="local-test",
            dimensions=3,
            timeout_seconds=1.0,
            client=http_client,
        )

        with pytest.raises(EmbeddingResponseError, match="형식"):
            await client.embed(["질문"])

