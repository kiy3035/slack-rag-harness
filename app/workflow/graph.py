from collections.abc import Awaitable, Callable
import logging
import re
from time import perf_counter
from typing import Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.common.domain import JobStatus
from app.retrieval.schemas import SearchHit
from app.workflow.model import (
    DocumentGradeClient,
    WorkflowModelClient,
    WorkflowModelResponseError,
)
from app.workflow.schemas import (
    AnswerOutput,
    ReviewReasonCode,
    RiskLevel,
    WorkflowState,
    WorkflowUpdate,
)


class SearchService(Protocol):
    """실제 pgvector 검색과 테스트 Fake가 공유하는 검색 경계다."""

    async def search(self, question: str) -> list[SearchHit]:
        """현재 검색어에 대한 문서 Chunk를 점수와 함께 반환한다."""
        ...


NodeOperation = Callable[[WorkflowState], Awaitable[WorkflowUpdate]]


class AnswerValidator:
    """모델 신뢰도와 독립적으로 출력·인용·민감 패턴을 코드로 검증한다."""

    _sensitive_patterns = (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+\b"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\b\d{6}-[1-4]\d{6}\b"),
    )

    def validate(
        self,
        answer: AnswerOutput,
        chunks: list[SearchHit],
    ) -> list[ReviewReasonCode]:
        """답변의 검토 요청과 인용 허용목록·민감 패턴 위반을 반환한다."""
        errors: list[ReviewReasonCode] = []
        if answer.needs_review:
            errors.append(ReviewReasonCode.MODEL_REVIEW_REQUIRED)

        allowed = {(chunk.document_id, chunk.chunk_id) for chunk in chunks}
        provided = {
            (citation.document_id, citation.chunk_id) for citation in answer.citations
        }
        if not answer.needs_review and (
            not provided or not provided.issubset(allowed)
        ):
            errors.append(ReviewReasonCode.CITATION_INVALID)

        if any(pattern.search(answer.answer) for pattern in self._sensitive_patterns):
            errors.append(ReviewReasonCode.SENSITIVE_OUTPUT)
        return list(dict.fromkeys(errors))


class RelevanceValidator:
    """LLM 관련성 판정 뒤 질문 핵심어가 문서에 실제 존재하는지 보수적으로 확인한다."""

    _generic_terms = {
        "관련",
        "대한",
        "방법",
        "무엇",
        "어떻게",
        "알려줘",
        "알려주세요",
        "있나요",
        "절차",
        "정책",
        "주세요",
        "하나요",
    }
    _particles = frozenset("은는이가을를에의와과도로")

    def filter(self, question: str, chunks: list[SearchHit]) -> list[SearchHit]:
        """질문의 비일반 핵심어가 충분히 겹치는 Chunk만 관련 근거로 남긴다."""
        keywords = self._keywords(question)
        if not keywords:
            return []
        required_matches = min(2, len(keywords))
        return [
            chunk
            for chunk in chunks
            if self._match_count(keywords, chunk) >= required_matches
        ]

    def _keywords(self, question: str) -> set[str]:
        """한글 조사와 일반 질문 표현을 제거해 결정적인 비교어를 만든다."""
        raw_tokens = re.findall(r"[0-9A-Za-z가-힣]+", question.lower())
        normalized = {self._strip_particle(token) for token in raw_tokens}
        return {
            token
            for token in normalized
            if len(token) >= 2 and token not in self._generic_terms
        }

    def _strip_particle(self, token: str) -> str:
        """두 글자보다 긴 한국어 토큰 끝의 한 글자 조사를 제거한다."""
        if len(token) > 2 and token[-1] in self._particles:
            return token[:-1]
        return token

    def _match_count(self, keywords: set[str], chunk: SearchHit) -> int:
        """제목·소제목·본문에 포함된 서로 다른 질문 핵심어 수를 센다."""
        searchable = " ".join(
            [chunk.title, chunk.heading or "", chunk.content]
        ).lower()
        return sum(keyword in searchable for keyword in keywords)


