"""Optional layout and OCR backend. The normalized Markdown remains the chunker input."""

from pathlib import Path
from collections import Counter
import hashlib
import re
from threading import Lock

import pymupdf as fitz

from gsme_rag.models import EvidenceDiagnostic, ParsedDocument, ParseWarning
from gsme_rag.parsing.markdown import finish_markdown, table_requires_review
from gsme_rag.parsing.fidelity import inspect_pdf_fidelity
from gsme_rag.text import normalize_inline


_MATCHER_LOCK = Lock()


def _capture_unmatched_cells(converter, path: Path):
    """Retain raw TableFormer cells even if its grid matcher discards them."""
    from docling_ibm_models.tableformer.data_management.matching_post_processor import MatchingPostProcessor

    original = MatchingPostProcessor.process
    losses = []

    def capturing_process(self, details, correct_overlapping_cells=False):
        result = original(self, details, correct_overlapping_cells)
        matches = {str(key) for key in result.get("matches", {})}
        missing = [dict(cell) for cell in result.get("pdf_cells", [])
                   if str(cell.get("id")) not in matches]
        if missing:
            table_cells = result.get("table_cells", [])
            losses.append({
                "bbox": result.get("table_bbox"),
                "page_width": result.get("page_width"),
                "page_height": result.get("page_height"),
                "pdf_cell_count": len(result.get("pdf_cells", [])),
                "grid_rows": max((cell.get("row_id", -1) for cell in table_cells), default=-1) + 1,
                "grid_columns": max((cell.get("column_id", -1) for cell in table_cells), default=-1) + 1,
                "missing_cells": missing,
                "all_cells": [dict(cell) for cell in result.get("pdf_cells", [])],
                "matched_cell_ids": sorted(matches),
            })
        return result

    with _MATCHER_LOCK:
        MatchingPostProcessor.process = capturing_process
        try:
            conversion = converter.convert(path, raises_on_error=True)
        finally:
            MatchingPostProcessor.process = original
    return conversion, losses


def _locate_cell_loss(source, loss):
    """Match a TableFormer grid to the source page using its raw cell text."""
    tokens = [normalize_inline(cell.get("text", "")) for cell in loss["missing_cells"]]
    tokens = [value for value in tokens if len(value) >= 5 and not set(value) <= {"."}]
    scores = []
    for number, page in enumerate(source, start=1):
        page_text = page.get_text("text").casefold()
        scores.append((sum(value.casefold() in page_text for value in tokens), number))
    score, page_number = max(scores, default=(0, None))
    return page_number if score >= 2 else None


def _cell_loss_evidence(source, losses):
    evidence = []
    warnings = []
    pages = set()
    for loss in losses:
        page_number = _locate_cell_loss(source, loss)
        if page_number is None:
            warnings.append(ParseWarning(
                "docling_table_cells_dropped", "Grid cell loss could not be assigned to a source page; review the conversion log"))
            continue
        pages.add(page_number)
        page = source[page_number - 1]
        sx = page.rect.width / loss["page_width"]
        sy = page.rect.height / loss["page_height"]
        grid = loss["bbox"]
        grid_bbox = tuple(round(value * (sx if i % 2 == 0 else sy), 2)
                          for i, value in enumerate(grid)) if grid else None
        warnings.append(ParseWarning(
            "docling_table_cells_dropped",
            f"Docling dropped {len(loss['missing_cells'])} of {loss['pdf_cell_count']} PDF cells "
            f"from {loss.get('grid_rows', '?')}x{loss.get('grid_columns', '?')} grid {grid_bbox}; "
            "review source and recovered text before approval",
            f"page {page_number}; bbox {grid_bbox}",
        ))
        dropped_ids = {str(cell.get("id")) for cell in loss["missing_cells"]}
        for cell in loss["all_cells"]:
            raw_bbox = cell.get("bbox")
            bbox = tuple(round(value * (sx if i % 2 == 0 else sy), 2)
                         for i, value in enumerate(raw_bbox)) if raw_bbox else None
            raw = str(cell.get("text", ""))
            identifier = hashlib.sha256(
                f"table-cell-drop|{page_number}|{grid_bbox}|{cell.get('id')}|{bbox}|{raw}".encode()
            ).hexdigest()[:20]
            evidence.append(EvidenceDiagnostic(
                f"evidence-{identifier}",
                "table_cell_drop" if str(cell.get("id")) in dropped_ids else "table_grid_cell",
                page_number, bbox, raw, None,
                (f"Docling grid matcher dropped cell {cell.get('id')} from {grid_bbox}"
                 if str(cell.get("id")) in dropped_ids else
                 f"Raw source cell {cell.get('id')} from grid {grid_bbox}"),
            ))
    return evidence, warnings, pages


