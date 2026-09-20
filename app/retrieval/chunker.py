import hashlib
import re

from app.retrieval.schemas import DocumentChunk


HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class MarkdownChunker:
    """Markdown을 제목 경계와 제한 길이에 맞춰 결정적으로 분할한다."""

    def __init__(self, max_chars: int = 1_200, overlap_chars: int = 120) -> None:
        """Chunk 최대 길이와 긴 섹션의 문맥 중첩 길이를 설정한다."""
        if max_chars < 200:
            raise ValueError("max_chars는 200 이상이어야 합니다.")
        if overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("overlap_chars는 0 이상 max_chars 미만이어야 합니다.")
        self._max_chars = max_chars
        self._overlap_chars = overlap_chars

    def split(self, markdown: str) -> list[DocumentChunk]:
        """빈 본문을 거부하고 제목별 섹션을 검색 가능한 Chunk로 분리한다."""
        normalized = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("비어 있는 Markdown 문서는 분할할 수 없습니다.")

        raw_chunks: list[tuple[str | None, str]] = []
        for heading, section in self._split_sections(normalized):
            for part in self._split_long_section(section):
                raw_chunks.append((heading, part))

        return [
            DocumentChunk(
                chunk_index=index,
                heading=heading,
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
            for index, (heading, content) in enumerate(raw_chunks)
        ]

    def _split_sections(self, markdown: str) -> list[tuple[str | None, str]]:
        """제목이 바뀌는 지점에서 Markdown 섹션을 나눈다."""
        sections: list[tuple[str | None, str]] = []
        heading: str | None = None
        lines: list[str] = []

        for line in markdown.splitlines():
            match = HEADING_PATTERN.match(line)
            if match is not None:
                self._append_section(sections, heading, lines)
                heading = match.group(2).strip()
                lines = [line]
            else:
                lines.append(line)
        self._append_section(sections, heading, lines)
        return sections

    def _append_section(
        self,
        sections: list[tuple[str | None, str]],
        heading: str | None,
        lines: list[str],
    ) -> None:
        """공백뿐인 영역을 제외하고 정규화한 섹션을 누적한다."""
        content = "\n".join(lines).strip()
        if content:
            sections.append((heading, content))

    def _split_long_section(self, section: str) -> list[str]:
        """긴 섹션은 단어 경계를 우선해 제한 길이와 중첩을 지킨다."""
        if len(section) <= self._max_chars:
            return [section]

        parts: list[str] = []
        start = 0
        while start < len(section):
            hard_end = min(start + self._max_chars, len(section))
            end = hard_end
            if hard_end < len(section):
                boundary = max(
                    section.rfind("\n\n", start, hard_end),
                    section.rfind("\n", start, hard_end),
                    section.rfind(" ", start, hard_end),
                )
                if boundary > start + self._max_chars // 2:
                    end = boundary
            part = section[start:end].strip()
            if part:
                parts.append(part)
            if end >= len(section):
                break
            next_start = max(end - self._overlap_chars, start + 1)
            while next_start < end and not section[next_start].isspace():
                next_start += 1
            start = next_start
        return parts

