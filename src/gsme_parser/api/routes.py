from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.encoders import jsonable_encoder

from gsme_parser.api.schemas import ParseResponse, ParseWarningOut
from gsme_parser.faq import parse_faq
from gsme_parser.schedule import parse_schedule_csv


router = APIRouter(prefix="/v1")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".pdf", ".csv"}

STRATEGY_LABELS = {
    "docx_table": "native_docx_table",
    "docx_paragraphs": "formatted_docx_question_answer",
    "pdf_qa_layout": "layout_aware_pdf_question_answer",
    "pdf_table_layout": "layout_aware_pdf_table",
}


def _parser_strategy(suffix: str, requested: str) -> str:
    if requested == "auto":
        return "auto"
    if suffix == ".docx":
        return "paragraphs" if requested == "qa" else requested
    if suffix == ".pdf":
        return "qa" if requested == "paragraphs" else requested
    return "auto"


def _used_faq_strategy(records, suffix: str, requested: str) -> str:
    if records:
        return STRATEGY_LABELS.get(records[0].source_kind, records[0].source_kind)
    if requested != "auto":
        return f"{suffix.removeprefix('.')}_{requested}"
    return "not_detected"


async def _save_upload(upload: UploadFile) -> tuple[Path, str, str]:
    original_name = Path(upload.filename or "upload").name
    suffix = Path(original_name).suffix.casefold()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Supported files are DOCX, born-digital PDF, and CSV",
        )

    total = 0
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as target:
        path = Path(target.name)
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Upload exceeds the 20 MiB demo limit",
                )
            target.write(chunk)
    return path, original_name, suffix


@router.get("/health", summary="Check that the demo API is running")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/strategies", summary="Show the hybrid parsing branches")
async def strategies() -> dict:
    return {
        "approach": "document-type-aware, structure-first hybrid parsing",
        "branches": {
            "docx_table": "Read native Word rows and cells",
            "docx_question_answer": "Use paragraph order, headings, bold text, and question patterns",
            "pdf_question_answer": "Use font properties, coordinates, and reading order",
            "pdf_table": "Use column position and cross-page boundaries",
            "schedule_csv": "Map headers, convert types, and validate date order",
        },
        "out_of_scope": ["scanned PDFs", "images", "OCR", "database storage", "search"],
    }


@router.post(
    "/parse",
    response_model=ParseResponse,
    summary="Upload a file and preview normalized parser output",
)
async def parse_preview(
    file: UploadFile = File(...),
    strategy: str = Form(default="auto", pattern="^(auto|qa|table|paragraphs)$"),
    schedule_type: str | None = Form(default=None, pattern="^(shared|full)$"),
) -> ParseResponse:
    path, original_name, suffix = await _save_upload(file)
    try:
        if suffix in {".docx", ".pdf"}:
            selected = _parser_strategy(suffix, strategy)
            try:
                result = await asyncio.to_thread(parse_faq, path, selected)
            except (OSError, ValueError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

            for record in result.faq_records:
                record.source_file = original_name
            records = [jsonable_encoder(asdict(record)) for record in result.faq_records]
            strategy_used = _used_faq_strategy(result.faq_records, suffix, strategy)
            document_type = "docx_faq" if suffix == ".docx" else "born_digital_pdf_faq"
        else:
            chosen_schedule_type = schedule_type
            if chosen_schedule_type is None:
                lowered_name = original_name.casefold()
                if "shared" in lowered_name:
                    chosen_schedule_type = "shared"
                elif "full" in lowered_name:
                    chosen_schedule_type = "full"
            try:
                result = await asyncio.to_thread(
                    parse_schedule_csv,
                    path,
                    chosen_schedule_type,
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{error}. Select shared or full when the filename does not identify it."
                    ),
                ) from error
            for record in result.schedule_records:
                record.source_file = original_name
            records = [
                jsonable_encoder({"schedule_id": record.schedule_id, **asdict(record)})
                for record in result.schedule_records
            ]
            strategy_used = "schema_aware_schedule_csv"
            document_type = "schedule_csv"

        warnings = [ParseWarningOut.model_validate(asdict(item)) for item in result.warnings]
        return ParseResponse(
            filename=original_name,
            document_type=document_type,
            strategy_requested=strategy,
            strategy_used=strategy_used,
            record_count=len(records),
            warning_count=len(warnings),
            records=records,
            warnings=warnings,
        )
    finally:
        path.unlink(missing_ok=True)
