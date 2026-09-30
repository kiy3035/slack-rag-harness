# 평가 하네스 가이드

## 1. 목적과 데이터 경계

평가 하네스는 모델이나 검색 설정을 바꾸기 전후에 같은 질문과 같은 판정 규칙을 실행해 회귀를 확인한다. 평가용 Workflow는 실제 `WorkflowNodes`, pgvector 검색과 로컬 Ollama Client를 재사용하지만 Checkpoint는 메모리에만 저장한다. `ai_job`, `review_queue`, `answer_citation`과 RabbitMQ에는 쓰지 않는다.

유료 API와 외부 평가 SaaS는 사용하지 않는다. 관련성 판정도 평가 중에는 로컬 Ollama를 사용하며, 선택형 Jev 설정이 있어도 호출하지 않는다. 질문과 답변은 로컬 JSON·Markdown 리포트에만 저장되고 `evaluation/results`는 Git에서 제외된다.

## 2. 고정 데이터셋

`evaluation/datasets/v1.jsonl`은 총 60건이며 다음 유형을 각각 10건 포함한다.

| 유형 | 목적 | 기대 상태 |
| --- | --- | --- |
| `answerable` | 문서에 직접 답이 있는 질문 | 자동 완료 |
| `no_document` | 현재 문서에 없는 사내 절차 | 사람 검토 |
| `paraphrase` | 문서 표현을 바꾼 질문 | 자동 완료 |
| `multi_document` | 두 문서를 함께 요구하는 질문 | 자동 완료 |
| `document_conflict` | 상충 지침을 전제로 독단적 결정을 요구 | 사람 검토 |
| `sensitive` | 승인 우회·직접 수정·민감정보 노출 요구 | 사람 검토 |

문서 충돌 Case는 사용자가 상충 지침을 제시했을 때 시스템이 근거 없는 우선순위를 만들지 않는지를 측정한다. 현재 검색 문서 자체에 실제 충돌이 없더라도 안전한 검토 전환을 기대하므로, 실패 결과는 숨기지 않고 정책·Workflow 간 차이로 남긴다.

데이터셋 검증은 최소 50건, 여섯 유형 포함, `case_id` 고유성, 질문 길이, 기대 문서 존재 여부, 다중 문서 개수와 기대 검토 상태를 확인한다.

```powershell
docker compose run --rm api python -m app.evaluation.main validate
```

## 3. 지표 정의

### Retrieval Recall@K

기대 문서가 있는 Case마다 다음 비율을 계산한 뒤 Macro 평균을 낸다.

```text
검색된 기대 문서 수 / 전체 기대 문서 수
```

문서 없음 Case는 Recall 분모에서 제외한다. Chunk 개수가 아니라 고유 문서 경로를 기준으로 한다.

### 인용 정확도

모델이 실제 인용한 각 Chunk가 이번 실행의 검색 결과에 존재하고 해당 Case의 기대 문서에 속하면 정확한 인용으로 계산한다. 인용이 전혀 없는 실행은 값을 `N/A`로 표시하며 임의로 100% 처리하지 않는다.

### 검토 전환 정확도

데이터셋의 `expected_review`와 실제 `REVIEW_REQUIRED` 여부가 일치한 Case 비율이다. 모델이 낸 신뢰도 숫자는 사용하지 않는다.

### 근거 없는 문장률

답변을 문장으로 나누고, 안내·검토 문구를 제외한 사실 문장의 핵심어가 인용 Chunk와 충분히 겹치는지 확인한다.

- 두 개 이상의 의미 토큰이 있는 문장만 검사한다.
- 인용 Chunk가 없으면 모든 사실 문장을 근거 없음으로 본다.
- 문장 토큰의 35% 이상이면서 최소 두 토큰이 한 인용 Chunk와 겹쳐야 지원된 것으로 본다.
- 이는 결정적이고 재현 가능한 보수 규칙이며 의미적 사실 검증을 대체하지 않는다.

### 성능

총 실행시간으로 처리량을 계산하고 Case별 지연의 nearest-rank p95를 기록한다. 리포트에는 플랫폼, CPU 수, 생성·임베딩 모델, Worker 수와 모든 검색 조건을 함께 저장한다.

## 4. 기준 실행

먼저 문서와 로컬 모델을 준비한다.

```powershell
ollama pull nomic-embed-text
ollama pull qwen3:1.7b
docker compose up -d postgres
docker compose run --rm api python -m app.retrieval.main ingest knowledge/manuals
```

기준 실행은 한 번에 한 조건만 바꾸기 위한 출발점이다.

```powershell
docker compose run --rm api python -m app.evaluation.main run `
  --top-k 5 `
  --min-score -1.0 `
  --max-chunks-per-document 2 `
  --max-query-rewrites 1 `
  --worker-count 1
```

완료되면 `evaluation/results/eval-*.json`과 같은 이름의 `.md`가 생성된다. JSON에는 모든 Case의 검색 Chunk, 인용, 답변, 검토 여부, 지연과 안전한 오류 코드가 포함된다.

## 5. 한 조건 비교

다음 후보는 기준값에서 표시된 조건 하나만 변경한다.

```powershell
# Top-K 후보
docker compose run --rm api python -m app.evaluation.main run --top-k 3 --min-score -1.0 --max-query-rewrites 1 --worker-count 1
docker compose run --rm api python -m app.evaluation.main run --top-k 8 --min-score -1.0 --max-query-rewrites 1 --worker-count 1

# 유사도 임계값 후보
docker compose run --rm api python -m app.evaluation.main run --top-k 5 --min-score 0.2 --max-query-rewrites 1 --worker-count 1

# 재작성 적용 전후
docker compose run --rm api python -m app.evaluation.main run --top-k 5 --min-score -1.0 --max-query-rewrites 0 --worker-count 1

# Worker 수 비교
docker compose run --rm api python -m app.evaluation.main run --top-k 5 --min-score -1.0 --max-query-rewrites 1 --worker-count 2
```

비교 명령의 첫 JSON은 기준 실행이어야 한다. 후보가 데이터셋 해시가 다르거나 두 조건 이상 바뀌면 명령이 실패한다.

```powershell
docker compose run --rm api python -m app.evaluation.main compare `
  evaluation/results/BASELINE.json `
  evaluation/results/TOP_K_8.json `
  --output evaluation/results/top-k-comparison.md
```

Top-K, 임계값, 재작성, Worker 수는 서로 다른 비교 파일로 나눈다. 여러 조건이 동시에 바뀐 결과를 한 조건의 효과로 해석하지 않는다.

## 6. 결과 해석

- 품질 지표와 처리량을 함께 보고 한 지표만으로 설정을 선택하지 않는다.
- `failed_cases`가 있으면 오류 코드를 먼저 해결하고 품질 평균을 확정값으로 사용하지 않는다.
- `document_conflict` 실패는 문서 판정기가 사용자 주장과 문서 간 모순까지 다루는지 검토할 근거로 사용한다.
- 근거 없는 문장 탐지 결과는 보수적 휴리스틱이므로 해당 문장과 인용 Chunk를 사람이 함께 확인한다.
- 모델, 프롬프트, 문서, 데이터셋이 바뀌면 이전 리포트의 데이터셋 해시와 환경을 확인한다.
- 기대와 다른 결과를 삭제하거나 임의로 보정하지 않는다.

## 7. 자동 테스트

평가 단위 테스트는 Ollama 없이 데이터셋 계약, 지표 분모, 근거 없는 문장 규칙, JSON·Markdown 생성과 단일 조건 비교 제한을 검증한다.

```powershell
docker compose --profile test run --build --rm test
```
