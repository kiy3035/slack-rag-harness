# 문서 적재와 검색

3단계 검색 하네스는 무료 로컬 Ollama와 PostgreSQL의 pgvector 확장만 사용한다. 저장소의 `knowledge/manuals`에는 실제 회사 정보가 아닌 가상 운영 매뉴얼 6개가 있다.

## 사전 준비

Ollama를 호스트에 설치하고 768차원 임베딩 모델을 준비한다.

```powershell
ollama pull nomic-embed-text
docker compose up --build -d postgres rabbitmq api outbox-publisher
```

컨테이너에서 호스트 Ollama를 호출할 때 기본 주소는 `http://host.docker.internal:11434`다. 호스트에서 명령을 직접 실행할 때는 `.env.example`처럼 `http://localhost:11434`를 사용한다. `EMBEDDING_DIMENSIONS=768`은 Migration의 `vector(768)`과 묶인 값이므로 다른 차원 모델로 바꾸려면 새 Migration이 필요하다.

## 적재와 검색

애플리케이션 이미지에서 다음 명령을 실행할 수 있다.

```powershell
docker compose run --rm api python -m app.retrieval.main ingest knowledge/manuals
docker compose run --rm api python -m app.retrieval.main search "정산 배치 마감 전에 무엇을 확인하나요?"
```

Markdown은 제목 경계로 먼저 나뉘고, 긴 섹션은 기본 1,200자와 120자 중첩 규칙으로 추가 분할된다. 원문과 제목의 SHA-256이 같으면 임베딩 호출과 DB 쓰기를 모두 생략한다. 내용이 바뀌면 문서 버전을 올리고 이전 Chunk를 삭제한 뒤 새 Chunk 전체를 같은 트랜잭션에 저장한다. 따라서 검색 대상은 항상 현재 버전 하나이며 같은 문서 재적재로 Chunk가 누적되지 않는다.

검색은 코사인 유사도 Top-K를 사용한다. 최소 점수와 문서별 최대 Chunk 수는 각각 `RETRIEVAL_MIN_SCORE`, `RETRIEVAL_MAX_CHUNKS_PER_DOCUMENT`로 조정한다. 최소 점수 기본값 `-1.0`은 3단계에서 근거를 임의로 탈락시키지 않기 위한 값이며, 이후 고정 평가 데이터셋 결과를 근거로 조정해야 한다.
