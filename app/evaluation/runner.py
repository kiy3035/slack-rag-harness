import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import platform
from time import perf_counter
from collections.abc import Callable
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.config import Settings
from app.common.domain import JobStatus
from app.db.session import build_engine
from app.evaluation.checkpoint import (
    checkpoint_path,
    load_checkpoint,
    validate_checkpoint_context,
    write_checkpoint,
)
from app.evaluation.dataset import dataset_sha256, load_dataset
from app.evaluation.schemas import (
    CaseEvaluation,
    EvaluationCase,
    EvaluationCheckpoint,
    EvaluationChunk,
    EvaluationConfig,
    EvaluationEnvironment,
    EvaluationPrediction,
    EvaluationReport,
)
from app.evaluation.scoring import evaluate_case, summarize_cases
from app.retrieval.embedding import OllamaEmbeddingClient
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.schemas import SearchHit
from app.retrieval.service import KnowledgeSearchService
from app.workflow.graph import WorkflowNodes, build_workflow_graph
from app.workflow.model import OllamaWorkflowModelClient
from app.workflow.schemas import AnswerOutput, WorkflowRequest, WorkflowState


ProgressCallback = Callable[[int, int, str | None], None]


class LiveEvaluationHarness:
    """운영 테이블을 쓰지 않고 실제 검색과 Workflow 노드로 평가 Case를 실행한다."""

    def __init__(self, settings: Settings, config: EvaluationConfig) -> None:
        """로컬 DB·Ollama 설정과 한 번의 고정 실험 조건을 보관한다."""
        self._settings = settings
        self._config = config
        self._engine = build_engine(settings.database_url)
        session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        self._embedding_client = OllamaEmbeddingClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.ollama_timeout_seconds,
        )
        self._model_client = OllamaWorkflowModelClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_generation_model,
            timeout_seconds=settings.ollama_generation_timeout_seconds,
        )
        search_service = KnowledgeSearchService(
            repository=KnowledgeRepository(session_factory),
            embedding_client=self._embedding_client,
            embedding_dimensions=settings.embedding_dimensions,
            top_k=config.top_k,
            min_score=config.min_score,
            max_chunks_per_document=config.max_chunks_per_document,
        )
        nodes = WorkflowNodes(
            search_service=search_service,
            model_client=self._model_client,
            document_grade_client=self._model_client,
            question_max_chars=settings.workflow_question_max_chars,
            max_query_rewrites=config.max_query_rewrites,
            max_generation_attempts=settings.workflow_max_generation_attempts,
        )
        self._graph = build_workflow_graph(nodes, InMemorySaver())

    async def run_cases(
        self,
        cases: list[EvaluationCase],
        run_id: str,
        *,
        existing_predictions: list[EvaluationPrediction] | None = None,
        on_progress: Callable[[list[EvaluationPrediction], str], None] | None = None,
    ) -> list[EvaluationPrediction]:
        """완료 Case를 건너뛰고 새 결과를 저장하면서 입력 순서로 반환한다."""
        semaphore = asyncio.Semaphore(self._config.worker_count)
        result_by_case_id = {
            prediction.case_id: prediction
            for prediction in existing_predictions or []
        }
        progress_lock = asyncio.Lock()

        async def run_bounded(case: EvaluationCase) -> EvaluationPrediction:
            """동시성 한도 안에서 Case를 실행하고 완료 스냅샷을 직렬 저장한다."""
            async with semaphore:
                prediction = await self._run_case(case, run_id)
            async with progress_lock:
                result_by_case_id[case.case_id] = prediction
                if on_progress is not None:
                    snapshot = [
                        result_by_case_id[item.case_id]
                        for item in cases
                        if item.case_id in result_by_case_id
                    ]
                    on_progress(snapshot, case.case_id)
            return prediction

        pending_cases = [
            case for case in cases if case.case_id not in result_by_case_id
        ]
        await asyncio.gather(*(run_bounded(case) for case in pending_cases))
        return [result_by_case_id[case.case_id] for case in cases]

    async def _run_case(
        self,
        case: EvaluationCase,
        run_id: str,
    ) -> EvaluationPrediction:
        """Case별 고유 Thread로 실제 그래프를 실행하고 안전한 오류 코드까지 보존한다."""
        started_at = perf_counter()
        try:
            request = WorkflowRequest(
                job_id=uuid5(NAMESPACE_URL, f"{run_id}:{case.case_id}"),
                thread_id=f"evaluation:{run_id}:{case.case_id}",
                question=case.question,
                workflow_revision=0,
            )
            state = cast(
                WorkflowState,
                await self._graph.ainvoke(
                    request.to_state(),
                    config={"configurable": {"thread_id": request.thread_id}},
                ),
            )
            return self._to_prediction(
                case,
                state,
                latency_ms=(perf_counter() - started_at) * 1_000,
            )
        except Exception as error:
            return EvaluationPrediction(
                case_id=case.case_id,
                review_required=True,
                latency_ms=(perf_counter() - started_at) * 1_000,
                error_code=type(error).__name__,
            )

    def _to_prediction(
        self,
        case: EvaluationCase,
        state: WorkflowState,
        *,
        latency_ms: float,
    ) -> EvaluationPrediction:
        """직렬화 가능한 Workflow 종료 상태를 평가 예측 Schema로 축약한다."""
        hits = [
            SearchHit.model_validate(item) for item in state.get("retrieved_chunks", [])
        ]
        raw_answer = state.get("draft_answer")
        answer = AnswerOutput.model_validate(raw_answer) if raw_answer is not None else None
        return EvaluationPrediction(
            case_id=case.case_id,
            retrieved_chunks=[
                EvaluationChunk(
                    chunk_id=str(hit.chunk_id),
                    document_path=hit.source_path,
                    content=hit.content,
                    score=hit.score,
                )
                for hit in hits
            ],
            cited_chunk_ids=(
                [str(citation.chunk_id) for citation in answer.citations]
                if answer is not None
                else []
            ),
            answer=answer.answer if answer is not None else None,
            review_required=(
                state.get("final_status") != JobStatus.COMPLETED.value
            ),
            review_reason_code=state.get("review_reason_code"),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        """평가가 소유한 Ollama HTTP Client와 DB 연결 풀을 모두 닫는다."""
        await self._embedding_client.aclose()
        await self._model_client.aclose()
        await self._engine.dispose()


def create_run_id(config: EvaluationConfig) -> str:
    """시각과 핵심 조건을 포함해 충돌 가능성이 낮은 평가 실행 ID를 만든다."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    score = str(config.min_score).replace("-", "m").replace(".", "p")
    return (
        f"eval-{timestamp}-k{config.top_k}-s{score}-"
        f"r{config.max_query_rewrites}-w{config.worker_count}-{uuid4().hex[:6]}"
    )


def _environment(settings: Settings) -> EvaluationEnvironment:
    """실제 측정 결과와 함께 저장할 운영체제·CPU·모델 정보를 수집한다."""
    return EvaluationEnvironment(
        platform=platform.platform(),
        machine=platform.machine(),
        cpu_count=os.cpu_count(),
        generation_model=settings.ollama_generation_model,
        embedding_model=settings.ollama_embedding_model,
    )


async def run_live_evaluation(
    dataset_path: Path,
    settings: Settings,
    config: EvaluationConfig,
    *,
    output_directory: Path,
    run_id: str,
    progress_callback: ProgressCallback | None = None,
) -> EvaluationReport:
    """고정 데이터셋을 Case별 저장·재개하며 실행하고 최종 리포트를 반환한다."""
    cases = load_dataset(dataset_path)
    current_dataset_hash = dataset_sha256(dataset_path)
    current_environment = _environment(settings)
    current_checkpoint_path = checkpoint_path(output_directory, run_id)
    if current_checkpoint_path.exists():
        checkpoint = load_checkpoint(current_checkpoint_path)
        validate_checkpoint_context(
            checkpoint,
            dataset_hash=current_dataset_hash,
            config=config,
            environment=current_environment,
            cases=cases,
        )
        existing_predictions = checkpoint.predictions
        previous_duration_ms = checkpoint.cumulative_duration_ms
    else:
        existing_predictions = []
        previous_duration_ms = 0.0
        checkpoint = EvaluationCheckpoint(
            run_id=run_id,
            dataset_path=dataset_path.as_posix(),
            dataset_sha256=current_dataset_hash,
            config=config,
            environment=current_environment,
            cumulative_duration_ms=0.0,
        )
        write_checkpoint(checkpoint, current_checkpoint_path)
    if progress_callback is not None:
        progress_callback(len(existing_predictions), len(cases), None)

    harness = LiveEvaluationHarness(settings, config)
    started_at = perf_counter()

    def persist_progress(
        predictions: list[EvaluationPrediction],
        completed_case_id: str,
    ) -> None:
        """새 Case 결과와 누적 활성 실행시간을 원자적으로 저장하고 진행률을 알린다."""
        elapsed_ms = (perf_counter() - started_at) * 1_000
        write_checkpoint(
            EvaluationCheckpoint(
                run_id=run_id,
                dataset_path=dataset_path.as_posix(),
                dataset_sha256=current_dataset_hash,
                config=config,
                environment=current_environment,
                cumulative_duration_ms=previous_duration_ms + elapsed_ms,
                predictions=predictions,
            ),
            current_checkpoint_path,
        )
        if progress_callback is not None:
            progress_callback(len(predictions), len(cases), completed_case_id)

    try:
        predictions = await harness.run_cases(
            cases,
            run_id,
            existing_predictions=existing_predictions,
            on_progress=persist_progress,
        )
    finally:
        await harness.aclose()
    duration_ms = previous_duration_ms + (perf_counter() - started_at) * 1_000
    write_checkpoint(
        EvaluationCheckpoint(
            run_id=run_id,
            dataset_path=dataset_path.as_posix(),
            dataset_sha256=current_dataset_hash,
            config=config,
            environment=current_environment,
            cumulative_duration_ms=duration_ms,
            predictions=predictions,
        ),
        current_checkpoint_path,
    )
    evaluations: list[CaseEvaluation] = [
        evaluate_case(case, prediction)
        for case, prediction in zip(cases, predictions, strict=True)
    ]
    return EvaluationReport(
        run_id=run_id,
        measured_at=datetime.now(UTC).isoformat(),
        dataset_path=dataset_path.as_posix(),
        dataset_sha256=current_dataset_hash,
        config=config,
        environment=current_environment,
        duration_ms=duration_ms,
        summary=summarize_cases(evaluations, duration_ms=duration_ms),
        cases=evaluations,
    )
