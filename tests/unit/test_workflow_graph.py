import logging
from uuid import UUID

import pytest

from app.common.domain import JobStatus
from app.retrieval.schemas import SearchHit
from app.workflow.graph import WorkflowNodes, WorkflowOutputError
from app.workflow.schemas import (
    AnswerOutput,
    CitationOutput,
    IntentCategory,
    IntentOutput,
    RiskLevel,
    WorkflowState,
)


DOCUMENT_ID = UUID("33333333-3333-3333-3333-333333333333")
CHUNK_ID = UUID("44444444-4444-4444-4444-444444444444")


class StaticSearchService:
    """한 개의 고정 검색 근거를 반환하는 단위 테스트 대역이다."""

    async def search(self, question: str) -> list[SearchHit]:
        """검색 질문을 확인하고 고정 Chunk를 반환한다."""
        assert question == "정산 절차는?"
        return [build_search_hit()]


class EmptySearchService:
    """근거 없는 질문의 안전한 검토 전환을 재현하는 검색 대역이다."""

    async def search(self, question: str) -> list[SearchHit]:
        """검색 결과가 없도록 빈 목록을 반환한다."""
        return []


class StaticModelClient:
    """결정적인 의도와 인용 답변을 반환하는 생성 모델 대역이다."""

    def __init__(self, citation_chunk_id: UUID = CHUNK_ID) -> None:
        """조작된 인용 실패도 재현할 수 있도록 Chunk ID를 주입받는다."""
        self._citation_chunk_id = citation_chunk_id

    async def classify_intent(self, question: str) -> IntentOutput:
        """일반 운영 절차 질문을 낮은 위험으로 분류한다."""
        return IntentOutput(
            intent=IntentCategory.PROCEDURE,
            risk_level=RiskLevel.LOW,
            reason="문서 절차 질문",
        )

    async def generate_answer(
        self, question: str, chunks: list[SearchHit]
    ) -> AnswerOutput:
        """검색 문서와 주입된 Chunk 식별자를 인용하는 답변을 반환한다."""
        return AnswerOutput(
            answer="승인 후 처리합니다.",
            citations=[
                CitationOutput(
                    document_id=chunks[0].document_id,
                    chunk_id=self._citation_chunk_id,
                )
            ],
        )


def build_search_hit() -> SearchHit:
    """노드 단위 테스트에 사용할 직렬화 가능한 검색 결과를 만든다."""
    return SearchHit(
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        source_path="knowledge/manuals/test.md",
        title="정산 절차",
        document_version=1,
        chunk_index=0,
        heading="마감",
        content="승인 후 처리한다.",
        score=0.95,
    )


def initial_state() -> WorkflowState:
    """기본 Workflow 노드가 공유할 최소 입력 상태를 만든다."""
    return {
        "job_id": "55555555-5555-5555-5555-555555555555",
        "thread_id": "test-thread",
        "question": "  정산   절차는?  ",
        "retrieved_chunks": [],
        "retrieval_attempts": 0,
        "generation_attempts": 0,
        "validation_errors": [],
    }


@pytest.mark.asyncio
async def test_workflow_nodes_complete_with_retrieved_citation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """정상 질문이 실제 검색 ID를 인용한 완료 상태와 노드 로그를 만드는지 확인한다."""
    clock_values = iter((1.0, 1.1, 2.0, 2.2, 3.0, 3.3, 4.0, 4.4))

    def monotonic_clock() -> float:
        """노드별 수행 시간 로그를 결정적으로 계산할 시각을 반환한다."""
        return next(clock_values)

    nodes = WorkflowNodes(
        search_service=StaticSearchService(),
        model_client=StaticModelClient(),
        question_max_chars=100,
        monotonic_clock=monotonic_clock,
    )
    state = initial_state()
    caplog.set_level(logging.INFO)
    state.update(await nodes.validate_input(state))
    state.update(await nodes.classify_intent(state))
    state.update(await nodes.retrieve_documents(state))
    state.update(await nodes.generate_answer(state))

    answer = AnswerOutput.model_validate(state["draft_answer"])
    assert state["final_status"] == JobStatus.COMPLETED.value
    assert answer.citations[0].chunk_id == CHUNK_ID
    assert "workflow_node_started node=validate_input" in caplog.text
    assert "workflow_node_completed node=generate_answer" in caplog.text


@pytest.mark.asyncio
async def test_workflow_rejects_citation_outside_current_search(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """생성 모델이 검색되지 않은 Chunk ID를 인용하면 완료 상태가 되지 않는지 확인한다."""
    nodes = WorkflowNodes(
        search_service=StaticSearchService(),
        model_client=StaticModelClient(
            citation_chunk_id=UUID("66666666-6666-6666-6666-666666666666")
        ),
        question_max_chars=100,
    )
    state = initial_state()
    state.update(await nodes.validate_input(state))
    state.update(await nodes.classify_intent(state))
    state.update(await nodes.retrieve_documents(state))
    caplog.set_level(logging.ERROR)

    with pytest.raises(WorkflowOutputError, match="검색 결과"):
        await nodes.generate_answer(state)
    assert "workflow_node_failed node=generate_answer" in caplog.text


@pytest.mark.asyncio
async def test_workflow_marks_missing_evidence_for_review() -> None:
    """검색 근거가 없으면 모델로 절차를 만들지 않고 검토 상태로 끝나는지 확인한다."""
    nodes = WorkflowNodes(
        search_service=EmptySearchService(),
        model_client=StaticModelClient(),
        question_max_chars=100,
    )
    state = initial_state()
    state.update(await nodes.validate_input(state))
    state.update(await nodes.classify_intent(state))
    state.update(await nodes.retrieve_documents(state))
    state.update(await nodes.generate_answer(state))

    answer = AnswerOutput.model_validate(state["draft_answer"])
    assert state["final_status"] == JobStatus.REVIEW_REQUIRED.value
    assert answer.needs_review is True
    assert answer.citations == []


@pytest.mark.asyncio
async def test_workflow_rejects_empty_and_oversized_questions() -> None:
    """공백 질문과 설정 한도를 넘긴 질문이 모델 호출 전에 거부되는지 확인한다."""
    nodes = WorkflowNodes(
        search_service=StaticSearchService(),
        model_client=StaticModelClient(),
        question_max_chars=10,
    )
    empty = initial_state()
    empty["question"] = "   "
    oversized = initial_state()
    oversized["question"] = "가" * 11

    with pytest.raises(ValueError, match="비어"):
        await nodes.validate_input(empty)
    with pytest.raises(ValueError, match="초과"):
        await nodes.validate_input(oversized)
