import pytest

from app.retrieval.chunker import MarkdownChunker


def test_chunker_splits_markdown_at_headings() -> None:
    """제목이 바뀌면 검색 근거와 제목 메타데이터가 함께 분리되는지 검증한다."""
    markdown = "# 운영 절차\n\n소개입니다.\n\n## 복구\n\n복구 절차입니다."

    chunks = MarkdownChunker(max_chars=200, overlap_chars=20).split(markdown)

    assert [chunk.heading for chunk in chunks] == ["운영 절차", "복구"]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert all(len(chunk.content_hash) == 64 for chunk in chunks)


def test_chunker_limits_long_section_and_preserves_overlap() -> None:
    """긴 단일 섹션도 최대 길이를 넘지 않고 여러 Chunk로 분할되는지 검증한다."""
    markdown = "# 긴 절차\n\n" + "복구 검증 단계를 반복합니다. " * 40

    chunks = MarkdownChunker(max_chars=220, overlap_chars=30).split(markdown)

    assert len(chunks) > 1
    assert all(len(chunk.content) <= 220 for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_chunker_rejects_empty_markdown() -> None:
    """검색 가치가 없는 공백 문서가 DB까지 도달하지 않는지 검증한다."""
    with pytest.raises(ValueError, match="비어 있는"):
        MarkdownChunker().split("  \n")

