# 문서 적재와 검색

3단계 검색 하네스는 무료 로컬 Ollama와 PostgreSQL의 pgvector 확장만 사용한다. 저장소의 `knowledge/manuals`에는 실제 회사 정보가 아닌 가상 운영 매뉴얼 6개가 있다.

## 사전 준비

Ollama를 호스트에 설치하고 768차원 임베딩 모델을 준비한다.

```powershell
ollama pull nomic-embed-text
docker compose up --build -d postgres rabbitmq api outbox-publisher
```

컨테이너에서 호스트 Ollama를 호출할 때 기본 주소는 `http://host.docker.internal:11434`다. 호스트에서 명령을 직접 실행할 때는 로컬 `.env`에 `OLLAMA_BASE_URL=http://localhost:11434`를 설정한다. `EMBEDDING_DIMENSIONS=768`은 Migration의 `vector(768)`과 묶인 값이므로 다른 차원 모델로 바꾸려면 새 Migration이 필요하다.

`nomic-embed-text`의 검색 계약에 맞춰 문서에는 `search_document:`, 질문에는 `search_query:`를 자동으로 붙인다. 이 접두어는 사용자가 입력하지 않는다.

## 적재와 검색

애플리케이션 이미지에서 다음 명령을 실행할 수 있다.

```powershell
docker compose run --rm api python -m app.retrieval.main ingest knowledge/manuals
docker compose run --rm api python -m app.retrieval.main search "정산 배치 마감 전에 무엇을 확인하나요?"
```

Markdown은 제목 경계로 먼저 나뉘고, 긴 섹션은 기본 1,200자와 120자 중첩 규칙으로 추가 분할된다. 원문·제목·임베딩 모델·검색 접두어 조합의 SHA-256이 같으면 임베딩 호출과 DB 쓰기를 모두 생략한다. 내용이나 임베딩 설정이 바뀌면 문서 버전을 올리고 이전 Chunk를 삭제한 뒤 새 Chunk 전체를 같은 트랜잭션에 저장한다. 따라서 검색 대상은 항상 현재 버전 하나이며 같은 문서 재적재로 Chunk가 누적되지 않는다.

검색은 코사인 유사도 Top-K를 사용한다. 최소 점수와 문서별 최대 Chunk 수는 각각 `RETRIEVAL_MIN_SCORE`, `RETRIEVAL_MAX_CHUNKS_PER_DOCUMENT`로 조정한다. 최소 점수 기본값 `-1.0`은 3단계에서 근거를 임의로 탈락시키지 않기 위한 값이며, 이후 고정 평가 데이터셋 결과를 근거로 조정해야 한다.

통합 테스트는 자동으로 생성되는 `rag_harness_test` DB만 비운다. 개발 DB의 실제 Ollama 임베딩 문서는 테스트 실행 뒤에도 유지된다.
