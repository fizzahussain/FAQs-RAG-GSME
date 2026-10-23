"""Compare the source TOC grid and exported/recovered Docling evidence row by row."""

from argparse import ArgumentParser
import hashlib
import json
from pathlib import Path
import re

import pymupdf as fitz


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\.{2,}", " ", value)).strip().casefold()


def _punctuation_fold(value: str) -> str:
    return value.replace("’", "'").replace("‘", "'")


def audit(pdf: Path, diagnostic: Path, markdown: Path) -> dict:
    data = json.loads(diagnostic.read_text(encoding="utf-8"))
    losses = [item for item in data["losses"] if item["pdf_cell_count"] == 183]
    if len(losses) != 1:
        raise ValueError("Expected the single 183-cell warning from the supplied manual")
    with fitz.open(pdf) as source:
        page = source[1]
        raw_rows = [block[4].strip() for block in page.get_text("blocks")
                    if 95 <= block[1] < 695 and block[0] >= 70]
    raw_rows[0] = raw_rows[0].replace("Table of Contents", "", 1).strip()
    normalized = markdown.read_text(encoding="utf-8")
    page_text = normalized.split("<!-- page: 2 -->", 1)[1].split("<!-- page: 3 -->", 1)[0]
    normalized_page = _normalize(page_text)
    exported_tables = [table for table in data["tables"] if table["page"] == 2]
    exported_text = _normalize(" ".join(table["markdown"] for table in exported_tables))
    dropped_ids = {cell["id"] for cell in losses[0]["missing_cells"]}
    cells = []
    for cell in losses[0]["all_pdf_cells"]:
        text = _normalize(cell["text"])
        cells.append({
            "cell_id": cell["id"], "bbox_tableformer": cell["bbox"],
            "raw_text": cell["text"], "dropped_by_matcher": cell["id"] in dropped_ids,
            "content_bearing": bool(text and not set(text) <= {"."}),
            "recovered_text_present": bool(text and text in normalized_page),
            "punctuation_equivalent_present": bool(text and
                _punctuation_fold(text) in _punctuation_fold(normalized_page)),
            "exported_table_text_present": bool(text and text in exported_text),
        })
    rows = []
    for number, raw in enumerate(raw_rows, start=1):
        row_text = _normalize(raw)
        match = re.match(r"(chapter\s+\d+|\d+(?:\.\d+)*\.?)\s+(.+?)\s+(\d+)$", row_text)
        rows.append({
            "row": number,
            "source_text": raw,
            "section": match.group(1) if match else None,
            "title": match.group(2) if match else None,
            "target_page": match.group(3) if match else None,
            "recovered_row_text_present": row_text in normalized_page,
            "exported_table_row_present": any(row_text in _normalize(table["markdown"])
                                              for table in exported_tables),
        })
    return {
        "content_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "normalized_sha256": hashlib.sha256(markdown.read_bytes()).hexdigest(),
        "source_page": 2,
        "grid_bbox_tableformer": losses[0]["table_bbox"],
        "grid_dimensions": "30x3",
        "pdf_cells": losses[0]["pdf_cell_count"],
        "dropped_cells": losses[0]["missing_cells"],
        "cells": cells,
        "exported_table_count": len(exported_tables),
        "rows": rows,
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("diagnostic", type=Path)
    parser.add_argument("markdown", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = audit(args.pdf, args.diagnostic, args.markdown)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "source_page": report["source_page"],
        "visible_rows": len(report["rows"]),
        "recovered_rows": sum(row["recovered_row_text_present"] for row in report["rows"]),
        "exported_table_rows": sum(row["exported_table_row_present"] for row in report["rows"]),
        "dropped_cells": len(report["dropped_cells"]),
        "raw_cells_audited": len(report["cells"]),
        "content_cells_recovered": sum(cell["recovered_text_present"] for cell in report["cells"]
                                       if cell["content_bearing"]),
        "content_cells_total": sum(cell["content_bearing"] for cell in report["cells"]),
        "punctuation_equivalent_cells": sum(cell["punctuation_equivalent_present"]
                                            for cell in report["cells"] if cell["content_bearing"]),
    }, indent=2))


if __name__ == "__main__":
    main()
