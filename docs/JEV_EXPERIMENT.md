# Jev 선택형 관련성 실험

## 목적과 범위

Jev는 RAG 답변 생성기가 아니라 pgvector가 반환한 검색 후보를 평가하는 선택형 판정기다. Chunk별 직접 관련성과 문서 간 충돌을 확률로 반환하고, 기존 하네스는 검색 집합 ID와 핵심어를 다시 검증한다. Jev에게 DB 쓰기, 운영 명령 실행, 답변 확정 권한을 주지 않는다.

기본 실행은 `WORKFLOW_DOCUMENT_GRADER=ollama`이며 Vercel 계정이나 외부 API가 필요 없다. 자동 테스트도 Mock Client만 사용한다.

연동은 SDK 내부 규격이 아니라 Vercel의 [Evaluation HTTP API](https://vercel.com/docs/ai-gateway/modalities/evaluation#http-api) `POST /v1/evaluate`를 사용한다. 요청의 `model`, `state`, `questions`와 응답의 `answers`를 Pydantic Schema로 검증한다.

## 무료 조건 확인

- Vercel 공식 [Jev 모델 페이지](https://vercel.com/ai-gateway/models/jev)는 입력·출력을 무료로 표시하며 프로모션 종료일을 2026-09-25로 명시한다.
- Vercel 공식 [AI Gateway 가격 문서](https://vercel.com/docs/ai-gateway/pricing)는 무료 Team에 월 $5 크레딧이 있지만 무료 대상 모델은 일부라고 설명한다.
- 따라서 Jev를 영구 무료라고 가정하지 않는다. 저장소 기본 종료일 이후에는 호출을 차단하고 가격을 다시 확인해야 한다.
- 환경변수로 확인된 무료 종료일보다 이른 날짜를 선택할 수는 있지만 더 늦은 날짜로 연장할 수는 없다. 연장하려면 공식 가격을 다시 확인하고 코드의 상한과 문서를 함께 변경해야 한다.

## 준비

1. Vercel Dashboard의 AI Gateway에서 API Key를 만든다.
2. Key를 채팅, 문서, Git에 남기지 않고 로컬 `.env`에만 입력한다.
3. 저장소에 포함된 가상 매뉴얼 외의 실제 회사 문서나 개인정보를 사용하지 않는다.

```dotenv
WORKFLOW_DOCUMENT_GRADER=jev
AI_GATEWAY_API_KEY=로컬에서만_입력
JEV_FALLBACK_TO_OLLAMA=true
JEV_FREE_USE_NOT_AFTER=2026-09-25
```

## 실행과 확인

```powershell
docker compose up --build -d worker
docker compose logs -f worker
```

Jev 호출이 실패하면 로그에 `jev_document_grade_fallback`과 오류 종류만 기록하고 질문·문서·Secret은 남기지 않는다. 종료일이 지난 경우 `jev_disabled_free_period_ended`를 기록하고 처음부터 로컬 Ollama 판정을 사용한다.

실제 비교 결과를 기록할 때는 같은 질문, 같은 검색 Chunk, 같은 임계값을 사용한다. 관련성 정확도와 검토 전환 결과를 측정하기 전에는 Jev 확률을 신뢰도 정답으로 취급하지 않는다.
