from datetime import date

from typing import Literal

from pydantic import BaseModel, Field


class ParseWarningOut(BaseModel):
    code: str
    message: str
    location: str | None = None


class ParseResponse(BaseModel):
    filename: str
    document_type: str
    strategy_used: str
    markdown: str
    markdown_character_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    warnings: list[ParseWarningOut]


class ScheduleRecordOut(BaseModel):
    source_row_number: int
    technology_nm: int
    flavor: str
    trial_deadline: date
    final_deadline: date
    tapeout_date: date
    estimated_ship_date: date


class SchedulePreviewResponse(BaseModel):
    source_file: str
    total_records: int
    matched_records: int
    records: list[ScheduleRecordOut]


class ChunkRequest(BaseModel):
    markdown: str
    source_file: str = "normalized.md"
    max_chars: int = Field(default=1800, ge=200, le=20_000)
    strategy: Literal["fixed_size", "layout_aware", "hybrid", "paragraph_aware"] = "layout_aware"
    overlap_chars: int = Field(default=120, ge=0)


class MarkdownChunkOut(BaseModel):
    chunk_id: str
    index: int = Field(ge=0)
    text: str
    chunk_type: str
    source_file: str
    heading_path: list[str]
    source_page_start: int | None = None
    source_page_end: int | None = None
    table_row_number: int | None = None
    unit_id: str
    part_index: int = Field(ge=0)
    part_count: int = Field(ge=1)
    character_count: int = Field(ge=0)


class ChunkResponse(BaseModel):
    source_file: str
    max_chars: int
    strategy: str
    overlap_chars: int
    chunk_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    chunks: list[MarkdownChunkOut]
    warnings: list[ParseWarningOut]


class EvidenceCase(BaseModel):
    question: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class CompareRequest(BaseModel):
    markdown: str
    source_file: str = "normalized.md"
    max_chars: int = Field(default=1800, ge=200, le=20_000)
    overlap_chars: int = Field(default=120, ge=0)
    cases: list[EvidenceCase] = Field(default_factory=list)
