from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from gsme_rag.api.schemas import (
    ChunkRequest,
    ChunkResponse,
    CompareRequest,
    MarkdownChunkOut,
    ParseResponse,
    ParseWarningOut,
    SchedulePreviewResponse,
)
from gsme_rag.chunking import chunk_markdown, compare_strategies
from gsme_rag.models import DocumentMetadata, chunk_to_record
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.ocr import tesseract_environment
from gsme_rag.parsing.schedules import (
    filter_schedule_records,
    make_source_id,
    parse_schedule_records,
    resolve_schedule_kind,
)


router = APIRouter(prefix="/v1")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".pdf", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


async def _save_upload(upload: UploadFile) -> tuple[Path, str, str]:
    original_name = Path(upload.filename or "upload").name
    suffix = Path(original_name).suffix.casefold()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Supported files are DOCX, PDF, CSV, PNG, JPEG, TIFF, and BMP",
        )

    total = 0
    # Stream demo uploads to disk within the public size limit
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as target:
            path = Path(target.name)
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Upload exceeds the 20 MiB demo limit",
                    )
                target.write(chunk)
    except BaseException:
        # Close the handle before removing a rejected/interrupted upload on Windows.
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path, original_name, suffix


@router.get("/health", summary="Check that the demo API is running")
async def health() -> dict[str, str | bool]:
    return {"status": "ok", "ocr_available": tesseract_environment()["english_available"]}


@router.get("/architecture", summary="Show the separated processing stages")
async def architecture() -> dict:
    return {
        "stage_1": {
            "name": "parser",
            "input": ["DOCX", "PDF", "CSV", "PNG/JPEG/TIFF/BMP images"],
            "output": "normalized Markdown",
            "schedule_csv_output": "optional typed records for exact filtering",
            "makes_chunk_decisions": False,
        },
        "stage_2": {
            "name": "chunker",
            "input": "normalized Markdown",
            "output": "chunks with source metadata",
            "strategies": ["auto", "fixed_size", "layout_aware", "hybrid", "paragraph_aware"],
            "reads_original_file": False,
        },
        "stage_3": {"name": "structured preview", "input": "schedule CSV", "output": "typed records with source metadata", "chunking_required": False},
        "local_storage_cli": "Separate authenticated local operator process; not mounted in this public demo",
        "out_of_scope": ["public ingestion", "public review", "LLM answers", "unverified chart interpretation"],
    }


@router.post(
    "/parse",
    response_model=ParseResponse,
    response_model_exclude_none=True,
    summary="Normalize one supported file to Markdown",
)
async def parse_preview(
    file: UploadFile = File(...), schedule_kind: str = Form("auto")
) -> ParseResponse:
    path, original_name, _ = await _save_upload(file)
    try:
        try:
            result = await asyncio.to_thread(
                parse_to_markdown,
                path,
                original_name,
                schedule_kind,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        warnings = [ParseWarningOut.model_validate(asdict(item)) for item in result.warnings]
        return ParseResponse(
            filename=result.filename,
            document_type=result.document_type,
            strategy_used=result.strategy_used,
            markdown=result.markdown,
            markdown_character_count=len(result.markdown),
            warning_count=len(warnings),
            warnings=warnings,
            evidence_diagnostics=[asdict(item) for item in result.evidence_diagnostics],
            metadata=asdict(result.metadata),
        )
    finally:
        path.unlink(missing_ok=True)


@router.post(
    "/schedules/preview",
    response_model=SchedulePreviewResponse,
    response_model_exclude_none=True,
    summary="Preview and filter typed records from a schedule CSV",
)
async def schedule_preview(
    file: UploadFile = File(...),
    technology_nm: int | None = Query(default=None, ge=0),
    flavor: str | None = None,
    tapeout_on_or_after: str | None = None,
    schedule_kind: str = Form("auto"),
) -> SchedulePreviewResponse:
    path, original_name, suffix = await _save_upload(file)
    try:
        if suffix != ".csv":
            raise HTTPException(status_code=415, detail="A schedule CSV is required")
        try:
            # This public endpoint previews rows without saving them to the database
            records = await asyncio.to_thread(parse_schedule_records, path)
            kind = resolve_schedule_kind(original_name, schedule_kind)
            if kind is None:
                raise ValueError("Schedule type is unknown. Select Full or Shared for this CSV.")
            digest_builder = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest_builder.update(block)
            digest = digest_builder.hexdigest()
            source_id = make_source_id(digest, kind)
            kind_source = "filename" if resolve_schedule_kind(original_name) == kind else "user"
            matched = filter_schedule_records(
                records, technology_nm, flavor, tapeout_on_or_after
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return SchedulePreviewResponse(
            source_file=original_name,
            schedule_kind=kind,
            total_records=len(records),
            matched_records=len(matched),
            records=[{
                "schedule_kind": kind,
                "source_id": source_id,
                "record_id": f"{source_id}:row-{record['source_row_number']}",
                "source_file": original_name,
                **record,
            } for record in matched],
            warnings=[],
            source_id=source_id,
            schedule_kind_source=kind_source,
        )
    finally:
        path.unlink(missing_ok=True)


@router.post(
    "/chunk",
    response_model=ChunkResponse,
    response_model_exclude_none=True,
    summary="Create comparable chunks from normalized Markdown",
)
async def chunk_preview(request: ChunkRequest) -> ChunkResponse:
    source_file = Path(request.source_file).name or "normalized.md"
    try:
        # The chunker reads normalized Markdown and optional parse metadata
        metadata = DocumentMetadata(**request.metadata.model_dump()) if request.metadata else None
        result = await asyncio.to_thread(
            chunk_markdown,
            request.markdown,
            source_file,
            request.max_chars,
            request.strategy,
            request.overlap_chars,
            metadata,
            request.semantic_schedule_rows,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    chunks = [
        MarkdownChunkOut(**chunk_to_record(chunk))
        for chunk in result.chunks
    ]
    warnings = [ParseWarningOut.model_validate(asdict(item)) for item in result.warnings]
    return ChunkResponse(
        source_file=source_file,
        max_chars=request.max_chars,
        strategy=request.strategy,
        content_route=result.content_route,
        overlap_chars=request.overlap_chars if request.strategy in {"auto", "hybrid"} else 0,
        chunk_count=len(chunks),
        warning_count=len(warnings),
        chunks=chunks,
        warnings=warnings,
        source_id=metadata.source_id if metadata else (chunks[0].source_id if chunks else ""),
    )


@router.post("/compare", summary="Compare chunk strategies on the same Markdown", include_in_schema=False)
async def compare_preview(request: CompareRequest) -> dict:
    try:
        return await asyncio.to_thread(
            compare_strategies,
            request.markdown,
            [case.model_dump() for case in request.cases],
            Path(request.source_file).name or "normalized.md",
            request.max_chars,
            request.overlap_chars,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
