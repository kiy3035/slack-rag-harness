from collections.abc import Awaitable, Callable
import logging
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.common.domain import JobStatus
from app.retrieval.schemas import SearchHit
from app.workflow.model import WorkflowModelClient
from app.workflow.schemas import (
    AnswerOutput,
    RiskLevel,
    WorkflowState,
    WorkflowUpdate,
)


class SearchService(Protocol):
    """실제 pgvector 검색과 테스트 Fake가 공유하는 검색 경계다."""

    async def search(self, question: str) -> list[SearchHit]:
        """정규화된 질문에 대한 현재 문서 Chunk를 반환한다."""
        ...


NodeOperation = Callable[[WorkflowState], Awaitable[WorkflowUpdate]]


class WorkflowOutputError(RuntimeError):
    """생성 답변이 이번 검색 근거 계약을 위반했음을 나타낸다."""


class WorkflowNodes:
    """입력 검증부터 근거 기반 답변 생성까지 노드 책임을 분리한다."""

    def __init__(
        self,
        search_service: SearchService,
        model_client: WorkflowModelClient,
        question_max_chars: int,
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        """검색·생성 외부 경계와 검증 한계, 교체 가능한 시계를 주입한다."""
        self._search_service = search_service
        self._model_client = model_client
        self._question_max_chars = question_max_chars
        self._clock = monotonic_clock
        self._logger = logging.getLogger(__name__)

    async def validate_input(self, state: WorkflowState) -> WorkflowUpdate:
        """질문을 정규화하고 비어 있거나 과도한 입력을 영구 거부한다."""
        return await self._run_node("validate_input", state, self._validate_input)

    async def classify_intent(self, state: WorkflowState) -> WorkflowUpdate:
        """정규화된 질문의 의도와 자동 처리 위험을 구조화해 저장한다."""
        return await self._run_node("classify_intent", state, self._classify_intent)

    async def retrieve_documents(self, state: WorkflowState) -> WorkflowUpdate:
        """질문과 가장 가까운 현재 문서 Chunk를 pgvector에서 검색한다."""
        return await self._run_node(
            "retrieve_documents", state, self._retrieve_documents
        )

    async def generate_answer(self, state: WorkflowState) -> WorkflowUpdate:
        """검색 Chunk만 허용한 구조화 답변을 생성하고 인용 집합을 제한한다."""
        return await self._run_node("generate_answer", state, self._generate_answer)

    async def _run_node(
        self,
        node_name: str,
        state: WorkflowState,
        operation: NodeOperation,
    ) -> WorkflowUpdate:
        """노드별 시작·종료·실패와 실제 수행 시간을 일관되게 기록한다."""
        started_at = self._clock()
        self._logger.info(
            "workflow_node_started node=%s job_id=%s thread_id=%s",
            node_name,
            state.get("job_id"),
            state.get("thread_id"),
        )
        try:
            update = await operation(state)
        except Exception as error:
            elapsed_ms = (self._clock() - started_at) * 1_000
            self._logger.error(
                "workflow_node_failed node=%s job_id=%s thread_id=%s "
                "duration_ms=%.3f error_code=%s",
                node_name,
                state.get("job_id"),
                state.get("thread_id"),
                elapsed_ms,
                type(error).__name__,
            )
            raise
        elapsed_ms = (self._clock() - started_at) * 1_000
        self._logger.info(
            "workflow_node_completed node=%s job_id=%s thread_id=%s duration_ms=%.3f",
            node_name,
            state.get("job_id"),
            state.get("thread_id"),
            elapsed_ms,
        )
        return {**update, "last_node": node_name}

    async def _validate_input(self, state: WorkflowState) -> WorkflowUpdate:
        """공백을 한 칸으로 정규화하고 설정된 최대 질문 길이를 적용한다."""
        normalized = " ".join(state["question"].split())
        if not normalized:
            raise ValueError("질문은 비어 있을 수 없습니다.")
        if len(normalized) > self._question_max_chars:
            raise ValueError("질문이 허용 길이를 초과했습니다.")
        return {"normalized_question": normalized}

    async def _classify_intent(self, state: WorkflowState) -> WorkflowUpdate:
        """생성 모델의 분류를 Enum 값으로 검증하고 위험 질문을 중단 표시한다."""
        result = await self._model_client.classify_intent(state["normalized_question"])
        final_status = (
            JobStatus.REVIEW_REQUIRED.value
            if result.risk_level != RiskLevel.LOW
            else None
        )
        return {
            "intent": result.intent.value,
            "risk_level": result.risk_level.value,
            "intent_reason": result.reason,
            "final_status": final_status,
        }

    async def _retrieve_documents(self, state: WorkflowState) -> WorkflowUpdate:
        """검색 결과를 UUID까지 JSON 직렬화 가능한 Checkpoint 값으로 바꾼다."""
        chunks = await self._search_service.search(state["normalized_question"])
        serialized = [chunk.model_dump(mode="json") for chunk in chunks]
        return {
            "retrieved_chunks": serialized,
            "retrieval_attempts": state.get("retrieval_attempts", 0) + 1,
        }

    async def _generate_answer(self, state: WorkflowState) -> WorkflowUpdate:
        """근거 없음은 검토로 보내고 정상 출력은 검색 ID 포함 여부를 검사한다."""
        chunks = [SearchHit.model_validate(chunk) for chunk in state["retrieved_chunks"]]
        if not chunks:
            answer = AnswerOutput(
                answer="검색 근거를 찾지 못해 자동 답변을 중단했습니다.",
                citations=[],
                needs_review=True,
                review_reason="검색된 문서 Chunk가 없습니다.",
            )
        else:
            answer = await self._model_client.generate_answer(
                state["normalized_question"], chunks
            )
            self._validate_citations(answer, chunks)
        status = (
            JobStatus.REVIEW_REQUIRED
            if answer.needs_review
            else JobStatus.COMPLETED
        )
        return {
            "draft_answer": answer.model_dump(mode="json"),
            "generation_attempts": state.get("generation_attempts", 0) + 1,
            "final_status": status.value,
        }

    def _validate_citations(
        self, answer: AnswerOutput, chunks: list[SearchHit]
    ) -> None:
        """정상 답변이 이번 실행에서 검색한 문서·Chunk 쌍만 인용하게 강제한다."""
        if answer.needs_review:
            return
        allowed = {(chunk.document_id, chunk.chunk_id) for chunk in chunks}
        provided = {
            (citation.document_id, citation.chunk_id) for citation in answer.citations
        }
        if not provided or not provided.issubset(allowed):
            raise WorkflowOutputError("답변 인용이 이번 검색 결과와 일치하지 않습니다.")


def route_after_intent(
    state: WorkflowState,
) -> Literal["retrieve_documents", "__end__"]:
    """안전 등급이 낮은 질문만 검색 단계로 보내고 나머지는 검토 상태로 끝낸다."""
    if state.get("final_status") == JobStatus.REVIEW_REQUIRED.value:
        return END
    return "retrieve_documents"


def build_workflow_graph(
    nodes: WorkflowNodes,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """4단계 기본 노드와 PostgreSQL Checkpointer를 하나의 그래프로 컴파일한다."""
    builder = StateGraph(WorkflowState)
    builder.add_node("validate_input", nodes.validate_input)
    builder.add_node("classify_intent", nodes.classify_intent)
    builder.add_node("retrieve_documents", nodes.retrieve_documents)
    builder.add_node("generate_answer", nodes.generate_answer)
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "classify_intent")
    builder.add_conditional_edges("classify_intent", route_after_intent)
    builder.add_edge("retrieve_documents", "generate_answer")
    builder.add_edge("generate_answer", END)
    return builder.compile(checkpointer=checkpointer)
