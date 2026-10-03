from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from gsme_rag.api.schemas import (
    ChunkRequest,
    ChunkResponse,
    MarkdownChunkOut,
    ParseResponse,
    ParseWarningOut,
)
from gsme_rag.chunking import chunk_markdown
from gsme_rag.parsing import parse_to_markdown


router = APIRouter(prefix="/v1")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".pdf", ".csv"}


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


@router.get("/architecture", summary="Show the separated processing stages")
async def architecture() -> dict:
    return {
        "stage_1": {
            "name": "parser",
            "input": ["DOCX", "born-digital PDF", "CSV"],
            "output": "normalized Markdown",
            "makes_chunk_decisions": False,
        },
        "stage_2": {
            "name": "chunker",
            "input": "normalized Markdown",
            "output": "layout-aware chunks with source metadata",
            "reads_original_file": False,
        },
        "out_of_scope": ["OCR", "database storage", "search", "embeddings"],
    }


@router.post(
    "/parse",
    response_model=ParseResponse,
    summary="Normalize one supported file to Markdown",
)
async def parse_preview(file: UploadFile = File(...)) -> ParseResponse:
    path, original_name, _ = await _save_upload(file)
    try:
        try:
            result = await asyncio.to_thread(
                parse_to_markdown,
                path,
                original_name,
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
        )
    finally:
        path.unlink(missing_ok=True)


@router.post(
    "/chunk",
    response_model=ChunkResponse,
    summary="Create layout-aware chunks from normalized Markdown",
)
async def chunk_preview(request: ChunkRequest) -> ChunkResponse:
    source_file = Path(request.source_file).name or "normalized.md"
    try:
        result = await asyncio.to_thread(
            chunk_markdown,
            request.markdown,
            source_file,
            request.max_chars,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    chunks = [
        MarkdownChunkOut(
            **asdict(chunk),
            character_count=chunk.character_count,
        )
        for chunk in result.chunks
    ]
    warnings = [ParseWarningOut.model_validate(asdict(item)) for item in result.warnings]
    return ChunkResponse(
        source_file=source_file,
        max_chars=request.max_chars,
        chunk_count=len(chunks),
        warning_count=len(warnings),
        chunks=chunks,
        warnings=warnings,
    )
