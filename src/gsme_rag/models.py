from dataclasses import dataclass, field
import re
from typing import Literal


@dataclass(slots=True)
class ParseWarning:
    code: str
    message: str
    location: str | None = None


@dataclass(slots=True)
class EvidenceDiagnostic:
    """Raw, unapproved source evidence; decisions live in a separate ledger."""

    evidence_id: str
    kind: str
    page: int
    bbox: tuple[float, float, float, float] | None
    original_text: str
    alternate_reading: str | None
    reason: str
    content_sha256: str = ""


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
    schema_version: str = "3"
    parser_version: str = "0.14.6"
    csv_source_rows: list[int] | None = None


@dataclass(slots=True)
class ParsedDocument:
    filename: str
    document_type: str
    strategy_used: str
    markdown: str
    warnings: list[ParseWarning] = field(default_factory=list)
    metadata: DocumentMetadata | None = None
    csv_source_rows: list[int] | None = None
    evidence_diagnostics: list[EvidenceDiagnostic] = field(default_factory=list)


def _extract_faq_question(
    chunk_type: str, text: str, heading_path: list[str]
) -> str | None:
    """Return the source question only for a recognized FAQ chunk."""
    if chunk_type == "table_row":
        match = re.search(
            r"(?:^|\n)Question:\s*(.*?)\n\nAnswer:", text, flags=re.DOTALL
        )
        if not match:
            return None
        question = match.group(1)
    elif chunk_type == "question_answer":
        content = text
        if heading_path:
            _, separator, remainder = content.partition("\n\n")
            if separator:
                content = remainder
        question = content.split("\n\n", 1)[0].strip()
        if question.startswith("**") and question.endswith("**"):
            question = question[2:-2]
        elif question.startswith("__") and question.endswith("__"):
            question = question[2:-2]
        if not question.strip().endswith("?"):
            return None
    else:
        return None
    return re.sub(r"\s+", " ", question).strip() or None


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
    content_sha256: str = ""
    schedule_kind: str | None = None
    source_row_number: int | None = None
    faq_question: str | None = None
    review_required: bool = False

    def __post_init__(self) -> None:
        if self.faq_question is None:
            self.faq_question = _extract_faq_question(
                self.chunk_type, self.text, self.heading_path
            )

    @property
    def character_count(self) -> int:
        return len(self.text)


def chunk_to_record(chunk: MarkdownChunk) -> dict:
    """Expose source evidence and location without repeating document diagnostics."""
    record = {
        "chunk_id": chunk.chunk_id,
        "index": chunk.index,
        "text": chunk.text,
        "chunk_type": chunk.chunk_type,
        "source_file": chunk.source_file,
        "source_id": chunk.source_id,
    }
    if chunk.content_sha256:
        record["content_sha256"] = chunk.content_sha256
    if chunk.category:
        record["category"] = chunk.category
    if chunk.heading_path and chunk.heading_path != [chunk.category]:
        record["heading_path"] = chunk.heading_path
    if chunk.faq_question:
        record["faq_question"] = chunk.faq_question
    if chunk.source_page_start is not None:
        record["source_page_start"] = chunk.source_page_start
        if chunk.source_page_end != chunk.source_page_start:
            record["source_page_end"] = chunk.source_page_end
    if chunk.table_row_number is not None:
        record["table_row_number"] = chunk.table_row_number
    if chunk.source_row_number is not None:
        record["source_row_number"] = chunk.source_row_number
    if chunk.schedule_kind:
        record["schedule_kind"] = chunk.schedule_kind
    if chunk.review_required:
        record["review_required"] = True
    if chunk.part_count > 1:
        record.update(
            unit_id=chunk.unit_id,
            part_index=chunk.part_index,
            part_count=chunk.part_count,
        )
    return record


@dataclass(slots=True)
class ChunkResult:
    chunks: list[MarkdownChunk] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    content_route: str | None = None
