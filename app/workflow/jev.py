import logging
from collections.abc import Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.retrieval.schemas import SearchHit
from app.workflow.model import (
    DocumentGradeClient,
    WorkflowModelError,
    WorkflowModelResponseError,
    WorkflowModelTimeoutError,
)
from app.workflow.schemas import DocumentGradeOutput


class JevBooleanQuestion(BaseModel):
    """Vercel Evaluation v4가 Jev에 전달할 이진 질문을 검증한다."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["boolean"] = "boolean"
    instructions: str = Field(min_length=1, max_length=2_000)


class JevGatewayRequest(BaseModel):
    """Vercel AI Gateway 공개 Evaluation HTTP 요청 본문을 검증한다."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    state: dict[str, object]
    questions: dict[str, JevBooleanQuestion] = Field(min_length=1)


class JevBooleanAnswer(BaseModel):
    """Vercel Evaluation v4 이진 판정 응답의 확률 범위를 검증한다."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["boolean"]
    probability: float = Field(ge=0.0, le=1.0)


class JevGatewayResponse(BaseModel):
    """관련성 판정에 필요한 Vercel Evaluation v4 응답 필드만 검증한다."""

    model_config = ConfigDict(extra="ignore")

    answers: dict[str, JevBooleanAnswer]


class JevDocumentGradeClient:
    """Jev 확률 판정을 기존 문서 관련성 출력 계약으로 변환한다."""

    _conflict_key = "document_conflict"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        relevance_threshold: float,
        conflict_threshold: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Secret과 확률 임계값, 테스트에서 교체 가능한 HTTP Client를 주입받는다."""
        if not api_key.get_secret_value():
            raise ValueError("AI Gateway API Key가 필요합니다.")
        self._api_key = api_key
        self._model = model
        self._relevance_threshold = relevance_threshold
        self._conflict_threshold = conflict_threshold
        self._logger = logging.getLogger(__name__)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """각 Chunk의 직접 관련성과 전체 문서 충돌을 한 번의 Jev 호출로 판정한다."""
        request = self._build_request(question, chunks)
        try:
            response = await self._client.post(
                "/v1/evaluate",
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=request.model_dump(mode="json"),
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise WorkflowModelTimeoutError(
                "Jev 문서 관련성 판정 시간이 초과됐습니다."
            ) from exc
        except httpx.HTTPError as exc:
            raise WorkflowModelError("Jev 문서 관련성 판정 호출에 실패했습니다.") from exc

        try:
            payload = JevGatewayResponse.model_validate(response.json())
            expected_keys = {
                *(self._chunk_key(index) for index in range(len(chunks))),
                self._conflict_key,
            }
            if not expected_keys.issubset(payload.answers):
                raise ValueError("Jev 응답에 요청한 판정 결과가 없습니다.")
        except (ValueError, ValidationError) as exc:
            raise WorkflowModelResponseError(
                "Jev 구조화 출력 형식이 올바르지 않습니다."
            ) from exc

        relevant_ids = [
            chunk.chunk_id
            for index, chunk in enumerate(chunks)
            if payload.answers[self._chunk_key(index)].probability
            >= self._relevance_threshold
        ]
        conflict_probability = payload.answers[self._conflict_key].probability
        self._logger.info(
            "jev_document_grade_succeeded model=%s chunk_count=%d "
            "relevant_count=%d conflict_probability=%.3f",
            self._model,
            len(chunks),
            len(relevant_ids),
            conflict_probability,
        )
        return DocumentGradeOutput(
            relevant_chunk_ids=relevant_ids,
            conflict_detected=conflict_probability >= self._conflict_threshold,
            reason=(
                "Jev 판정: "
                f"관련 Chunk {len(relevant_ids)}/{len(chunks)}, "
                f"충돌 확률 {conflict_probability:.3f}"
            ),
        )

    def _build_request(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> JevGatewayRequest:
        """질문과 검색 후보를 외부 명령과 분리된 구조화 상태로 직렬화한다."""
        state_chunks = [
            {
                "chunk_id": str(chunk.chunk_id),
                "title": chunk.title,
                "heading": chunk.heading,
                "content": chunk.content,
                "retrieval_score": chunk.score,
            }
            for chunk in chunks
        ]
        questions = {
            self._chunk_key(index): JevBooleanQuestion(
                instructions=(
                    f"chunk_id={chunk.chunk_id}가 사용자 질문의 답을 직접 뒷받침하는 "
                    "근거이면 높은 확률을 반환하세요. 단순 단어 중복은 근거가 아닙니다."
                )
            )
            for index, chunk in enumerate(chunks)
        }
        questions[self._conflict_key] = JevBooleanQuestion(
            instructions=(
                "검색 Chunk들이 사용자 질문에 적용되는 핵심 절차, 조건 또는 수치에서 "
                "서로 모순되면 높은 확률을 반환하세요."
            )
        )
        return JevGatewayRequest(
            model=self._model,
            state={"question": question, "chunks": state_chunks},
            questions=questions,
        )

    def _chunk_key(self, index: int) -> str:
        """응답을 원래 Chunk 순서에 안전하게 연결할 고정 질문 키를 만든다."""
        return f"chunk_{index}"

    async def aclose(self) -> None:
        """내부에서 생성한 Jev HTTP 연결 풀만 안전하게 종료한다."""
        if self._owns_client:
            await self._client.aclose()


class FallbackDocumentGradeClient:
    """Jev 장애 시 핵심 Workflow를 로컬 Ollama 판정으로 계속 실행한다."""

    def __init__(
        self,
        primary: DocumentGradeClient,
        fallback: DocumentGradeClient,
    ) -> None:
        """우선 판정기와 외부 장애 시 사용할 로컬 판정기를 주입받는다."""
        self._primary = primary
        self._fallback = fallback
        self._logger = logging.getLogger(__name__)

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """Jev 계열 오류만 기록하고 동일 입력을 로컬 판정기로 넘긴다."""
        try:
            return await self._primary.grade_documents(question, chunks)
        except WorkflowModelError as error:
            self._logger.warning(
                "jev_document_grade_fallback error_code=%s",
                type(error).__name__,
            )
            return await self._fallback.grade_documents(question, chunks)
