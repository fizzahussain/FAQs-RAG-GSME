from dataclasses import dataclass, field


@dataclass(slots=True)
class ParseWarning:
    code: str
    message: str
    location: str | None = None


@dataclass(slots=True)
class ParsedDocument:
    filename: str
    document_type: str
    strategy_used: str
    markdown: str
    warnings: list[ParseWarning] = field(default_factory=list)


@dataclass(slots=True)
class MarkdownChunk:
    chunk_id: str
    index: int
    text: str
    chunk_type: str
    source_file: str
    heading_path: list[str]
    source_page_start: int | None = None
    source_page_end: int | None = None

    @property
    def character_count(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class ChunkResult:
    chunks: list[MarkdownChunk] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
