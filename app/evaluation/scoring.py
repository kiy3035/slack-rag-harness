import math
import re

from app.evaluation.schemas import (
    CaseEvaluation,
    CategorySummary,
    EvaluationCase,
    EvaluationCategory,
    EvaluationPrediction,
    MetricSummary,
)


_GENERIC_TOKENS = {
    "그리고",
    "그러나",
    "관련",
    "대한",
    "따라",
    "합니다",
    "됩니다",
    "있습니다",
    "없습니다",
    "해야",
    "경우",
    "위해",
}
_NON_CLAIM_MARKERS = (
    "확인할 수 없습니다",
    "근거가 부족",
    "사람 검토",
    "담당자에게 문의",
    "답변할 수 없습니다",
)


def _tokens(text: str) -> set[str]:
    """근거 겹침 계산에 쓸 의미 있는 한글·영문·숫자 토큰을 추출한다."""
    return {
        token
        for token in re.findall(r"[0-9A-Za-z가-힣]+", text.lower())
        if len(token) >= 2 and token not in _GENERIC_TOKENS
    }


def extract_claims(answer: str) -> list[str]:
    """답변을 보수적인 문장 단위로 나누고 비주장 안내 문구를 제외한다."""
    candidates = re.split(r"(?<=[.!?])\s+|\n+", answer)
    claims: list[str] = []
    for candidate in candidates:
        normalized = re.sub(r"^[\s#>*\-\d.)]+", "", candidate).strip()
        if len(_tokens(normalized)) < 2:
            continue
        if any(marker in normalized for marker in _NON_CLAIM_MARKERS):
            continue
        claims.append(normalized)
    return claims


def find_unsupported_claims(
    answer: str | None,
    evidence_contents: list[str],
) -> tuple[list[str], int]:
    """인용 Chunk와 핵심어 겹침이 부족한 사실 문장을 보수적으로 표시한다."""
    if not answer:
        return [], 0
    claims = extract_claims(answer)
    if not evidence_contents:
        return claims, len(claims)
    evidence_tokens = [_tokens(content) for content in evidence_contents]
    unsupported: list[str] = []
    for claim in claims:
        claim_tokens = _tokens(claim)
        required = max(2, math.ceil(len(claim_tokens) * 0.35))
        if not any(len(claim_tokens & tokens) >= required for tokens in evidence_tokens):
            unsupported.append(claim)
    return unsupported, len(claims)


def evaluate_case(
    case: EvaluationCase,
    prediction: EvaluationPrediction,
) -> CaseEvaluation:
    """검색 회수·인용 유효성·검토 전환·근거 문장을 질문 한 건에서 계산한다."""
    if case.case_id != prediction.case_id:
        raise ValueError("평가 질문과 예측의 case_id가 일치하지 않습니다.")
    expected_paths = set(case.expected_document_paths)
    retrieved_paths = {chunk.document_path for chunk in prediction.retrieved_chunks}
    recall = (
        len(expected_paths & retrieved_paths) / len(expected_paths)
        if expected_paths
        else None
    )
    chunk_by_id = {chunk.chunk_id: chunk for chunk in prediction.retrieved_chunks}
    citation_correct = sum(
        chunk_id in chunk_by_id
        and chunk_by_id[chunk_id].document_path in expected_paths
        for chunk_id in prediction.cited_chunk_ids
    )
    evidence_contents = [
        chunk_by_id[chunk_id].content
        for chunk_id in prediction.cited_chunk_ids
        if chunk_id in chunk_by_id
    ]
    unsupported, claim_count = find_unsupported_claims(
        prediction.answer,
        evidence_contents,
    )
    return CaseEvaluation(
        case_id=case.case_id,
        category=case.category,
        retrieval_recall_at_k=recall,
        citation_correct=citation_correct,
        citation_total=len(prediction.cited_chunk_ids),
        review_correct=case.expected_review == prediction.review_required,
        unsupported_claims=unsupported,
        claim_count=claim_count,
        prediction=prediction,
    )


def percentile_95(values: list[float]) -> float:
    """보간 없이 관측값을 보존하는 nearest-rank 방식으로 p95를 계산한다."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * 0.95))
    return ordered[rank - 1]


def summarize_cases(
    evaluations: list[CaseEvaluation],
    *,
    duration_ms: float,
) -> MetricSummary:
    """개별 결과를 명시된 분모 규칙으로 전체 품질·성능 지표에 집계한다."""
    if not evaluations:
        raise ValueError("집계할 평가 결과가 없습니다.")
    recalls = [
        item.retrieval_recall_at_k
        for item in evaluations
        if item.retrieval_recall_at_k is not None
    ]
    citation_total = sum(item.citation_total for item in evaluations)
    citation_correct = sum(item.citation_correct for item in evaluations)
    claim_total = sum(item.claim_count for item in evaluations)
    unsupported_total = sum(len(item.unsupported_claims) for item in evaluations)
    duration_seconds = duration_ms / 1_000
    return MetricSummary(
        case_count=len(evaluations),
        retrieval_recall_at_k=(sum(recalls) / len(recalls) if recalls else None),
        citation_accuracy=(
            citation_correct / citation_total if citation_total else None
        ),
        review_transition_accuracy=(
            sum(item.review_correct for item in evaluations) / len(evaluations)
        ),
        unsupported_claim_rate=(
            unsupported_total / claim_total if claim_total else None
        ),
        throughput_cases_per_second=(
            len(evaluations) / duration_seconds if duration_seconds > 0 else 0.0
        ),
        p95_latency_ms=percentile_95(
            [item.prediction.latency_ms for item in evaluations]
        ),
        failed_cases=sum(item.prediction.error_code is not None for item in evaluations),
    )


def summarize_categories(
    evaluations: list[CaseEvaluation],
) -> list[CategorySummary]:
    """전체 지표와 같은 분모 규칙을 질문 유형별로 다시 계산한다."""
    summaries: list[CategorySummary] = []
    for category in EvaluationCategory:
        selected = [item for item in evaluations if item.category == category]
        if not selected:
            continue
        summary = summarize_cases(selected, duration_ms=0.0)
        summaries.append(
            CategorySummary(
                category=category,
                case_count=summary.case_count,
                retrieval_recall_at_k=summary.retrieval_recall_at_k,
                citation_accuracy=summary.citation_accuracy,
                review_transition_accuracy=summary.review_transition_accuracy,
                unsupported_claim_rate=summary.unsupported_claim_rate,
            )
        )
    return summaries