def _chart_label_evidence(source, visual_pages: set[int]):
    evidence = []
    for page_number in sorted(visual_pages):
        page = source[page_number - 1]
        labels = re.findall(r"\b\d+(?:\.\d+)?%", page.get_text("text"))
        if len(labels) < 3:
            continue
        boxes = [box for label in labels for box in page.search_for(label)]
        bbox = (tuple(round(value, 2) for value in
                      (max(page.rect.x0, min(box.x0 for box in boxes) - 75),
                       max(page.rect.y0, min(box.y0 for box in boxes) - 70),
                       min(page.rect.x1, max(box.x1 for box in boxes) + 75),
                       min(page.rect.y1, max(box.y1 for box in boxes) + 45)))
                if boxes else None)
        raw = ", ".join(labels)
        digest = hashlib.sha256(f"chart|{page_number}|{bbox}|{raw}".encode()).hexdigest()[:20]
        evidence.append(EvidenceDiagnostic(
            f"evidence-{digest}", "chart_labels_unmapped", page_number, bbox,
            raw, None, "Chart label text is present but category-to-percentage associations are unverified",
        ))
    return evidence


def _normalize_document(document, source_name: str, review_pages: set[int],
                        visual_review_pages: set[int] | None = None,
                        ocr_review_pages: set[int] | None = None) -> ParsedDocument:
    parts: list[str] = []
    warnings: list[ParseWarning] = []
    active_page: int | None = None
    active_picture_level: int | None = None
    picture_review_open = False
    page_review_open = False
    reported_ocr_pages: set[int] = set()
    picture_count = 0
    last_header_part: int | None = None
    last_header_page: int | None = None
    # Match captions before collecting pictures to avoid duplicates
    emitted_picture_captions = {
        caption.casefold()
        for item, _ in document.iterate_items()
        if str(getattr(getattr(item, "label", ""), "value", getattr(item, "label", ""))).lower()
        in {"picture", "chart"}
        and (caption := normalize_inline(item.caption_text(document)))
    }
    emitted_table_captions: set[tuple[int | None, str]] = set()
    for candidate, _ in document.iterate_items():
        candidate_label = str(getattr(getattr(candidate, "label", ""), "value",
                                      getattr(candidate, "label", ""))).lower()
        if candidate_label != "table":
            continue
        candidate_prov = getattr(candidate, "prov", None) or []
        candidate_page = candidate_prov[0].page_no if candidate_prov else None
        for table_line in candidate.export_to_markdown(doc=document).splitlines():
            table_line = normalize_inline(table_line)
            if not table_line:
                continue
            if table_line.startswith("|"):
                break
            emitted_table_captions.add((candidate_page, table_line.casefold()))
    rendered_picture_captions: set[tuple[int | None, str]] = set()
    reported_figure_pages: set[int | None] = set()
    reported_visual_pages: set[int | None] = set()
    filter_visuals = visual_review_pages is not None
    visual_review_pages = visual_review_pages or set()
    ocr_review_pages = review_pages if ocr_review_pages is None else ocr_review_pages

    for item, level in document.iterate_items():
        if active_picture_level is not None and level <= active_picture_level:
            if picture_review_open:
                parts.append("<!-- review-end -->")
            picture_review_open = False
            active_picture_level = None

        provenance = getattr(item, "prov", None) or []
        page = provenance[0].page_no if provenance else active_page
        if page is not None and page != active_page:
            if page_review_open:
                parts.append("<!-- review-end -->")
            active_page = page
            last_header_part = None
            last_header_page = None
            parts.append(f"<!-- page: {page} -->")
            # A figure review marker should not cover unrelated page text
            page_review_open = page in review_pages
            if page_review_open:
                reason = "ocr" if page in ocr_review_pages else "source-evidence"
                parts.append(f"<!-- review-required: {reason} -->")
                if page in ocr_review_pages:
                    warnings.append(ParseWarning(
                        code="ocr_needs_review",
                        message="Image-dominant page may contain OCR errors; check values against the PDF",
                        location=f"page {page}",
                    ))
                    reported_ocr_pages.add(page)

        label = str(getattr(getattr(item, "label", ""), "value", getattr(item, "label", ""))).lower()
        if label in {"page_header", "page_footer"}:
            continue
        if label == "table":
            table = item.export_to_markdown(doc=document).strip()
            if table:
                formula_review = table_requires_review(table)
                if not page_review_open:
                    parts.append("<!-- review-required: docling-table -->")
                parts.append(table)
                if not page_review_open:
                    parts.append("<!-- review-end -->")
                warnings.append(ParseWarning(
                    code="table_needs_review",
                    message="Docling reconstructed table cells and reading order; verify them against the PDF",
                    location=f"page {page}" if page else None,
                ))
                if formula_review:
                    warnings.append(ParseWarning(
                        code="formula_table_needs_review",
                        message="Formula symbols or values in this table need source review",
                        location=f"page {page}" if page else None,
                    ))
            last_header_part = None
            last_header_page = None
            continue
        if label in {"picture", "chart"}:
            # Repeated logos are page furniture rather than missing evidence
            if filter_visuals and page not in visual_review_pages:
                continue
            picture_count += 1
            active_picture_level = level
            caption = normalize_inline(item.caption_text(document))
            caption_key = (page, caption.casefold())
            if caption and caption_key not in rendered_picture_captions:
                if not page_review_open:
                    parts.append("<!-- review-required: figure -->")
                parts.append(caption)
                if not page_review_open:
                    parts.append("<!-- review-end -->")
                rendered_picture_captions.add(caption_key)
            if page not in reported_figure_pages:
                warnings.append(ParseWarning(
                    code="figure_needs_review",
                    message="A figure may contain information absent from its caption; review the source image",
                    location=f"page {page}" if page else f"figure {picture_count}",
                ))
                reported_figure_pages.add(page)
            if page not in reported_visual_pages:
                if not page_review_open:
                    parts.append("<!-- review-required: visual-evidence -->")
                parts.append(f"Visual evidence on page {page or 'unknown'} requires source review; no verified description is available.")
                if not page_review_open:
                    parts.append("<!-- review-end -->")
                warnings.append(ParseWarning(
                    code="visual_evidence_missing",
                    message="Figure relationships are not verified by OCR or its caption",
                    location=f"page {page}" if page else f"figure {picture_count}",
                ))
                reported_visual_pages.add(page)
            last_header_part = None
            last_header_page = None
            continue

        value = normalize_inline(getattr(item, "text", ""))
        if not value:
            continue
        if (label in {"caption", "text"} and value.casefold() in emitted_picture_captions
                or (page, value.casefold()) in emitted_table_captions):
            continue
        if active_picture_level is not None and level > active_picture_level and not page_review_open:
            if not picture_review_open:
                parts.append("<!-- review-required: ocr -->")
                picture_review_open = True
            if page is not None and page not in reported_ocr_pages:
                warnings.append(ParseWarning(
                    code="ocr_needs_review", message="OCR inside an image needs source review",
                    location=f"page {page}",
                ))
                reported_ocr_pages.add(page)
        if label == "title":
            parts.append(f"# {value}")
        elif label == "section_header":
            if last_header_part is not None and last_header_page == page:
                previous = parts[last_header_part]
                if previous.startswith("## "):
                    parts[last_header_part] = f"**{previous[3:]}**"
            prefix = "#" if re.match(r"^chapter\s+\d+\b", value, re.I) else "##"
            parts.append(f"{prefix} {value}")
            last_header_part = len(parts) - 1
            last_header_page = page
        else:
            parts.append(value)
            last_header_part = None
            last_header_page = None

    if picture_review_open or page_review_open:
        parts.append("<!-- review-end -->")
    markdown = finish_markdown(parts)
    if not any(part for part in parts if not part.startswith("<!--")):
        warnings.append(ParseWarning(code="no_pdf_text", message="No text or table was extracted"))
    return ParsedDocument(
        filename=source_name, document_type="pdf", strategy_used="docling_layout_ocr_to_markdown",
        markdown=markdown, warnings=warnings,
    )


