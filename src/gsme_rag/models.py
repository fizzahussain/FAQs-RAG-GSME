from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class ParseWarning:
    code: str
    message: str
    location: str | None = None


@dataclass(slots=True)
class DocumentMetadata:
    source_id: str
    content_sha256: str
    normalized_sha256: str
    source_file: str
    document_type: str
    document_class: str
    schedule_kind: Literal["full", "shared"] | None = None
    schedule_kind_source: str | None = None
    classification_status: str = "ready"
    schema_version: str = "2"
    parser_version: str = "0.9.1"


@dataclass(slots=True)
class ParsedDocument:
    filename: str
    document_type: str
    strategy_used: str
    markdown: str
    warnings: list[ParseWarning] = field(default_factory=list)
    metadata: DocumentMetadata | None = None


@dataclass(slots=True)
class MarkdownChunk:
    chunk_id: str
    index: int
    text: str
    chunk_type: str
    source_file: str
    heading_path: list[str]
    category: str | None = None
    source_page_start: int | None = None
    source_page_end: int | None = None
    table_row_number: int | None = None
    unit_id: str = ""
    part_index: int = 0
    part_count: int = 1
    source_id: str = ""
    normalized_sha256: str = ""
    document_type: str = "markdown"
    document_class: str = "mixed"
    schedule_kind: str | None = None
    schedule_kind_source: str | None = None
    retrieval_channel: str = "semantic"
    strategy_used: str = "auto"
    routing_reason: str = ""
    schema_version: str = "2"
    source_row_number: int | None = None

    @property
    def character_count(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class ChunkResult:
    chunks: list[MarkdownChunk] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
