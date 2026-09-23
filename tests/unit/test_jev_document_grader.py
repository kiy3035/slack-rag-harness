import json
import logging
from collections.abc import Sequence
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.retrieval.schemas import SearchHit
from app.workflow.jev import FallbackDocumentGradeClient, JevDocumentGradeClient
from app.workflow.model import WorkflowModelResponseError, WorkflowModelTimeoutError
from app.workflow.schemas import DocumentGradeOutput


DOCUMENT_ID = UUID("77777777-7777-7777-7777-777777777777")
FIRST_CHUNK_ID = UUID("88888888-8888-8888-8888-888888888888")
SECOND_CHUNK_ID = UUID("99999999-9999-9999-9999-999999999999")


def build_search_hits() -> list[SearchHit]:
    """Jev 관련성 경계 테스트에 사용할 두 개의 가상 문서 Chunk를 만든다."""
    return [
        SearchHit(
            chunk_id=FIRST_CHUNK_ID,
            document_id=DOCUMENT_ID,
            source_path="knowledge/manuals/settlement.md",
            title="정산 배치 운영 절차",
            document_version=2,
            chunk_index=0,
            heading="마감 전 점검",
            content="마감 전에 중복 실행 여부와 승인 상태를 확인한다.",
            score=0.91,
        ),
        SearchHit(
            chunk_id=SECOND_CHUNK_ID,
            document_id=DOCUMENT_ID,
            source_path="knowledge/manuals/settlement.md",
            title="정산 배치 운영 절차",
            document_version=2,
            chunk_index=1,
            heading="장애 공지",
            content="장애가 확인되면 고객 공지 초안을 작성한다.",
            score=0.62,
        ),
    ]


def valid_jev_response(request: httpx.Request) -> httpx.Response:
    """Authorization과 구조화 상태를 확인한 뒤 결정적인 Jev 응답을 반환한다."""
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert request.headers["Content-Type"] == "application/json"
    assert request.url.path == "/v1/evaluate"
    payload = json.loads(request.content)
    assert payload["model"] == "typesafe-ai/jev"
    assert payload["state"]["question"] == "정산 마감 전에 무엇을 확인하나요?"
    assert len(payload["state"]["chunks"]) == 2
    assert set(payload["questions"]) == {
        "chunk_0",
        "chunk_1",
        "document_conflict",
    }
    return httpx.Response(
        200,
        json={
            "answers": {
                "chunk_0": {"type": "boolean", "probability": 0.92},
                "chunk_1": {"type": "boolean", "probability": 0.41},
                "document_conflict": {
                    "type": "boolean",
                    "probability": 0.2,
                },
            },
        },
        request=request,
    )


def malformed_jev_response(request: httpx.Request) -> httpx.Response:
    """요청한 Chunk 판정 하나가 빠진 Jev 응답을 반환한다."""
    return httpx.Response(
        200,
        json={
            "answers": {
                "chunk_0": {"type": "boolean", "probability": 0.9},
                "document_conflict": {
                    "type": "boolean",
                    "probability": 0.1,
                },
            },
        },
        request=request,
    )


def timeout_jev_response(request: httpx.Request) -> httpx.Response:
    """외부 Jev 판정의 읽기 제한 시간 초과를 재현한다."""
    raise httpx.ReadTimeout("느린 Jev 응답", request=request)


class FailingGradeClient:
    """로컬 폴백 경로를 검증하기 위해 Jev 응답 오류를 발생시킨다."""

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """외부 구조화 응답이 깨진 상황을 전용 오류로 재현한다."""
        raise WorkflowModelResponseError("잘못된 Jev 응답")


class LocalGradeClient:
    """Jev 장애 뒤 호출 여부를 확인할 결정적 로컬 판정기다."""

    def __init__(self) -> None:
        """폴백 호출 횟수를 0으로 초기화한다."""
        self.calls = 0

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """첫 Chunk를 관련 근거로 선택하고 호출 횟수를 기록한다."""
        self.calls += 1
        return DocumentGradeOutput(
            relevant_chunk_ids=[chunks[0].chunk_id],
            reason="로컬 폴백 판정",
        )


