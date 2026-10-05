# 평가 하네스 가이드

## 1. 목적과 데이터 경계

평가 하네스는 모델이나 검색 설정을 바꾸기 전후에 같은 질문과 같은 판정 규칙을 실행해 회귀를 확인한다. 평가용 Workflow는 실제 `WorkflowNodes`, pgvector 검색과 로컬 Ollama Client를 재사용한다. Case 내부 LangGraph Checkpoint는 메모리에만 저장하고, 완료된 Case 결과와 누적 활성 실행시간은 실행 ID별 로컬 JSON Checkpoint에 원자적으로 저장한다. `ai_job`, `review_queue`, `answer_citation`과 RabbitMQ에는 쓰지 않는다.

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
  --run-id baseline-v1 `
  --top-k 5 `
  --min-score -1.0 `
  --max-chunks-per-document 2 `
  --max-query-rewrites 1 `
  --worker-count 1
```

실행 중에는 Case가 끝날 때마다 `evaluation/results/baseline-v1.checkpoint.json`을 임시 파일 교체 방식으로 저장하고 진행률을 한 줄 JSON으로 출력한다. 중단되면 설정을 바꾸지 않고 같은 명령을 실행한다. 데이터셋 SHA-256, Top-K, 임계값, 문서별 Chunk 수, 재작성 횟수, Worker 수, 운영체제·CPU와 모델명이 모두 같아야 저장된 Case 이후부터 재개한다.

같은 실행 ID는 운영체제 파일 잠금으로 한 프로세스만 사용할 수 있다. 이미 실행 중인 ID를 다시 시작하면 즉시 실패하므로, 먼저 시작한 컨테이너가 실제로 종료됐는지 확인한 뒤 재개해야 한다. 프로세스 종료나 장애 시 잠금은 운영체제가 자동 해제하며 `.lock` 파일 자체는 진단 정보로 남을 수 있다.

완료되면 `evaluation/results/baseline-v1.json`과 같은 이름의 `.md`가 생성되고 Checkpoint는 제거된다. JSON에는 모든 Case의 검색 Chunk, 인용, 답변, 검토 여부, 지연과 안전한 오류 코드가 포함된다. 실행 ID를 생략하면 자동 생성된 ID가 첫 진행 로그에 출력되므로 그 값을 재개 명령에 사용한다.

## 5. 한 조건 비교

다음 후보는 기준값에서 표시된 조건 하나만 변경한다.

```powershell
# Top-K 후보
docker compose run --rm api python -m app.evaluation.main run --run-id top-k-3-v1 --top-k 3 --min-score -1.0 --max-query-rewrites 1 --worker-count 1
docker compose run --rm api python -m app.evaluation.main run --run-id top-k-8-v1 --top-k 8 --min-score -1.0 --max-query-rewrites 1 --worker-count 1

# 유사도 임계값 후보
docker compose run --rm api python -m app.evaluation.main run --run-id min-score-0p2-v1 --top-k 5 --min-score 0.2 --max-query-rewrites 1 --worker-count 1

# 재작성 적용 전후
docker compose run --rm api python -m app.evaluation.main run --run-id no-rewrite-v1 --top-k 5 --min-score -1.0 --max-query-rewrites 0 --worker-count 1

# Worker 수 비교
docker compose run --rm api python -m app.evaluation.main run --run-id workers-2-v1 --top-k 5 --min-score -1.0 --max-query-rewrites 1 --worker-count 2
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

### 6.1. 2026-10-05 Worker 수 비교

WSL2 Linux x86_64, 논리 CPU 16개, `qwen3:1.7b`, `nomic-embed-text` 환경에서 같은 60건 데이터셋을 실행했다. 기준 실행은 Worker 1개이며 후보는 Worker 수만 2개로 변경했다.

| 실행 | Worker | Recall@5 | 인용 정확도 | 검토 전환 | 근거 없는 문장률 | 처리량(cases/s) | p95 | 총 실행시간 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline-cpu-v3` | 1 | 74.00% | 93.62% | 70.00% | 60.12% | 0.019200 | 91,920.22ms | 52.08분 |
| `workers-2-cpu-v1` | 2 | 74.00% | 94.00% | 65.00% | 61.05% | 0.018232 | 175,151.57ms | 54.85분 |

Worker 2는 Worker 1보다 처리량이 약 5.0% 낮고 p95가 약 90.6% 높았다. 단일 로컬 Ollama CPU 모델이 생성 요청을 처리하는 환경에서는 Worker를 늘려도 추론 처리량이 선형 증가하지 않고 자원 경합이 커진 것으로 해석한다. GPU 또는 여러 Ollama 인스턴스로 일반화할 수 없으며, 생성의 비결정성 때문에 작은 품질 차이도 Worker 수의 직접 효과로 단정하지 않는다.

## 7. 자동 테스트

평가 단위 테스트는 Ollama 없이 데이터셋 계약, 지표 분모, 근거 없는 문장 규칙, JSON·Markdown 생성, 단일 조건 비교 제한과 Checkpoint 저장·계약 검증·완료 Case 건너뛰기·동일 실행 ID 중복 잠금을 검증한다.

```powershell
docker compose --profile test run --build --rm test
```
