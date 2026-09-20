import json
from uuid import UUID

import httpx
import pytest

from app.retrieval.schemas import SearchHit
from app.workflow.model import (
    OllamaWorkflowModelClient,
    WorkflowModelResponseError,
    WorkflowModelTimeoutError,
)
from app.workflow.schemas import IntentCategory, RiskLevel


DOCUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")
CHUNK_ID = UUID("22222222-2222-2222-2222-222222222222")


def build_search_hit() -> SearchHit:
    """구조화 답변 계약 테스트에 사용할 고정 검색 결과를 만든다."""
    return SearchHit(
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        source_path="knowledge/manuals/test.md",
        title="테스트 절차",
        document_version=1,
        chunk_index=0,
        heading="절차",
        content="승인 후 순서대로 처리한다.",
        score=0.9,
    )


def valid_model_response(request: httpx.Request) -> httpx.Response:
    """요청 스키마에 따라 정상 의도 또는 답변 JSON을 반환한다."""
    payload = json.loads(request.content)
    assert payload["stream"] is False
    assert payload["options"]["temperature"] == 0.0
    assert payload["format"]["type"] == "object"
    if "위험" in payload["system"]:
        output = {
            "intent": "PROCEDURE",
            "risk_level": "LOW",
            "reason": "운영 절차 질문",
        }
    else:
        output = {
            "answer": "승인 후 순서대로 처리합니다.",
            "citations": [
                {"document_id": str(DOCUMENT_ID), "chunk_id": str(CHUNK_ID)}
            ],
            "needs_review": False,
            "review_reason": None,
        }
    return httpx.Response(
        200,
        json={"response": json.dumps(output, ensure_ascii=False), "done": True},
        request=request,
    )


def malformed_model_response(request: httpx.Request) -> httpx.Response:
    """Pydantic 출력 계약을 위반한 Ollama 응답을 반환한다."""
    return httpx.Response(
        200,
        json={"response": '{"unknown": true}', "done": True},
        request=request,
    )


def timeout_model_response(request: httpx.Request) -> httpx.Response:
    """Ollama 생성 제한 시간 초과를 재현한다."""
    raise httpx.ReadTimeout("느린 생성 모델", request=request)


@pytest.mark.asyncio
async def test_ollama_model_validates_intent_and_answer_schema() -> None:
    """Ollama JSON Schema 요청이 검증된 의도와 근거 답변으로 변환되는지 확인한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(valid_model_response),
        base_url="http://ollama.test",
    ) as http_client:
        client = OllamaWorkflowModelClient(
            base_url="http://ollama.test",
            model="local-test",
            timeout_seconds=1.0,
            client=http_client,
        )
        intent = await client.classify_intent("정산 절차는?")
        answer = await client.generate_answer("정산 절차는?", [build_search_hit()])

    assert intent.intent == IntentCategory.PROCEDURE
    assert intent.risk_level == RiskLevel.LOW
    assert answer.citations[0].chunk_id == CHUNK_ID


@pytest.mark.asyncio
async def test_ollama_model_rejects_malformed_structured_output() -> None:
    """필수 필드가 없는 모델 출력이 Workflow 상태에 저장되기 전에 거부되는지 확인한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(malformed_model_response),
        base_url="http://ollama.test",
    ) as http_client:
        client = OllamaWorkflowModelClient(
            base_url="http://ollama.test",
            model="local-test",
            timeout_seconds=1.0,
            client=http_client,
        )
        with pytest.raises(WorkflowModelResponseError, match="구조화 출력"):
            await client.classify_intent("질문")


@pytest.mark.asyncio
async def test_ollama_model_classifies_timeout() -> None:
    """생성 timeout이 후속 장애 정책에서 구분할 수 있는 전용 오류로 변환되는지 확인한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_model_response),
        base_url="http://ollama.test",
    ) as http_client:
        client = OllamaWorkflowModelClient(
            base_url="http://ollama.test",
            model="local-test",
            timeout_seconds=1.0,
            client=http_client,
        )
        with pytest.raises(WorkflowModelTimeoutError, match="초과"):
            await client.classify_intent("질문")
