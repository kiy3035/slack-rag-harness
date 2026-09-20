from pathlib import Path

from app.retrieval.schemas import MarkdownDocument


def load_markdown_documents(directory: Path) -> list[MarkdownDocument]:
    """지정 디렉터리의 가상 Markdown 매뉴얼을 안정된 경로 순서로 읽는다."""
    if not directory.is_dir():
        raise ValueError(f"문서 디렉터리를 찾을 수 없습니다: {directory}")
    documents: list[MarkdownDocument] = []
    for path in sorted(directory.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        documents.append(
            MarkdownDocument(
                source_path=path.as_posix(),
                title=_extract_title(path, content),
                content=content,
            )
        )
    if not documents:
        raise ValueError("적재할 Markdown 문서가 없습니다.")
    return documents


def _extract_title(path: Path, content: str) -> str:
    """첫 번째 H1을 문서 제목으로 사용하고 없으면 파일명을 사용한다."""
    for line in content.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return path.stem

