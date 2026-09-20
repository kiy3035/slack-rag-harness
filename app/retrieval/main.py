import argparse
import asyncio
from pathlib import Path

from app.common.config import Settings, get_settings
from app.db.session import session_factory
from app.retrieval.chunker import MarkdownChunker
from app.retrieval.embedding import OllamaEmbeddingClient
from app.retrieval.loader import load_markdown_documents
from app.retrieval.repository import KnowledgeRepository
from app.retrieval.service import DocumentIngestionService, KnowledgeSearchService


def build_parser() -> argparse.ArgumentParser:
    """문서 적재와 검색을 로컬에서 실행할 명령행 인자를 정의한다."""
    parser = argparse.ArgumentParser(description="로컬 Markdown 검색 하네스")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser("ingest", help="Markdown 매뉴얼을 적재합니다.")
    ingest_parser.add_argument("directory", nargs="?", default="knowledge/manuals")
    search_parser = subparsers.add_parser("search", help="적재된 매뉴얼을 검색합니다.")
    search_parser.add_argument("question")
    return parser


def build_client(settings: Settings) -> OllamaEmbeddingClient:
    """검증된 설정으로 로컬 Ollama 임베딩 Client를 생성한다."""
    return OllamaEmbeddingClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.ollama_timeout_seconds,
    )


async def run_ingestion(directory: Path, settings: Settings) -> None:
    """디렉터리의 모든 Markdown 문서를 현재 버전으로 적재한다."""
    repository = KnowledgeRepository(session_factory)
    client = build_client(settings)
    service = DocumentIngestionService(
        repository=repository,
        chunker=MarkdownChunker(
            max_chars=settings.document_chunk_max_chars,
            overlap_chars=settings.document_chunk_overlap_chars,
        ),
        embedding_client=client,
        embedding_dimensions=settings.embedding_dimensions,
    )
    try:
        for document in load_markdown_documents(directory):
            result = await service.ingest(document)
            print(result.model_dump_json())
    finally:
        await client.aclose()


async def run_search(question: str, settings: Settings) -> None:
    """질문과 가까운 현재 문서 Chunk를 JSON으로 출력한다."""
    repository = KnowledgeRepository(session_factory)
    client = build_client(settings)
    service = KnowledgeSearchService(
        repository=repository,
        embedding_client=client,
        embedding_dimensions=settings.embedding_dimensions,
        top_k=settings.retrieval_top_k,
        min_score=settings.retrieval_min_score,
        max_chunks_per_document=settings.retrieval_max_chunks_per_document,
    )
    try:
        for hit in await service.search(question):
            print(hit.model_dump_json())
    finally:
        await client.aclose()


async def run(args: argparse.Namespace, settings: Settings) -> None:
    """선택된 명령만 실행하고 알 수 없는 명령은 명시적으로 거부한다."""
    if args.command == "ingest":
        await run_ingestion(Path(args.directory), settings)
        return
    if args.command == "search":
        await run_search(args.question, settings)
        return
    raise ValueError(f"지원하지 않는 명령입니다: {args.command}")


def main() -> None:
    """설정을 한 번 읽고 비동기 검색 명령을 실행한다."""
    asyncio.run(run(build_parser().parse_args(), get_settings()))


if __name__ == "__main__":
    main()
