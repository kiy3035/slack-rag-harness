from collections.abc import Sequence
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.retrieval.schemas import SearchHit
from app.workflow.prompts import (
    ANSWER_SYSTEM_PROMPT,
    DOCUMENT_GRADE_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    build_answer_prompt,
    build_document_grade_prompt,
    build_intent_prompt,
    build_rewrite_prompt,
)
from app.workflow.schemas import (
    AnswerOutput,
    DocumentGradeOutput,
    IntentOutput,
    RewriteQueryOutput,
)


def default_generation_options() -> dict[str, float]:
    """결정적 구조화 출력을 위해 Ollama temperature 기본값을 만든다."""
    return {"temperature": 0.0}


class WorkflowModelError(RuntimeError):
    """로컬 생성 모델 호출이 안전하게 완료되지 못한 기본 오류다."""


class WorkflowModelTimeoutError(WorkflowModelError):
    """Ollama 생성 제한 시간 초과가 일시 오류임을 나타낸다."""


class WorkflowModelResponseError(WorkflowModelError):
    """Ollama 응답 또는 구조화 출력이 계약을 위반했음을 나타낸다."""


class WorkflowModelClient(Protocol):
    """실제 Ollama와 결정적 Fake가 공유하는 생성 모델 경계다."""

    async def classify_intent(self, question: str) -> IntentOutput:
        """질문 의도와 위험 등급을 검증된 구조로 반환한다."""
        ...

    async def generate_answer(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> AnswerOutput:
        """이번 실행의 검색 Chunk만 인용하는 구조화 답변을 반환한다."""
        ...

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """검색 Chunk 중 질문을 직접 뒷받침하는 근거 ID를 반환한다."""
        ...

    async def rewrite_query(self, question: str, reason: str) -> RewriteQueryOutput:
        """검색 부족 사유를 반영하되 원래 의미를 유지한 검색어를 반환한다."""
        ...


class OllamaGenerateRequest(BaseModel):
    """Ollama generate API의 비스트리밍 구조화 출력 요청을 검증한다."""

    model: str = Field(min_length=1)
    system: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    stream: bool = False
    format: dict[str, object]
    options: dict[str, float] = Field(default_factory=default_generation_options)


class OllamaGenerateResponse(BaseModel):
    """Ollama generate API에서 필요한 완료 본문만 검증한다."""

    model_config = ConfigDict(extra="ignore")

    response: str = Field(min_length=1)
    done: bool


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class OllamaWorkflowModelClient:
    """로컬 Ollama의 JSON Schema 출력을 Pydantic 모델로 강제한다."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """모델 설정과 테스트에서 교체 가능한 HTTP Client를 주입받는다."""
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._owns_client = client is None

    async def classify_intent(self, question: str) -> IntentOutput:
        """질문을 스키마 기반 의도와 위험 등급으로 분류한다."""
        return await self._generate(
            system_prompt=INTENT_SYSTEM_PROMPT,
            prompt=build_intent_prompt(question),
            output_type=IntentOutput,
        )

    async def generate_answer(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> AnswerOutput:
        """검색 Context와 인용 식별자를 제한한 구조화 답변을 생성한다."""
        return await self._generate(
            system_prompt=ANSWER_SYSTEM_PROMPT,
            prompt=build_answer_prompt(question, chunks),
            output_type=AnswerOutput,
        )

    async def grade_documents(
        self, question: str, chunks: Sequence[SearchHit]
    ) -> DocumentGradeOutput:
        """검색 결과의 관련성과 충돌 여부를 JSON Schema로 판정한다."""
        return await self._generate(
            system_prompt=DOCUMENT_GRADE_SYSTEM_PROMPT,
            prompt=build_document_grade_prompt(question, chunks),
            output_type=DocumentGradeOutput,
        )

    async def rewrite_query(self, question: str, reason: str) -> RewriteQueryOutput:
        """근거 부족 질문을 한정된 검색어 구조로 재작성한다."""
        return await self._generate(
            system_prompt=REWRITE_SYSTEM_PROMPT,
            prompt=build_rewrite_prompt(question, reason),
            output_type=RewriteQueryOutput,
        )

    async def _generate(
        self,
        *,
        system_prompt: str,
        prompt: str,
        output_type: type[OutputModel],
    ) -> OutputModel:
        """공통 Ollama 호출과 HTTP·JSON·Pydantic 오류 변환을 수행한다."""
        request = OllamaGenerateRequest(
            model=self._model,
            system=system_prompt,
            prompt=prompt,
            format=output_type.model_json_schema(),
        )
        try:
            response = await self._client.post("/api/generate", json=request.model_dump())
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise WorkflowModelTimeoutError("Ollama 답변 생성 시간이 초과됐습니다.") from exc
        except httpx.HTTPError as exc:
            raise WorkflowModelError("Ollama 답변 생성 호출에 실패했습니다.") from exc

        try:
            payload = OllamaGenerateResponse.model_validate(response.json())
            if not payload.done:
                raise ValueError("비스트리밍 응답이 완료되지 않았습니다.")
            return output_type.model_validate_json(payload.response)
        except (ValueError, ValidationError) as exc:
            raise WorkflowModelResponseError(
                "Ollama 구조화 출력 형식이 올바르지 않습니다."
            ) from exc

    async def aclose(self) -> None:
        """내부에서 생성한 HTTP 연결 풀만 안전하게 종료한다."""
        if self._owns_client:
            await self._client.aclose()