class WorkflowNodes:
    """검색 관련성·제한 재시도·출력 검증을 포함한 Workflow 노드를 제공한다."""

    def __init__(
        self,
        search_service: SearchService,
        model_client: WorkflowModelClient,
        question_max_chars: int,
        max_query_rewrites: int = 1,
        max_generation_attempts: int = 2,
        document_grade_client: DocumentGradeClient | None = None,
        answer_validator: AnswerValidator | None = None,
        relevance_validator: RelevanceValidator | None = None,
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        """외부 경계와 재시도 상한, 교체 가능한 판정기·검증기·시계를 주입한다."""
        self._search_service = search_service
        self._model_client = model_client
        self._question_max_chars = question_max_chars
        self._max_query_rewrites = max_query_rewrites
        self._max_generation_attempts = max_generation_attempts
        self._document_grade_client = document_grade_client or model_client
        self._answer_validator = answer_validator or AnswerValidator()
        self._relevance_validator = relevance_validator or RelevanceValidator()
        self._clock = monotonic_clock
        self._logger = logging.getLogger(__name__)

    async def validate_input(self, state: WorkflowState) -> WorkflowUpdate:
        """질문을 정규화하고 비어 있거나 과도한 입력을 영구 거부한다."""
        return await self._run_node("validate_input", state, self._validate_input)

    async def classify_intent(self, state: WorkflowState) -> WorkflowUpdate:
        """질문의 의도와 실제 실행 위험을 구조화해 검토 분기를 결정한다."""
        return await self._run_node("classify_intent", state, self._classify_intent)

    async def retrieve_documents(self, state: WorkflowState) -> WorkflowUpdate:
        """원 질문 또는 제한적으로 재작성한 검색어로 현재 Chunk를 검색한다."""
        return await self._run_node(
            "retrieve_documents", state, self._retrieve_documents
        )

    async def grade_documents(self, state: WorkflowState) -> WorkflowUpdate:
        """검색 점수와 별도로 질문을 직접 뒷받침하는 Chunk를 판정한다."""
        return await self._run_node("grade_documents", state, self._grade_documents)

    async def rewrite_query(self, state: WorkflowState) -> WorkflowUpdate:
        """관련 근거가 부족할 때 원 의미를 유지한 검색어를 제한 횟수로 만든다."""
        return await self._run_node("rewrite_query", state, self._rewrite_query)

    async def generate_answer(self, state: WorkflowState) -> WorkflowUpdate:
        """관련성이 통과된 Chunk만 제공해 구조화 답변 초안을 생성한다."""
        return await self._run_node("generate_answer", state, self._generate_answer)

    async def validate_answer(self, state: WorkflowState) -> WorkflowUpdate:
        """Schema·인용·민감 패턴을 검증하고 재생성 또는 검토 전환한다."""
        return await self._run_node("validate_answer", state, self._validate_answer)

    def route_after_grade(
        self, state: WorkflowState
    ) -> Literal["rewrite_query", "generate_answer", "__end__"]:
        """관련성 통과·재작성 가능·검토 전환 중 다음 경로를 선택한다."""
        if state.get("final_status") == JobStatus.REVIEW_REQUIRED.value:
            return END
        if state.get("relevance_passed"):
            return "generate_answer"
        return "rewrite_query"

    def route_after_validation(
        self, state: WorkflowState
    ) -> Literal["generate_answer", "__end__"]:
        """검증 실패 횟수가 남았을 때만 답변을 다시 생성한다."""
        if state.get("final_status") in {
            JobStatus.COMPLETED.value,
            JobStatus.REVIEW_REQUIRED.value,
        }:
            return END
        return "generate_answer"

    def route_after_rewrite(
        self, state: WorkflowState
    ) -> Literal["retrieve_documents", "__end__"]:
        """재작성 출력 계약이 깨졌으면 추가 검색 없이 검토 상태로 끝낸다."""
        if state.get("final_status") == JobStatus.REVIEW_REQUIRED.value:
            return END
        return "retrieve_documents"

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
        return {"normalized_question": normalized, "search_query": normalized}

    async def _classify_intent(self, state: WorkflowState) -> WorkflowUpdate:
        """낮은 위험이 아닌 질문을 생성 전에 사람 검토 대상으로 표시한다."""
        result = await self._model_client.classify_intent(state["normalized_question"])
        update: WorkflowUpdate = {
            "intent": result.intent.value,
            "risk_level": result.risk_level.value,
            "intent_reason": result.reason,
        }
        if result.risk_level != RiskLevel.LOW:
            update.update(
                final_status=JobStatus.REVIEW_REQUIRED.value,
                review_reason_code=ReviewReasonCode.SENSITIVE_INTENT.value,
            )
        return update

    async def _retrieve_documents(self, state: WorkflowState) -> WorkflowUpdate:
        """현재 검색어의 결과를 UUID까지 JSON 직렬화 가능한 상태로 바꾼다."""
        query = state.get("search_query", state["normalized_question"])
        chunks = await self._search_service.search(query)
        return {
            "retrieved_chunks": [chunk.model_dump(mode="json") for chunk in chunks],
            "retrieval_attempts": state.get("retrieval_attempts", 0) + 1,
        }

    async def _grade_documents(self, state: WorkflowState) -> WorkflowUpdate:
        """관련 ID를 검색 집합으로 제한하고 충돌·재작성 소진을 검토로 보낸다."""
        chunks = [SearchHit.model_validate(chunk) for chunk in state["retrieved_chunks"]]
        if not chunks:
            passed = False
            relevant: list[SearchHit] = []
            reason = "검색된 문서 Chunk가 없습니다."
            conflict = False
            grade_invalid = False
        else:
            try:
                grade = await self._document_grade_client.grade_documents(
                    state["normalized_question"], chunks
                )
            except WorkflowModelResponseError:
                return {
                    "relevant_chunks": [],
                    "relevance_passed": False,
                    "conflict_detected": False,
                    "final_status": JobStatus.REVIEW_REQUIRED.value,
                    "review_reason_code": (
                        ReviewReasonCode.RELEVANCE_OUTPUT_INVALID.value
                    ),
                    "validation_errors": [
                        ReviewReasonCode.RELEVANCE_OUTPUT_INVALID.value
                    ],
                }
            allowed_ids = {chunk.chunk_id for chunk in chunks}
            provided_ids = set(grade.relevant_chunk_ids)
            grade_invalid = not provided_ids.issubset(allowed_ids)
            model_relevant = [
                chunk for chunk in chunks if chunk.chunk_id in provided_ids
            ]
            relevant = self._relevance_validator.filter(
                state["normalized_question"], model_relevant
            )
            conflict = grade.conflict_detected
            passed = bool(relevant) and not conflict and not grade_invalid
            reason = (
                grade.reason
                if relevant or not model_relevant
                else "LLM 관련성 판정이 질문 핵심어의 문서 근거를 충족하지 못했습니다."
            )

        update: WorkflowUpdate = {
            "relevant_chunks": [chunk.model_dump(mode="json") for chunk in relevant],
            "relevance_reason": reason,
            "relevance_passed": passed,
            "conflict_detected": conflict,
        }
        if conflict:
            update.update(
                final_status=JobStatus.REVIEW_REQUIRED.value,
                review_reason_code=ReviewReasonCode.DOCUMENT_CONFLICT.value,
            )
        elif grade_invalid:
            update.update(
                final_status=JobStatus.REVIEW_REQUIRED.value,
                review_reason_code=ReviewReasonCode.RELEVANCE_OUTPUT_INVALID.value,
            )
        elif not passed and (
            state.get("query_rewrite_attempts", 0) >= self._max_query_rewrites
        ):
            update.update(
                final_status=JobStatus.REVIEW_REQUIRED.value,
                review_reason_code=ReviewReasonCode.INSUFFICIENT_EVIDENCE.value,
            )
        return update

    async def _rewrite_query(self, state: WorkflowState) -> WorkflowUpdate:
        """검색 부족 사유를 포함해 재작성 횟수를 정확히 한 번 증가시킨다."""
        try:
            result = await self._model_client.rewrite_query(
                state["normalized_question"],
                state.get("relevance_reason", "관련 문서가 부족합니다."),
            )
        except WorkflowModelResponseError:
            return {
                "final_status": JobStatus.REVIEW_REQUIRED.value,
                "review_reason_code": ReviewReasonCode.OUTPUT_SCHEMA_INVALID.value,
                "validation_errors": [ReviewReasonCode.OUTPUT_SCHEMA_INVALID.value],
            }
        return {
            "search_query": result.query,
            "query_rewrite_attempts": state.get("query_rewrite_attempts", 0) + 1,
        }

    async def _generate_answer(self, state: WorkflowState) -> WorkflowUpdate:
        """구조화 출력 오류도 횟수에 포함해 무한 재생성을 막는다."""
        chunks = [
            SearchHit.model_validate(chunk)
            for chunk in state.get("relevant_chunks", state["retrieved_chunks"])
        ]
        attempts = state.get("generation_attempts", 0) + 1
        try:
            answer = await self._model_client.generate_answer(
                state["normalized_question"], chunks
            )
        except WorkflowModelResponseError:
            return {
                "generation_attempts": attempts,
                "schema_valid": False,
                "citations_valid": False,
                "validation_errors": [ReviewReasonCode.OUTPUT_SCHEMA_INVALID.value],
            }
        return {
            "draft_answer": answer.model_dump(mode="json"),
            "generation_attempts": attempts,
            "schema_valid": True,
            "validation_errors": [],
        }

    async def _validate_answer(self, state: WorkflowState) -> WorkflowUpdate:
        """현재 초안을 검증하고 성공·재생성·검토 상태 중 하나를 명시한다."""
        raw_answer = state.get("draft_answer")
        chunks = [
            SearchHit.model_validate(chunk)
            for chunk in state.get("relevant_chunks", state["retrieved_chunks"])
        ]
        if raw_answer is None or not state.get("schema_valid", False):
            errors = [ReviewReasonCode.OUTPUT_SCHEMA_INVALID]
            citations_valid = False
        else:
            answer = AnswerOutput.model_validate(raw_answer)
            errors = self._answer_validator.validate(answer, chunks)
            citations_valid = ReviewReasonCode.CITATION_INVALID not in errors

        update: WorkflowUpdate = {
            "schema_valid": raw_answer is not None and state.get("schema_valid", False),
            "citations_valid": citations_valid,
            "validation_errors": [error.value for error in errors],
        }
        if not errors:
            update["final_status"] = JobStatus.COMPLETED.value
            return update

        attempts = state.get("generation_attempts", 0)
        must_review = (
            ReviewReasonCode.MODEL_REVIEW_REQUIRED in errors
            or attempts >= self._max_generation_attempts
        )
        if must_review:
            update.update(
                final_status=JobStatus.REVIEW_REQUIRED.value,
                review_reason_code=errors[0].value,
            )
        return update


def route_after_intent(
    state: WorkflowState,
) -> Literal["retrieve_documents", "__end__"]:
    """안전 등급이 낮은 질문만 검색 단계로 보내고 나머지는 검토로 끝낸다."""
    if state.get("final_status") == JobStatus.REVIEW_REQUIRED.value:
        return END
    return "retrieve_documents"


def build_workflow_graph(
    nodes: WorkflowNodes,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """5단계 검색 판정·제한 루프·출력 검증 그래프를 Checkpointer와 컴파일한다."""
    builder = StateGraph(WorkflowState)
    builder.add_node("validate_input", nodes.validate_input)
    builder.add_node("classify_intent", nodes.classify_intent)
    builder.add_node("retrieve_documents", nodes.retrieve_documents)
    builder.add_node("grade_documents", nodes.grade_documents)
    builder.add_node("rewrite_query", nodes.rewrite_query)
    builder.add_node("generate_answer", nodes.generate_answer)
    builder.add_node("validate_answer", nodes.validate_answer)
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "classify_intent")
    builder.add_conditional_edges("classify_intent", route_after_intent)
    builder.add_edge("retrieve_documents", "grade_documents")
    builder.add_conditional_edges("grade_documents", nodes.route_after_grade)
    builder.add_conditional_edges("rewrite_query", nodes.route_after_rewrite)
    builder.add_edge("generate_answer", "validate_answer")
    builder.add_conditional_edges("validate_answer", nodes.route_after_validation)
    return builder.compile(checkpointer=checkpointer)
