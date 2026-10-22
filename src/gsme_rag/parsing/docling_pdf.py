"""Optional layout and OCR backend. The normalized Markdown remains the chunker input."""

from pathlib import Path
from collections import Counter
import re

import pymupdf as fitz

from gsme_rag.models import ParsedDocument, ParseWarning
from gsme_rag.parsing.markdown import finish_markdown, table_requires_review
from gsme_rag.text import normalize_inline


def _normalize_document(document, source_name: str, review_pages: set[int],
                        visual_review_pages: set[int] | None = None) -> ParsedDocument:
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
    # Docling can emit a caption before or after its picture. Collect these up
    # front so the separate caption item cannot duplicate the figure caption.
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
            # A figure is local uncertain evidence. Do not make otherwise
            # selectable prose and tables on its entire page untrusted.
            page_review_open = page in review_pages
            if page_review_open:
                parts.append("<!-- review-required: ocr -->")
                if page in review_pages:
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
            # Repeated logos and other small page furniture are often labelled
            # as pictures. They are not missing semantic evidence.
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
    # Import here so DOCX, CSV, and the lightweight PDF fallback work without ML dependencies.
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    # Docling handles layout and tables. Raster OCR is supplemented afterward
    # with the bounded local helper, which avoids Docling's unconditional OSD
    # pass over sparse regions and preserves inspectable OCR conflicts.
    options.do_ocr = False
    options.do_table_structure = True
    # Device defaults to auto; CPU works without a GPU. Set DOCLING_DEVICE=cuda later.
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options)
    })
    conversion = converter.convert(path, raises_on_error=True)
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
        # Ignore images repeated throughout the document (the manual logo is
        # present on all 175 pages) and tiny decorative assets. A unique image
        # covering at least 2% of a page is conservatively substantive.
        visual_review_pages = {
            page_number
            for page_number, page_area, info in image_occurrences
            if fitz.Rect(info["bbox"]).get_area() >= page_area * .02
            and digest_counts[info.get("digest")] <= max(2, page_count // 20)
        }
        # A selectable formula or chart label is still unsafe when it shares a
        # page with visual evidence: symbol fidelity and spatial relationships
        # are not verified by text extraction alone. Keep this page-level state
        # distinct from ordinary image pages so surrounding prose on page 2
        # remains trusted.
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
        review_pages | visual_review_pages_with_numeric_evidence,
        visual_review_pages,
    )
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