@pytest.mark.asyncio
async def test_jev_grader_maps_probabilities_to_relevant_chunks() -> None:
    """Jev 확률이 설정 임계값을 넘은 Chunk만 기존 관련성 계약에 포함하는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(valid_jev_response),
        base_url="https://gateway.test",
    ) as http_client:
        client = JevDocumentGradeClient(
            base_url="https://gateway.test",
            api_key=SecretStr("test-secret"),
            model="typesafe-ai/jev",
            timeout_seconds=1.0,
            relevance_threshold=0.7,
            conflict_threshold=0.7,
            client=http_client,
        )
        result = await client.grade_documents(
            "정산 마감 전에 무엇을 확인하나요?", build_search_hits()
        )

    assert result.relevant_chunk_ids == [FIRST_CHUNK_ID]
    assert result.conflict_detected is False
    assert "1/2" in result.reason


@pytest.mark.asyncio
async def test_jev_grader_logs_safe_success_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """성공 로그가 질문·문서·Secret 없이 판정 요약만 남기는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(valid_jev_response),
        base_url="https://gateway.test",
    ) as http_client:
        client = JevDocumentGradeClient(
            base_url="https://gateway.test",
            api_key=SecretStr("test-secret"),
            model="typesafe-ai/jev",
            timeout_seconds=1.0,
            relevance_threshold=0.7,
            conflict_threshold=0.7,
            client=http_client,
        )
        with caplog.at_level(logging.INFO, logger="app.workflow.jev"):
            await client.grade_documents(
                "정산 마감 전에 무엇을 확인하나요?", build_search_hits()
            )

    message = caplog.messages[-1]
    assert "jev_document_grade_succeeded" in message
    assert "chunk_count=2" in message
    assert "relevant_count=1" in message
    assert "정산 마감" not in message
    assert "test-secret" not in message


@pytest.mark.asyncio
async def test_jev_grader_rejects_incomplete_structured_response() -> None:
    """요청한 판정이 누락된 Jev 응답을 관련성 결과로 사용하지 않는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(malformed_jev_response),
        base_url="https://gateway.test",
    ) as http_client:
        client = JevDocumentGradeClient(
            base_url="https://gateway.test",
            api_key=SecretStr("test-secret"),
            model="typesafe-ai/jev",
            timeout_seconds=1.0,
            relevance_threshold=0.7,
            conflict_threshold=0.7,
            client=http_client,
        )
        with pytest.raises(WorkflowModelResponseError, match="구조화 출력"):
            await client.grade_documents("질문", build_search_hits())


@pytest.mark.asyncio
async def test_jev_grader_classifies_timeout() -> None:
    """Jev timeout을 후속 장애 정책이 구분할 수 있는 전용 오류로 변환하는지 검증한다."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_jev_response),
        base_url="https://gateway.test",
    ) as http_client:
        client = JevDocumentGradeClient(
            base_url="https://gateway.test",
            api_key=SecretStr("test-secret"),
            model="typesafe-ai/jev",
            timeout_seconds=1.0,
            relevance_threshold=0.7,
            conflict_threshold=0.7,
            client=http_client,
        )
        with pytest.raises(WorkflowModelTimeoutError, match="초과"):
            await client.grade_documents("질문", build_search_hits())


@pytest.mark.asyncio
async def test_jev_failure_falls_back_to_local_grader() -> None:
    """Jev 호출이 실패해도 동일 검색 입력을 로컬 판정기로 처리하는지 검증한다."""
    local_client = LocalGradeClient()
    client = FallbackDocumentGradeClient(FailingGradeClient(), local_client)

    result = await client.grade_documents("정산 질문", build_search_hits())

    assert result.relevant_chunk_ids == [FIRST_CHUNK_ID]
    assert local_client.calls == 1


def test_jev_grader_requires_non_empty_secret() -> None:
    """Jev가 선택됐는데 API Key가 비어 있으면 외부 호출 전에 명확히 실패하는지 검증한다."""
    with pytest.raises(ValueError, match="API Key"):
        JevDocumentGradeClient(
            base_url="https://gateway.test",
            api_key=SecretStr(""),
            model="typesafe-ai/jev",
            timeout_seconds=1.0,
            relevance_threshold=0.7,
            conflict_threshold=0.7,
        )
