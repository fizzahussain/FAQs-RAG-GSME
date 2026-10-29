"""Record one PDF backend run with parser, environment, and chunk evidence."""

from argparse import ArgumentParser
from dataclasses import asdict
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from gsme_rag.chunking import chunk_markdown
from gsme_rag.models import chunk_to_record
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.ocr import find_tesseract


def optional_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--backend", choices=["native", "docling"], required=True)
    args = parser.parse_args()

    os.environ["GSME_PDF_BACKEND"] = args.backend
    started = time.perf_counter()
    parsed = parse_to_markdown(args.pdf)
    chunks = chunk_markdown(
        parsed.markdown, parsed.filename, metadata=parsed.metadata,
    )
    elapsed = time.perf_counter() - started

    tesseract = find_tesseract()
    languages: list[str] = []
    if tesseract:
        result = subprocess.run(
            [tesseract, "--list-langs"], capture_output=True, text=True,
            timeout=15, check=False,
        )
        languages = [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "normalized.md").write_text(parsed.markdown, encoding="utf-8")
    record = {
        "requested_backend": args.backend,
        "strategy_used": parsed.strategy_used,
        "elapsed_seconds": round(elapsed, 3),
        "environment": {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "gsme_rag_version": importlib.metadata.version("gsme-rag"),
            "pymupdf_version": importlib.metadata.version("PyMuPDF"),
            "docling_version": optional_version("docling"),
            "tesseract_executable": tesseract,
            "tesseract_languages": languages,
            "docling_device": os.getenv("DOCLING_DEVICE"),
        },
        "metadata": asdict(parsed.metadata) if parsed.metadata else None,
        "parse_warnings": [asdict(warning) for warning in parsed.warnings],
        "evidence_diagnostics": [asdict(item) for item in parsed.evidence_diagnostics],
        "content_route": chunks.content_route,
        "chunk_count": len(chunks.chunks),
        "chunk_warnings": [asdict(warning) for warning in chunks.warnings],
        "chunks": [chunk_to_record(chunk) for chunk in chunks.chunks],
    }
    (args.output / "validation.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8",
    )
    print(json.dumps({
        "requested_backend": args.backend,
        "strategy_used": parsed.strategy_used,
        "warnings": [warning.code for warning in parsed.warnings],
        "content_route": chunks.content_route,
        "chunk_count": len(chunks.chunks),
        "elapsed_seconds": round(elapsed, 3),
    }, indent=2))


if __name__ == "__main__":
    main()
