from datetime import date

import pytest
from pydantic import ValidationError

from app.common.config import Settings
from app.workflow.jev import FallbackDocumentGradeClient
from app.workflow.model import OllamaWorkflowModelClient
from app.worker.main import build_document_grade_client


def build_local_model_client() -> OllamaWorkflowModelClient:
    """외부 요청 없이 판정기 선택만 검증할 로컬 모델 Client를 만든다."""
    return OllamaWorkflowModelClient(
        base_url="http://ollama.test",
        model="local-test",
        timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_jev_is_selected_during_confirmed_free_period() -> None:
    """명시적 Jev 설정과 유효한 무료 기간이면 로컬 폴백을 포함해 선택하는지 검증한다."""
    settings = Settings(
        workflow_document_grader="jev",
        ai_gateway_api_key="test-secret",
        jev_free_use_not_after=date(2026, 9, 25),
    )
    local_client = build_local_model_client()
    grade_client, jev_client = build_document_grade_client(
        settings,
        local_client,
        current_date=date(2026, 9, 21),
    )

    assert isinstance(grade_client, FallbackDocumentGradeClient)
    assert jev_client is not None
    await jev_client.aclose()
    await local_client.aclose()


@pytest.mark.asyncio
async def test_jev_is_blocked_after_confirmed_free_period() -> None:
    """무료 종료일 다음 날에는 Key가 있어도 Jev를 만들거나 호출하지 않는지 검증한다."""
    settings = Settings(
        workflow_document_grader="jev",
        ai_gateway_api_key="test-secret",
        jev_free_use_not_after=date(2026, 9, 25),
    )
    local_client = build_local_model_client()
    grade_client, jev_client = build_document_grade_client(
        settings,
        local_client,
        current_date=date(2026, 9, 26),
    )

    assert grade_client is local_client
    assert jev_client is None
    await local_client.aclose()


def test_jev_confirmed_free_period_cannot_be_extended_by_environment() -> None:
    """가격 재확인 없이 환경변수만으로 검증된 무료 종료일을 늦추지 못하게 한다."""
    with pytest.raises(ValidationError, match="jev_free_use_not_after"):
        Settings(jev_free_use_not_after=date(2026, 9, 26))
