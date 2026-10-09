from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class ParseWarningOut(BaseModel):
    code: str
    message: str
    location: str | None = None


class DocumentMetadataOut(BaseModel):
    source_id: str
    content_sha256: str
    normalized_sha256: str
    source_file: str
    document_type: str
    document_class: str
    schedule_kind: Literal["full", "shared"] | None = None
    schedule_kind_source: str | None = None
    classification_status: str
    schema_version: str
    parser_version: str
    csv_source_rows: list[int] | None = None


class ParseResponse(BaseModel):
    filename: str
    document_type: str
    strategy_used: str
    markdown: str
    markdown_character_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    warnings: list[ParseWarningOut]
    metadata: DocumentMetadataOut


class ScheduleRecordOut(BaseModel):
    source_row_number: int
    schedule_kind: Literal["full", "shared"] | None
    technology_nm: int
    flavor: str
    trial_deadline: date
    final_deadline: date
    tapeout_date: date
    estimated_ship_date: date
    source_id: str = ""
    record_id: str = ""
    source_file: str = ""


class SchedulePreviewResponse(BaseModel):
    source_file: str
    schedule_kind: Literal["full", "shared"] | None
    total_records: int
    matched_records: int
    records: list[ScheduleRecordOut]
    warnings: list[ParseWarningOut]
    source_id: str = ""
    schedule_kind_source: str | None = None
    classification_status: str = "ready"
    retrieval_channel: Literal["structured"] = "structured"


class ChunkRequest(BaseModel):
    markdown: str
    source_file: str = "normalized.md"
    max_chars: int = Field(default=1800, ge=200, le=20_000)
    strategy: Literal[
        "auto",
        "fixed_size",
        "layout_aware",
        "hybrid",
        "paragraph_aware",
    ] = "auto"
    overlap_chars: int = Field(default=120, ge=0)
    metadata: DocumentMetadataOut | None = None
    semantic_schedule_rows: bool = False


class MarkdownChunkOut(BaseModel):
    chunk_id: str
    index: int = Field(ge=0)
    text: str
    chunk_type: str
    source_file: str
    heading_path: list[str] | None = None
    category: str | None = None
    faq_question: str | None = None
    source_page_start: int | None = None
    source_page_end: int | None = None
    table_row_number: int | None = None
    unit_id: str | None = None
    part_index: int | None = Field(default=None, ge=0)
    part_count: int | None = Field(default=None, ge=1)
    source_id: str
    schedule_kind: Literal["full", "shared"] | None = None
    source_row_number: int | None = None


class ChunkResponse(BaseModel):
    source_file: str
    max_chars: int
    strategy: str
    overlap_chars: int
    chunk_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    chunks: list[MarkdownChunkOut]
    warnings: list[ParseWarningOut]
    retrieval_channel: Literal["semantic"] = "semantic"
    source_id: str = ""


class EvidenceCase(BaseModel):
    question: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class CompareRequest(BaseModel):
    markdown: str
    source_file: str = "normalized.md"
    max_chars: int = Field(default=1800, ge=200, le=20_000)
    overlap_chars: int = Field(default=120, ge=0)
    cases: list[EvidenceCase] = Field(default_factory=list)