def parse_docling_pdf(path: Path, source_name: str) -> ParsedDocument:
    # Load Docling only when this PDF backend is selected
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    # Local OCR checks sparse image text after Docling finds layout and tables
    options.do_ocr = False
    options.do_table_structure = True
    # Auto device works on CPU and can use GPU when available
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options)
    })
    conversion, cell_losses = _capture_unmatched_cells(converter, path)
    document = conversion.document
    with fitz.open(path) as source:
        page_count = len(source)
        page_text = {index: page.get_text("text") for index, page in enumerate(source, start=1)}
        review_pages = {
            index for index, page in enumerate(source, start=1)
            if len(page.get_text("text").strip()) < 90 and page.get_images()
        }
        image_occurrences = [
            (page_number, page.rect.get_area(), info)
            for page_number, page in enumerate(source, start=1)
            for info in page.get_image_info(hashes=True, xrefs=True)
        ]
        digest_counts = Counter(info.get("digest") for _, _, info in image_occurrences)
        # Skip repeated logos and tiny decorative images
        visual_review_pages = {
            page_number
            for page_number, page_area, info in image_occurrences
            if fitz.Rect(info["bbox"]).get_area() >= page_area * .02
            and digest_counts[info.get("digest")] <= max(2, page_count // 20)
        }
        cell_evidence, cell_warnings, cell_review_pages = _cell_loss_evidence(source, cell_losses)
        fidelity_evidence, fidelity_warnings, fidelity_review_pages = inspect_pdf_fidelity(source)
        chart_evidence = _chart_label_evidence(source, visual_review_pages)
        # Selected text does not verify formulas or chart relationships
    for item, _ in document.iterate_items():
        label = str(getattr(getattr(item, "label", ""), "value", getattr(item, "label", ""))).lower()
        provenance = getattr(item, "prov", None) or []
        if label not in {"picture", "chart"} or not provenance:
            continue
        page = provenance[0].page_no
        caption = normalize_inline(item.caption_text(document))
        if caption:
            visual_review_pages.add(page)
    visual_review_pages_with_numeric_evidence = {
        page_number
        for page_number, text in page_text.items()
        if page_number in visual_review_pages
        and (
            len(re.findall(r"\b\d+(?:\.\d+)?(?:%|\s+percent\b)", text, re.I)) >= 2
            or "=" in text
        )
    }
    result = _normalize_document(
        document,
        source_name,
        review_pages | visual_review_pages_with_numeric_evidence | cell_review_pages | fidelity_review_pages,
        visual_review_pages,
        ocr_review_pages=review_pages,
    )
    result.evidence_diagnostics.extend(cell_evidence)
    result.evidence_diagnostics.extend(fidelity_evidence)
    result.evidence_diagnostics.extend(chart_evidence)
    result.warnings.extend(cell_warnings)
    result.warnings.extend(fidelity_warnings)
    result.warnings.extend(ParseWarning(
        "chart_mapping_unverified",
        "Numeric labels are visible, but their category associations have not been verified against the chart",
        f"page {item.page}; bbox {item.bbox}",
    ) for item in chart_evidence)
    for page in review_pages:
        match = re.search(
            rf"<!-- page: {page} -->(.*?)(?=<!-- page: \d+ -->|\Z)",
            result.markdown, flags=re.DOTALL,
        )
        content = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.DOTALL).strip() if match else ""
        content = re.sub(r"(?m)^Figure:.*$", "", content).strip()
        if not content:
            result.warnings.append(ParseWarning(
                code="ocr_page_empty",
                message="Docling returned no readable text for an image-dominant page",
                location=f"page {page}",
            ))
    conversion_status = getattr(getattr(conversion, "status", None), "value", None)
    if conversion_status and str(conversion_status).lower() != "success":
        result.warnings.append(ParseWarning(
            code="partial_pdf_conversion",
            message=f"Docling conversion status is {conversion_status}; check for missing pages",
        ))
    if hasattr(document, "pages") and len(document.pages) < page_count:
        result.warnings.append(ParseWarning(
            code="partial_pdf_conversion",
            message=f"Only {len(document.pages)} of {page_count} PDF pages were converted",
        ))
    return result
