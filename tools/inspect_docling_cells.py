"""Inspect Docling table cell loss without modifying its matching algorithm."""

from argparse import ArgumentParser
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_ibm_models.tableformer.data_management.matching_post_processor import MatchingPostProcessor


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    records = []
    original = MatchingPostProcessor.process

    def inspect(self, details, correct_overlapping_cells=False):
        result = original(self, details, correct_overlapping_cells)
        cells = result.get("pdf_cells", [])
        matches = result.get("matches", {})
        missing = [cell for cell in cells if str(cell["id"]) not in matches]
        if missing:
            records.append({
                "table_bbox": result.get("table_bbox"),
                "page_height": result.get("page_height"),
                "page_width": result.get("page_width"),
                "pdf_cell_count": len(cells),
                "table_cell_count": len(result.get("table_cells", [])),
                "missing_cells": missing,
                "all_pdf_cells": cells,
                "matched_cell_ids": sorted(matches, key=str),
            })
        return result

    MatchingPostProcessor.process = inspect
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    started = perf_counter()
    try:
        converter = DocumentConverter(format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options)
        })
        conversion = converter.convert(args.pdf, raises_on_error=True)
    finally:
        MatchingPostProcessor.process = original
    tables = []
    for item, _ in conversion.document.iterate_items():
        if str(getattr(getattr(item, "label", ""), "value", getattr(item, "label", ""))).lower() != "table":
            continue
        provenance = getattr(item, "prov", None) or []
        tables.append({
            "page": provenance[0].page_no if provenance else None,
            "bbox": provenance[0].bbox.model_dump() if provenance and hasattr(provenance[0].bbox, "model_dump") else None,
            "markdown": item.export_to_markdown(doc=conversion.document),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "pdf": str(args.pdf), "elapsed_seconds": round(perf_counter() - started, 3),
        "losses": records, "tables": tables,
    }, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"elapsed_seconds": round(perf_counter() - started, 3),
                      "loss_events": len(records),
                      "missing_counts": [len(record["missing_cells"]) for record in records],
                      "table_count": len(tables)}, indent=2))


if __name__ == "__main__":
    main()
