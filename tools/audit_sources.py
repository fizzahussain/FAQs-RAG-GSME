"""Create local parse/chunk evidence for a source file or representative PDF pages.

Run outputs belong in ignored validation/. No embedding provider is called.
"""

from argparse import ArgumentParser
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import time

import pymupdf as fitz

from gsme_rag.parsing import parse_to_markdown
from gsme_rag.chunking import chunk_markdown
from gsme_rag.models import chunk_to_record
from gsme_rag.parsing.profile import source_profile
from gsme_rag.storage.files import checksum


def audit(source: Path, output: Path, pages: list[int] | None = None,
          parse_pages: list[int] | None = None) -> dict:
    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    parse_input = source
    if parse_pages:
        if source.suffix.lower() != ".pdf":
            raise ValueError("--parse-pages requires a PDF")
        parse_input = output / (source.stem + "_selected-pages.pdf")
        with fitz.open(source) as original, fitz.open() as selected:
            for number in parse_pages:
                if not 1 <= number <= len(original):
                    raise ValueError("Selected source page is out of range")
                selected.insert_pdf(original, from_page=number - 1, to_page=number - 1)
            selected.save(parse_input)
    parsed = parse_to_markdown(parse_input)
    result = chunk_markdown(parsed.markdown, parsed.filename, metadata=parsed.metadata)
    (output / "normalized.md").write_text(parsed.markdown, encoding="utf-8")
    record = {"source": str(source), "strategy_used": parsed.strategy_used, "environment": source_profile(parse_input),
              "metadata": asdict(parsed.metadata), "warnings": [asdict(w) for w in parsed.warnings],
              "chunk_warnings": [asdict(w) for w in result.warnings],
              "diagnostics": [asdict(d) for d in parsed.evidence_diagnostics],
              "content_route": result.content_route, "chunks": [chunk_to_record(c) for c in result.chunks]}
    if parse_pages:
        record["parent_source_sha256"] = checksum(source)
        record["page_map"] = {str(index): number for index, number in enumerate(parse_pages, start=1)}
    (output / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if source.suffix.lower() == ".pdf" and pages:
        with fitz.open(source) as pdf:
            for number in pages:
                pdf[number - 1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).save(output / f"page-{number}.png")
    return {"file": source.name, "strategy_used": parsed.strategy_used, "route": result.content_route,
            "chunks": len(result.chunks), "review_required": sum(c.review_required for c in result.chunks),
            "excluded": sum(c.evidence_status == "excluded" for c in result.chunks),
            "types": dict(Counter(c.chunk_type for c in result.chunks)),
            "warnings": dict(Counter(w.code for w in parsed.warnings)),
            "oversized": sum(len(c.text) > 1800 for c in result.chunks),
            "seconds": round(time.perf_counter() - started, 3)}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path("validation/audit"))
    parser.add_argument("--backend", choices=("native", "docling"), default="native")
    parser.add_argument("--render-pages", type=int, nargs="*")
    parser.add_argument("--parse-pages", type=int, nargs="+", help="Audit a PDF slice; records its parent hash and original-page map")
    args = parser.parse_args()
    os.environ["GSME_PDF_BACKEND"] = args.backend
    summaries = []
    for source in args.sources:
        try:
            summary = audit(source, args.output / (source.stem + "_" + source.suffix[1:]), args.render_pages, args.parse_pages)
        except (ValueError, OSError) as error:
            summary = {"file": source.name, "error": str(error)}
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=True), flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
