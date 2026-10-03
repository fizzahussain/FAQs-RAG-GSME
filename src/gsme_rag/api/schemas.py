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


class ChunkRequest(BaseModel):
    markdown: str
    source_file: str = "normalized.md"
    max_chars: int = Field(default=1800, ge=200, le=20_000)


class MarkdownChunkOut(BaseModel):
    chunk_id: str
    index: int = Field(ge=0)
    text: str
    chunk_type: str
    source_file: str
    heading_path: list[str]
    source_page_start: int | None = None
    source_page_end: int | None = None
    character_count: int = Field(ge=0)


class ChunkResponse(BaseModel):
    source_file: str
    max_chars: int
    chunk_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    chunks: list[MarkdownChunkOut]
    warnings: list[ParseWarningOut]
