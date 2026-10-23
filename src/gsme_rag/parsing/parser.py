from pathlib import Path
import hashlib
from zipfile import BadZipFile

import pymupdf as fitz
from docx.opc.exceptions import PackageNotFoundError

from gsme_rag.parsing.csv import parse_csv_to_markdown
from gsme_rag.parsing.docx import parse_docx_to_markdown
from gsme_rag.parsing.image import parse_image_to_markdown
from gsme_rag.models import DocumentMetadata, ParsedDocument
from gsme_rag.parsing.schedules import is_schedule_header, make_source_id, resolve_schedule_kind
from gsme_rag.parsing.pdf import parse_pdf_to_markdown


def parse_to_markdown(
    path: str | Path,
    source_name: str | None = None,
    schedule_kind: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    suffix = input_path.suffix.casefold()
    if suffix != ".csv" and schedule_kind in {"full", "shared"}:
        raise ValueError("Full or Shared schedule type applies only to a schedule CSV")
    try:
        if suffix == ".docx":
            result = parse_docx_to_markdown(input_path, source_name=source_name)
        elif suffix == ".pdf":
            result = parse_pdf_to_markdown(input_path, source_name=source_name)
        elif suffix == ".csv":
            result = parse_csv_to_markdown(input_path, source_name=source_name, schedule_kind=schedule_kind)
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
            result = parse_image_to_markdown(input_path, source_name=source_name)
        else:
            raise ValueError("Input must be a DOCX, PDF, CSV, PNG, JPEG, TIFF, or BMP file")
    except (fitz.FileDataError, BadZipFile, PackageNotFoundError) as error:
        raise ValueError(f"The {suffix.lstrip('.').upper()} file is invalid or unreadable") from error
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    is_schedule = False
    # Header detection checks the table header itself, never row counts or dates.
    if suffix == ".csv":
        header = next((line for line in result.markdown.splitlines() if line.startswith("|")), "")
        is_schedule = is_schedule_header(header.strip("|").split("|"))
    kind = resolve_schedule_kind(result.filename, schedule_kind) if is_schedule else None
    kind_source = (
        "filename" if kind and resolve_schedule_kind(result.filename) == kind
        else "user" if kind else None
    )
    result.metadata = DocumentMetadata(
        source_id=make_source_id(digest, kind),
        content_sha256=digest,
        normalized_sha256=hashlib.sha256(result.markdown.encode("utf-8")).hexdigest(),
        source_file=result.filename,
        document_type=result.document_type,
        document_class="schedule" if is_schedule else "document",
        schedule_kind=kind,
        schedule_kind_source=kind_source,
        classification_status="needs_schedule_kind" if is_schedule and not kind else "ready",
        csv_source_rows=result.csv_source_rows if is_schedule else None,
    )
    for evidence in result.evidence_diagnostics:
        evidence.content_sha256 = digest
        evidence.evidence_id = "evidence-" + hashlib.sha256(
            f"{digest}|{evidence.evidence_id}".encode("utf-8")
        ).hexdigest()[:20]
    return result
