from typing import cast
from uuid import UUID

from langgraph.graph.state import CompiledStateGraph

from app.common.domain import JobStatus
from app.messaging.messages import JobMessage
from app.workflow.repository import WorkflowRepository
from app.workflow.schemas import (
    AnswerOutput,
    WorkflowRequest,
    WorkflowResult,
    WorkflowState,
)


GraphConfig = dict[str, dict[str, str]]


class WorkflowRunner:
    """Checkpoint 존재 여부에 따라 새 실행과 중단된 실행 재개를 조정한다."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        repository: WorkflowRepository,
    ) -> None:
        """컴파일된 그래프와 작업 결과 저장소를 주입받는다."""
        self._graph = graph
        self._repository = repository

    async def run(self, request: WorkflowRequest) -> WorkflowResult:
        """같은 thread_id의 Checkpoint를 재사용해 완료 결과를 멱등하게 저장한다."""
        config = self._build_config(request.thread_id)
        snapshot = await self._graph.aget_state(config)
        if snapshot.values and not snapshot.next and snapshot.values.get("final_status"):
            state = cast(WorkflowState, snapshot.values)
        else:
            graph_input = None if snapshot.values else request.to_state()
            state = cast(
                WorkflowState,
                await self._graph.ainvoke(graph_input, config=config),
            )
        result = self._build_result(state)
        await self._repository.record_outcome(
            request.job_id,
            status=result.status,
            answer=result.answer,
        )
        return result

    def _build_config(self, thread_id: str) -> GraphConfig:
        """LangGraph Checkpoint 조회에 사용할 안정적인 실행 식별자를 만든다."""
        return {"configurable": {"thread_id": thread_id}}

    def _build_result(self, state: WorkflowState) -> WorkflowResult:
        """그래프의 느슨한 상태 값을 검증된 종료 결과로 변환한다."""
        raw_status = state.get("final_status")
        if raw_status is None:
            raise RuntimeError("Workflow가 종료 상태 없이 끝났습니다.")
        status = JobStatus(raw_status)
        raw_answer = state.get("draft_answer")
        answer = AnswerOutput.model_validate(raw_answer) if raw_answer is not None else None
        chunk_ids = [
            UUID(str(chunk["chunk_id"])) for chunk in state.get("retrieved_chunks", [])
        ]
        return WorkflowResult(
            job_id=UUID(state["job_id"]),
            thread_id=state["thread_id"],
            status=status,
            answer=answer,
            retrieved_chunk_ids=chunk_ids,
        )


class WorkflowJobHandler:
    """RabbitMQ 작업 메시지를 영속 작업 조회와 Workflow 실행에 연결한다."""

    def __init__(
        self,
        repository: WorkflowRepository,
        runner: WorkflowRunner,
    ) -> None:
        """작업 조회 저장소와 재개 가능한 Workflow Runner를 주입받는다."""
        self._repository = repository
        self._runner = runner

    async def __call__(self, message: JobMessage) -> None:
        """선점된 작업을 조회하고 job_id 기반 thread_id로 그래프를 실행한다."""
        job = await self._repository.get_job(message.job_id)
        if job is None:
            raise LookupError("WORKFLOW_JOB_NOT_FOUND")
        if job.status != JobStatus.PROCESSING:
            raise RuntimeError("WORKFLOW_JOB_NOT_PROCESSING")
        thread_id = message.thread_id or str(message.job_id)
        await self._runner.run(
            WorkflowRequest(
                job_id=job.job_id,
                thread_id=thread_id,
                question=job.question,
            )
        )
