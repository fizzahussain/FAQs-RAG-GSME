from dataclasses import dataclass
from collections import Counter
from difflib import SequenceMatcher
import hashlib
from importlib.util import find_spec
import os
from pathlib import Path
import re
import shutil
from statistics import median

import pymupdf as fitz

from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table, table_requires_review
from gsme_rag.parsing.ocr import OcrUnavailable, ocr_image
from gsme_rag.models import ParseWarning, ParsedDocument
from gsme_rag.text import normalize_inline, normalize_multiline, split_question_number


@dataclass(slots=True)
class _Line:
    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold_share: float


def _extract_lines(document: fitz.Document) -> list[_Line]:
    lines: list[_Line] = []
    for page_number, page in enumerate(document, start=1):
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            for raw_line in block.get("lines", []):
                spans = raw_line.get("spans", [])
                text = normalize_inline("".join(span.get("text", "") for span in spans))
                if not text:
                    continue
                total_chars = sum(len(span.get("text", "")) for span in spans)
                bold_chars = sum(
                    len(span.get("text", ""))
                    for span in spans
                    if "bold" in span.get("font", "").lower()
                )
                sizes = [float(span.get("size", 0.0)) for span in spans if span.get("text")]
                bbox = raw_line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                lines.append(
                    _Line(
                        page=page_number,
                        text=text,
                        x0=float(bbox[0]),
                        y0=float(bbox[1]),
                        x1=float(bbox[2]),
                        y1=float(bbox[3]),
                        size=max(sizes, default=0.0),
                        bold_share=bold_chars / total_chars if total_chars else 0.0,
                    )
                )
    if len(document) < 2:
        return sorted(lines, key=lambda line: (line.page, round(line.y0, 1), line.x0))

    # Repeated margin text is source metadata, not page evidence. Require the
    # same line in a majority of pages and restrict removal to page margins.
    repeated: Counter[tuple[str, str]] = Counter()
    for line in lines:
        page_height = document[line.page - 1].rect.height
        zone = "top" if line.y0 < page_height * .11 else "bottom" if line.y0 > page_height * .88 else ""
        if zone:
            repeated[(zone, line.text.casefold())] += 1
    threshold = max(2, min(3, (len(document) + 1) // 2))
    filtered = []
    for line in lines:
        page_height = document[line.page - 1].rect.height
        zone = "top" if line.y0 < page_height * .11 else "bottom" if line.y0 > page_height * .88 else ""
        if zone == "bottom" and line.text == str(line.page):
            continue
        if zone and repeated[(zone, line.text.casefold())] >= threshold:
            continue
        filtered.append(line)
    return sorted(filtered, key=lambda line: (line.page, round(line.y0, 1), line.x0))


def _detect_table_layout(lines: list[_Line], page_width: float) -> bool:
    numbered_left = [
        line
        for line in lines
        if line.x0 < page_width * 0.42
        and line.bold_share >= 0.6
        and re.match(r"^\d{1,3}\s+", line.text)
    ]
    right_text = sum(1 for line in lines if line.x0 >= page_width * 0.33)
    # Numbered technical steps and two-column prose are not an FAQ table.
    question_lines = sum(
        "?" in line.text
        for line in lines
        if line.x0 < page_width * 0.42 and line.bold_share >= 0.6
    )
    has_column_headers = {line.text.casefold() for line in lines} >= {"question", "answer"}
    return (
        len(numbered_left) >= 3
        and right_text >= 6
        and (question_lines >= 2 or has_column_headers and question_lines >= 1)
    )


def _lines_to_text(lines: list[_Line]) -> str:
    return normalize_multiline("\n".join(line.text for line in lines))


def _table_markdown(lines: list[_Line], page_width: float) -> tuple[str, list[ParseWarning]]:
    split_x = page_width * 0.33
    warnings: list[ParseWarning] = []
    left_lines = [
        line
        for line in lines
        if line.x0 < split_x and line.text.casefold() not in {"question", "answer"}
    ]
    right_lines = [
        line
        for line in lines
        if line.x0 >= split_x and line.text.casefold() not in {"question", "answer"}
    ]

    events: list[dict] = []
    question_event: dict | None = None
    category = ""

    for line in left_lines:
        number, stripped = split_question_number(line.text)
        if number and line.bold_share >= 0.6:
            if question_event is not None:
                events.append(question_event)
            question_event = {
                "kind": "question",
                "page": line.page,
                "y": line.y0,
                "parts": [stripped],
                "category": category,
            }
            continue

        if question_event is not None and line.bold_share >= 0.6:
            question_event["parts"].append(line.text)
            if normalize_inline(" ".join(question_event["parts"])).endswith("?"):
                events.append(question_event)
                question_event = None
            continue

        if line.bold_share >= 0.6 and "?" not in line.text and len(line.text) <= 80:
            if question_event is not None:
                events.append(question_event)
                question_event = None
            category = line.text
            events.append(
                {
                    "kind": "category",
                    "page": line.page,
                    "y": line.y0,
                    "category": category,
                }
            )

    if question_event is not None:
        events.append(question_event)

    boundaries = sorted(events, key=lambda event: (event["page"], event["y"]))
    questions = [event for event in events if event["kind"] == "question"]
    if any(not question["category"] for question in questions):
        warnings.append(ParseWarning(
            code="category_missing",
            message="Some FAQ rows have no visible category; no category was invented",
        ))
    rows = [["Category", "Question", "Answer", "Page"]]

    for question in questions:
        start = (question["page"], question["y"])
        following = next(
            (event for event in boundaries if (event["page"], event["y"]) > start),
            None,
        )
        end = (
            (following["page"], following["y"])
            if following is not None
            else (10**9, float("inf"))
        )
        answer_lines = [
            line for line in right_lines if start <= (line.page, line.y0) < end
        ]
        question_text = normalize_inline(" ".join(question["parts"]))
        answer = _lines_to_text(answer_lines)
        if not answer:
            warnings.append(
                ParseWarning(
                    code="missing_table_answer",
                    message=f"No answer text was found beside: {question_text}",
                    location=f"page {question['page']}",
                )
            )
        rows.append(
            [
                question["category"],
                question_text,
                answer,
                (
                    str(question["page"])
                    if not answer_lines or answer_lines[-1].page == question["page"]
                    else f"{question['page']}-{answer_lines[-1].page}"
                ),
            ]
        )

    return render_markdown_table(rows), warnings


def _layout_markdown(lines: list[_Line], allow_visual_headings: bool = True) -> str:
    sizes = [line.size for line in lines if 7 <= line.size <= 18]
    body_size = median(sizes) if sizes else 11.0
    headings = [
        line for line in lines
        if line.bold_share >= 0.6
        and line.size >= body_size * 1.12
        and "?" not in line.text
        and len(line.text) <= 100
    ]
    has_document_title = (
        len(headings) > 1
        and headings[0].size >= body_size * 1.5
        and headings[0].size >= max(line.size for line in headings[1:]) * 1.2
    )
    parts: list[str] = []
    active_page: int | None = None
    first_heading_written = False
    pending_line: _Line | None = None
    pending_text: list[str] = []

    def flush_text() -> None:
        nonlocal pending_line, pending_text
        if pending_text:
            parts.append(" ".join(pending_text))
        pending_line = None
        pending_text = []

    for line in lines:
        if line.page != active_page:
            flush_text()
            active_page = line.page
            parts.append(f"<!-- page: {active_page} -->")

        is_heading = (
            line.bold_share >= 0.6
            and line.size >= body_size * 1.12
            and "?" not in line.text
            and len(line.text) <= 100
        )
        numbered_section = bool(re.match(r"^\d+(?:\.\d+){1,}\s+\S", line.text))
        chapter = bool(re.match(r"^Chapter\s+\d+\b", line.text, re.I))
        is_heading = (is_heading and allow_visual_headings or numbered_section or chapter
                      or line.text.casefold() == "table of contents")
        if is_heading:
            flush_text()
            level = 1 if not has_document_title or not first_heading_written else 2
            parts.append(f"{'#' * level} {line.text}")
            first_heading_written = True
        elif line.bold_share >= 0.6:
            flush_text()
            parts.append(f"**{line.text}**")
        else:
            if pending_line and line.y0 - pending_line.y0 <= max(line.size, pending_line.size) * 1.4:
                pending_text.append(line.text)
            else:
                flush_text()
                pending_text = [line.text]
            pending_line = line

    flush_text()
    return finish_markdown(parts)


def _native_markdown(document: fitz.Document, lines: list[_Line]) -> tuple[str, list[ParseWarning]]:
    """Keep detected tables intact when the optional Docling backend is unavailable."""
    parts: list[str] = []
    warnings: list[ParseWarning] = []
    image_cache: dict[str, str] = {}
    for page_no, page in enumerate(document, start=1):
        page_lines = [line for line in lines if line.page == page_no]
        tables = list(page.find_tables().tables)
        accepted_tables = []
        entries: list[tuple[float, str | _Line]] = []
        page_review_required = False
        for table in tables:
            cells = [[cell or "" for cell in row] for row in table.extract()]
            # A one-row region often contains a formula rather than a table.
            # Full-page detections can swallow captions, prose and other tables.
            oversized = (fitz.Rect(table.bbox).get_area() > page.rect.get_area() * .65
                         or max((len(cell) for row in cells for cell in row), default=0) > 600)
            if len(cells) < 2 or not any(cells[0]) or oversized:
                warnings.append(ParseWarning(
                    code="table_needs_review",
                    message="Table boundaries or header could not be reconstructed; source lines retained for review",
                    location=f"page {page_no}",
                ))
                page_review_required = True
                continue
            # PyMuPDF may infer spacer columns around each genuine column.
            keep = [col for col in range(max(map(len, cells)))
                    if any(col < len(row) and row[col].strip() for row in cells)]
            cells = [[row[col] if col < len(row) else "" for col in keep] for row in cells]
            rendered_table = render_markdown_table(cells)
            formula = table_requires_review(rendered_table)
            rendered_table = (
                f"<!-- review-required: {'formula-table' if formula else 'inferred-table'} -->\n\n"
                + rendered_table + "\n\n<!-- review-end -->"
            )
            warnings.append(ParseWarning(
                code="formula_table_needs_review" if formula else "table_needs_review",
                message=("Formula symbols or values in this table need source review" if formula
                         else "Inferred PDF table cells and reading order need source review"),
                location=f"page {page_no}",
            ))
            accepted_tables.append(table)
            entries.append((table.bbox[1], rendered_table))
        for line in page_lines:
            # Table cells are already in a table; do not repeat them as prose.
            in_table = any(
                table.bbox[0] <= (line.x0 + line.x1) / 2 <= table.bbox[2]
                and table.bbox[1] <= (line.y0 + line.y1) / 2 <= table.bbox[3]
                for table in accepted_tables
            )
            if not in_table:
                entries.append((line.y0, line))
        if len(" ".join(line.text for line in page_lines)) < 90 and page.get_images() and not accepted_tables:
            recovered = ""
            if shutil.which("tesseract"):
                try:
                    recovered = normalize_multiline(page.get_text("text", textpage=page.get_textpage_ocr(dpi=200)))
                except RuntimeError:
                    pass
                if len(recovered) <= len(" ".join(line.text for line in page_lines)) + 30:
                    try:
                        scale = min(2.0, (6_000_000 / max(page.rect.get_area(), 1)) ** .5)
                        pixels = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        recovered = ocr_image(pixels.tobytes("png"))
                    except (OcrUnavailable, ValueError, RuntimeError) as error:
                        warnings.append(ParseWarning(
                            code="image_ocr_unavailable", message=f"Could not OCR scanned page: {error}",
                            location=f"page {page_no}",
                        ))
                # Whole-page OCR can rediscover only the repeated page furniture.
                recovered = re.sub(r"(?im)^MANUFACTURING DOCUMENT DOC NO:.*(?:\n|$)", "", recovered)
                recovered = re.sub(r"(?im)^\d+\s+CONFIDENTIAL:.*(?:\n|$)", "", recovered).strip()
                native_length = len(" ".join(line.text for line in page_lines))
                if recovered.strip() and len(recovered) > native_length + (10 if native_length else 0):
                    entries = [(0, recovered)]  # avoid duplicate footer/text-layer lines
                    page_review_required = True
                    warnings.append(ParseWarning(
                        code="ocr_needs_review", message="OCR text may misread values or table cells; verify against the source image",
                        location=f"page {page_no}",
                    ))
            if not any(code.code == "ocr_needs_review" and code.location == f"page {page_no}" for code in warnings):
                warnings.append(ParseWarning(
                    code="image_text_needs_review", message="Image-dominant page may contain unread text or table cells",
                    location=f"page {page_no}",
                ))
        elif any(
            rect.get_area() > page.rect.get_area() * 0.18
            for image in page.get_images()
            for rect in page.get_image_rects(image[0])
        ):
            warnings.append(ParseWarning(
                code="figure_needs_review", message="A large image or chart may contain facts not present in extracted text",
                location=f"page {page_no}",
            ))
        # The page may have selectable prose and a separate scanned figure.
        # OCR large raster regions independently, keeping them at their page position.
        if len(" ".join(line.text for line in page_lines)) >= 90:
            image_count = 0
            for info in page.get_image_info(xrefs=True):
                rect = fitz.Rect(info["bbox"])
                if rect.get_area() < page.rect.get_area() * 0.08:
                    continue
                if any(fitz.Rect(table.bbox).intersects(rect) for table in accepted_tables):
                    continue
                if any(rect.contains(fitz.Point((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2))
                       for line in page_lines):
                    continue  # existing selectable layer already represents this region
                if image_count >= 6:
                    warnings.append(ParseWarning(
                        code="image_ocr_limit", message="Only six large raster regions per page are OCRed",
                        location=f"page {page_no}",
                    ))
                    break
                image_count += 1
                try:
                    image_bytes = (document.extract_image(info["xref"])["image"] if info.get("xref")
                                   else page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect).tobytes("png"))
                    digest = hashlib.sha256(image_bytes).hexdigest()
                    if digest not in image_cache:
                        image_cache[digest] = ocr_image(image_bytes)
                    recognized = image_cache[digest]
                except (OcrUnavailable, ValueError, KeyError, RuntimeError) as error:
                    warnings.append(ParseWarning(
                        code="image_ocr_unavailable", message=f"Could not read image text: {error}",
                        location=f"page {page_no}",
                    ))
                    continue
                if recognized:
                    entries.append((rect.y0, f"<!-- review-required: pdf-image-ocr -->\n\nImage text (OCR): {recognized}\n\n<!-- review-end -->"))
                    warnings.append(ParseWarning(
                        code="ocr_needs_review", message="Text in a PDF image needs source review before indexing",
                        location=f"page {page_no}",
                    ))
                else:
                    warnings.append(ParseWarning(
                        code="figure_needs_review", message="Raster figure has no readable OCR text; inspect the source image",
                        location=f"page {page_no}",
                    ))
        if not page_lines and not page.get_images() and page.get_drawings():
            warnings.append(ParseWarning(
                code="vector_content_needs_review",
                message="Page contains drawn content but no extracted text",
                location=f"page {page_no}",
            ))
        elif len(page.get_drawings()) >= 10 and not accepted_tables:
            warnings.append(ParseWarning(
                code="vector_content_needs_review",
                message="Drawn graphics may contain relationships missing from extracted text",
                location=f"page {page_no}",
            ))
        image_regions = [fitz.Rect(info["bbox"]) for info in page.get_image_info(xrefs=True)
                         if fitz.Rect(info["bbox"]).get_area() >= page.rect.get_area() * .08]
        vector_regions = []
        for drawing in page.get_drawings():
            rect = fitz.Rect(drawing["rect"])
            share = rect.get_area() / max(page.rect.get_area(), 1)
            if (.05 <= share < .65 and rect.y0 > page.rect.height * .10
                    and rect.y1 < page.rect.height * .88
                    and not any(fitz.Rect(table.bbox).contains(rect) for table in accepted_tables)):
                vector_regions.append(rect)
        percent_labels = sum(bool(re.search(r"\b\d+(?:\.\d+)?(?:%|\s+percent\b)", line.text, re.I))
                             for line in page_lines)
        chart_text = (percent_labels >= 2
                      and any(re.search(r"\bFigure\s+\d+", line.text, re.I) for line in page_lines)
                      and bool(vector_regions or len(page.get_images()) >= 3))
        if chart_text:
            # Chart labels without their spatial relationships are unsafe as
            # apparently ordinary prose or inferred Markdown headings.
            page_review_required = True
        visual_missing = bool(image_regions or vector_regions or chart_text)
        if visual_missing:
            warnings.append(ParseWarning(
                code="visual_evidence_missing",
                message="A figure or chart has no verified semantic description; do not infer its relationships from OCR labels",
                location=f"page {page_no}",
            ))
        parts.append(f"<!-- page: {page_no} -->")
        if page_review_required:
            parts.append("<!-- review-required: ocr-or-table -->")
        prose: list[_Line] = []
        def flush_prose() -> None:
            if prose:
                rendered = _layout_markdown(prose, allow_visual_headings=not chart_text)
                rendered = re.sub(r"^<!-- page: \d+ -->\s*", "", rendered).strip()
                parts.append(rendered)
                prose.clear()

        for _, value in sorted(entries, key=lambda entry: entry[0]):
            if isinstance(value, _Line):
                prose.append(value)
            else:
                flush_prose()
                parts.append(value)
        flush_prose()
        if page_review_required:
            parts.append("<!-- review-end -->")
        if visual_missing:
            parts.extend(("<!-- review-required: visual-evidence -->",
                          f"Visual evidence on page {page_no} requires source review; no verified description is available.",
                          "<!-- review-end -->"))
    figure_warnings = [warning for warning in warnings if warning.code == "figure_needs_review"]
    if len(figure_warnings) > 12:
        pages = [int(warning.location.split()[-1]) for warning in figure_warnings]
        ranges: list[str] = []
        start = end = pages[0]
        for page in pages[1:]:
            if page == end + 1:
                end = page
            else:
                ranges.append(str(start) if start == end else f"{start}-{end}")
                start = end = page
        ranges.append(str(start) if start == end else f"{start}-{end}")
        warnings = [warning for warning in warnings if warning.code != "figure_needs_review"]
        warnings.append(ParseWarning(
            code="figure_needs_review",
            message=f"{len(pages)} pages contain large images or charts; review them for facts missing from text",
            location="pages " + ", ".join(ranges),
        ))
    return finish_markdown(parts), warnings


def _plain_markdown_line(value: str) -> str:
    plain = re.sub(r"^#{1,6}\s+", "", value.strip())
    plain = re.sub(r"^(?:\*\*|__)", "", plain)
    plain = re.sub(r"(?:\*\*|__)$", "", plain)
    return normalize_inline(plain)


def _similar_ocr_line(segment: str, value: str) -> str | None:
    expected = normalize_inline(value)
    if len(expected) < 12:
        return None
    best: tuple[float, str] | None = None
    for markdown_line in segment.splitlines():
        if markdown_line.lstrip().startswith(("<!--", "|")):
            continue
        candidate = _plain_markdown_line(markdown_line)
        if len(candidate) < 12:
            continue
        ratio = SequenceMatcher(None, expected.casefold(), candidate.casefold()).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, candidate)
    return best[1] if best and best[0] >= 0.90 else None


def _restore_numbered_section_headings(markdown: str, lines: list[_Line]) -> tuple[str, set[int]]:
    segments = re.split(r"(?=<!-- page: \d+ -->)", finish_markdown([markdown]))
    restored_pages: set[int] = set()
    for index, segment in enumerate(segments):
        match = re.match(r"<!-- page: (\d+) -->", segment)
        if not match:
            continue
        page_number = int(match.group(1))
        segment_lines = segment.splitlines()
        for source_line in sorted(
            (line for line in lines if line.page == page_number
             and re.match(r"^\d+(?:\.\d+)+\s+[A-Za-z]", line.text)),
            key=lambda item: (item.y0, item.x0),
        ):
            full = normalize_inline(source_line.text)
            suffix = normalize_inline(re.sub(r"^\d+(?:\.\d+)+\s+", "", source_line.text))
            for line_index, markdown_line in enumerate(segment_lines):
                if markdown_line.lstrip().startswith(("<!--", "|")):
                    continue
                plain = _plain_markdown_line(markdown_line)
                if plain.casefold() not in {full.casefold(), suffix.casefold()}:
                    continue
                replacement = f"## {source_line.text}"
                if markdown_line.strip() != replacement:
                    segment_lines[line_index] = replacement
                    restored_pages.add(page_number)
                break
        segments[index] = "\n".join(segment_lines).rstrip() + "\n\n"
    return "".join(segments).strip() + "\n", restored_pages


def _remove_repeated_pdf_furniture(markdown: str, document: fitz.Document) -> tuple[str, int]:
    """Remove repeated marginal text from Docling output, retaining metadata once."""
    if len(document) < 2:
        return markdown, 0
    repeated: Counter[tuple[str, str]] = Counter()
    display: dict[tuple[str, str], str] = {}
    for page in document:
        for block in page.get_text("dict").get("blocks", []):
            for raw_line in block.get("lines", []):
                value = normalize_inline("".join(span.get("text", "") for span in raw_line.get("spans", [])))
                if not value:
                    continue
                bbox = raw_line.get("bbox", (0, 0, 0, 0))
                y0, y1 = float(bbox[1]), float(bbox[3])
                zone = ("top" if y0 < page.rect.height * .13
                        else "bottom" if y1 > page.rect.height * .87 else "")
                if not zone:
                    continue
                # Footer lines often begin with the changing physical page number.
                stable = re.sub(r"^\d+\s+(?=\S)", "", value)
                key = (zone, stable.casefold())
                repeated[key] += 1
                display.setdefault(key, stable)
    threshold = max(2, min(3, (len(document) + 1) // 2))
    top_values = [display[key] for key, count in repeated.items() if key[0] == "top" and count >= threshold]
    has_confidential_footer = any(
        key[0] == "bottom" and count >= threshold and key[1].startswith("confidential:")
        for key, count in repeated.items()
    )
    metadata = [
        value for value in top_values
        if re.match(r"(?i)^(?:DOC(?:UMENT)?\s+NO\.?\s*:|DATE\s*:|MANUFACTURING DOCUMENT$)", value)
    ]
    removed = 0
    segments = re.split(r"(?=<!-- page: \d+ -->)", markdown)
    for index, segment in enumerate(segments):
        page_match = re.match(r"<!-- page: (\d+) -->", segment)
        if not page_match:
            continue
        page_number = int(page_match.group(1))
        lines_out: list[str] = []
        for line in segment.splitlines():
            plain = _plain_markdown_line(line)
            if plain and any(plain.casefold() == value.casefold() for value in top_values):
                removed += 1
                continue
            if plain == str(page_number):
                removed += 1
                continue
            lines_out.append(line)
        segment = "\n".join(lines_out)
        if has_confidential_footer:
            segment, count = re.subn(
                r"(?is)(?:^|\n)\s*CONFIDENTIAL:\s*This document contains proprietary information.*?"
                r"verified for correct revision if viewed/used outside of that system\.\s*(?=\n|\Z)",
                "\n", segment,
            )
            removed += count
        # Removing a furniture-only OCR block must not leave empty review markers.
        segment = re.sub(
            r"(?s)<!-- review-required:\s*[-\w]+\s*-->\s*<!-- review-end -->",
            "", segment,
        )
        segments[index] = segment.rstrip() + "\n\n"
    cleaned = "".join(segments).strip() + "\n"
    if metadata:
        metadata_block = "Source document metadata:\n\n" + "\n".join(dict.fromkeys(metadata))
        first_marker = re.search(r"<!-- page: 1 -->", cleaned)
        if first_marker:
            insert_at = first_marker.end()
            cleaned = cleaned[:insert_at] + f"\n\n{metadata_block}" + cleaned[insert_at:]
    return finish_markdown([cleaned]), removed


def _recover_sparse_docling_pages(converted: ParsedDocument, lines: list[_Line]) -> ParsedDocument:
    """Recover selectable pages that Docling reduced to a small fragment."""
    segments = re.split(r"(?=<!-- page: \d+ -->)", converted.markdown)
    changed = False
    for index, segment in enumerate(segments):
        match = re.match(r"<!-- page: (\d+) -->", segment)
        if not match:
            continue
        page_number = int(match.group(1))
        source_lines = [line for line in lines if line.page == page_number]
        source_length = len(" ".join(line.text for line in source_lines))
        if source_length < 180:
            continue
        existing = re.sub(r"<!--.*?-->", " ", segment, flags=re.DOTALL)
        existing = normalize_inline(" ".join(_plain_markdown_line(line) for line in existing.splitlines()))
        if len(existing) >= source_length * .25:
            continue
        # Keep recovery as evidence text, not inferred headings: a table of
        # contents consists mostly of numbered lines and must not disappear as
        # heading-only context during chunking.
        recovered = "\n\n".join(line.text for line in source_lines).strip()
        if not recovered:
            continue
        if re.match(r"^Table of Contents\b", recovered, re.I):
            recovered = re.sub(r"^Table of Contents\b\s*", "", recovered, count=1, flags=re.I)
            segment += "\n\n# Table of Contents"
        segment += (
            "\n\n<!-- review-required: recovered-selectable-page -->\n\n"
            "Selectable page text omitted by Docling; reading order was recovered from the PDF text layer:\n\n"
            f"{recovered}\n\n<!-- review-end -->"
        )
        converted.warnings.append(ParseWarning(
            code="docling_page_text_recovered",
            message="Docling omitted most selectable page text; the PDF text layer was retained for review",
            location=f"page {page_number}",
        ))
        segments[index] = segment.rstrip() + "\n\n"
        changed = True
    if changed:
        converted.markdown = "".join(segments).strip() + "\n"
    return converted


def _insert_image_ocr_at_figure(segment: str, value: str) -> str:
    """Insert image OCR after its caption, before later section headings."""
    block = (
        "<!-- review-required: pdf-image-ocr -->\n\n"
        f"Image text (OCR): {value}\n\n<!-- review-end -->"
    )
    lines = segment.splitlines()
    caption_index = next(
        (index for index, line in enumerate(lines)
         if re.match(r"^\s*(?:Figure|Fig\.?|Chart)\s+\d+\b", _plain_markdown_line(line), re.I)),
        None,
    )
    if caption_index is None:
        return segment.rstrip() + "\n\n" + block
    insert_at = caption_index + 1
    # Keep a caption's existing review wrapper intact, then place OCR before
    # the next heading or prose block.
    while insert_at < len(lines) and lines[insert_at].strip().startswith("<!--"):
        if lines[insert_at].strip() == "<!-- review-end -->":
            insert_at += 1
            break
        insert_at += 1
    lines[insert_at:insert_at] = ["", block, ""]
    return "\n".join(lines)


def _supplement_image_ocr(document: fitz.Document, converted: ParsedDocument,
                          lines: list[_Line]) -> ParsedDocument:
    """Read large raster images when a layout backend kept their page but missed their text."""
    additions: dict[int, list[str]] = {}
    replacements: dict[int, list[tuple[str, str]]] = {}
    conflicts: dict[int, list[tuple[str, str]]] = {}
    visual_gap_pages: set[int] = set()
    image_cache: dict[str, str] = {}
    converted.markdown, restored_pages = _restore_numbered_section_headings(converted.markdown, lines)
    for page_number in restored_pages:
        converted.warnings.append(ParseWarning(
            code="docling_heading_restored",
            message="Numbered section context was restored from selectable PDF text",
            location=f"page {page_number}",
        ))
    for page_number, page in enumerate(document, start=1):
        image_count = 0
        for info in page.get_image_info(xrefs=True):
            rect = fitz.Rect(info["bbox"])
            if rect.get_area() < page.rect.get_area() * 0.08:
                continue
            if any(rect.contains(fitz.Point((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2))
                   for line in lines if line.page == page_number):
                continue
            if image_count >= 6:
                converted.warnings.append(ParseWarning(
                    code="image_ocr_limit", message="Only six large raster regions per page are OCRed",
                    location=f"page {page_number}",
                ))
                break
            image_count += 1
            visual_gap_pages.add(page_number)
            try:
                data = (document.extract_image(info["xref"])["image"] if info.get("xref")
                        else page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect).tobytes("png"))
                digest = hashlib.sha256(data).hexdigest()
                if digest not in image_cache:
                    image_cache[digest] = ocr_image(data)
                value = image_cache[digest]
            except (OcrUnavailable, ValueError, KeyError, RuntimeError) as error:
                converted.warnings.append(ParseWarning(
                    code="image_ocr_unavailable", message=f"Could not read image text: {error}",
                    location=f"page {page_number}",
                ))
                continue
            page_match = re.search(
                rf"<!-- page: {page_number} -->(.*?)(?=<!-- page: \d+ -->|\Z)",
                converted.markdown, re.DOTALL,
            )
            existing = normalize_inline(page_match.group(1)) if page_match else ""
            if value:
                candidate = _similar_ocr_line(page_match.group(1), value) if page_match else None
                if candidate and candidate.casefold() == normalize_inline(value).casefold():
                    replacements.setdefault(page_number, []).append((candidate, value))
                elif candidate:
                    conflicts.setdefault(page_number, []).append((candidate, value))
                    converted.warnings.append(ParseWarning(
                        code="ocr_reading_conflict",
                        message="Docling and supplemental OCR disagree; both readings require source review",
                        location=f"page {page_number}",
                    ))
                elif normalize_inline(value) not in existing:
                    additions.setdefault(page_number, []).append(value)
                else:
                    replacements.setdefault(page_number, []).append((value, value))
                converted.warnings.append(ParseWarning(
                    code="ocr_needs_review", message="Text in a PDF image needs source review before indexing",
                    location=f"page {page_number}",
                ))
            else:
                converted.warnings.append(ParseWarning(
                    code="figure_needs_review", message="Raster figure has no readable OCR text; inspect the source image",
                    location=f"page {page_number}",
                ))

    recovered: dict[int, list[_Line]] = {}
    for line in lines:
        if not re.search(r"(?:\d+(?:\.\d+)?\s*(?:%|percent\b)|[=^])", line.text, re.I):
            continue
        page_match = re.search(
            rf"<!-- page: {line.page} -->(.*?)(?=<!-- page: \d+ -->|\Z)",
            converted.markdown, re.DOTALL,
        )
        existing = normalize_inline(page_match.group(1)) if page_match else ""
        if normalize_inline(line.text) not in existing:
            recovered.setdefault(line.page, []).append(line)
    for page_number in recovered:
        converted.warnings.append(ParseWarning(
            code="docling_text_omission",
            message="Docling omitted numeric selectable text; recovered source lines require layout review",
            location=f"page {page_number}",
        ))

    if not (additions or replacements or conflicts or visual_gap_pages or recovered):
        return converted
    segments = re.split(r"(?=<!-- page: \d+ -->)", converted.markdown)
    changed_pages = set(additions) | set(replacements) | set(conflicts) | set(recovered) | visual_gap_pages
    seen: set[int] = set()
    for index, segment in enumerate(segments):
        match = re.match(r"<!-- page: (\d+) -->", segment)
        if not match:
            continue
        page_number = int(match.group(1))
        if page_number not in changed_pages:
            continue
        seen.add(page_number)
        for candidate, value in replacements.get(page_number, []):
            segment_lines = segment.splitlines()
            for line_index, markdown_line in enumerate(segment_lines):
                if _plain_markdown_line(markdown_line).casefold() == candidate.casefold():
                    segment_lines[line_index] = (
                        "<!-- review-required: pdf-image-ocr -->\n\n"
                        f"Image text (OCR): {value}\n\n<!-- review-end -->"
                    )
                    break
            segment = "\n".join(segment_lines)
        for docling_value, supplemental_value in conflicts.get(page_number, []):
            segment_lines = segment.splitlines()
            for line_index, markdown_line in enumerate(segment_lines):
                if _plain_markdown_line(markdown_line).casefold() == docling_value.casefold():
                    segment_lines[line_index] = (
                        "<!-- review-required: conflicting-image-ocr -->\n\n"
                        "Conflicting image OCR readings; verify the source before use.\n\n"
                        f"Docling OCR: {docling_value}\n\n"
                        f"Supplemental OCR: {supplemental_value}\n\n"
                        "<!-- review-end -->"
                    )
                    break
            segment = "\n".join(segment_lines)
        if page_number in recovered:
            omitted = sorted(recovered[page_number], key=lambda item: (item.y0, item.x0))
            recovery = (
                "<!-- review-required: recovered-selectable-text -->\n\n"
                "Recovered selectable text omitted by layout conversion:\n\n"
                + "\n\n".join(item.text for item in omitted)
                + "\n\n<!-- review-end -->"
            )
            last_y = max(item.y1 for item in omitted)
            following = next((
                item for item in lines
                if item.page == page_number and item.y0 > last_y
                and re.match(r"^\d+(?:\.\d+)+\s+", item.text)
                and re.search(rf"(?m)^#+\s+{re.escape(item.text)}\s*$", segment)
            ), None)
            if following:
                segment = re.sub(
                    rf"(?m)^(#+\s+{re.escape(following.text)}\s*)$",
                    lambda heading: f"{recovery}\n\n{heading.group(1)}",
                    segment, count=1,
                )
            else:
                segment += f"\n\n{recovery}"
        for value in additions.get(page_number, []):
            segment = _insert_image_ocr_at_figure(segment, value)
        if (page_number in visual_gap_pages
                and f"Visual evidence on page {page_number}" not in segment):
            segment += (
                "\n\n<!-- review-required: visual-evidence -->\n\n"
                f"Visual evidence on page {page_number} requires source review; "
                "no verified description is available.\n\n<!-- review-end -->"
            )
            if not any(w.code == "visual_evidence_missing" and w.location == f"page {page_number}"
                       for w in converted.warnings):
                converted.warnings.append(ParseWarning(
                    code="visual_evidence_missing",
                    message="A figure or chart has no verified semantic description; do not infer its relationships from OCR labels",
                    location=f"page {page_number}",
                ))
        segments[index] = segment.rstrip() + "\n\n"
    for page_number, values in additions.items():
        if page_number not in seen:
            segments.append(f"\n\n<!-- page: {page_number} -->")
            for value in values:
                segments.append(f"\n\n<!-- review-required: pdf-image-ocr -->\n\nImage text (OCR): {value}\n\n<!-- review-end -->")
    converted.markdown = "".join(segments).strip() + "\n"
    return converted


def parse_pdf_to_markdown(
    path: str | Path,
    source_name: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    display_name = source_name or input_path.name
    warnings: list[ParseWarning] = []
    backend = os.environ.get("GSME_PDF_BACKEND", "auto").casefold()
    if backend not in {"auto", "native", "docling"}:
        raise ValueError("GSME_PDF_BACKEND must be auto, native or docling")

    # Bounded API uploads are opened from bytes so a failed PyMuPDF open
    # cannot retain the temporary PDF file on Windows.
    if input_path.stat().st_size <= 20 * 1024 * 1024:
        document = fitz.open(stream=input_path.read_bytes(), filetype="pdf")
    else:
        document = fitz.open(input_path)
    with document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs require an unlocked copy")
        if not document.page_count:
            return ParsedDocument(
                filename=display_name, document_type="pdf", strategy_used="pdf_no_content",
                markdown="", warnings=[ParseWarning("no_pdf_text", "PDF contains no pages")],
            )
        lines = _extract_lines(document)
        if not lines and not any(page.get_images() or page.get_drawings() for page in document):
            return ParsedDocument(
                filename=display_name, document_type="pdf", strategy_used="pdf_no_content",
                markdown="", warnings=[ParseWarning(code="no_pdf_text", message="No readable text or images were found")],
            )
        page_width = float(document[0].rect.width)

        special_faq = bool(lines) and _detect_table_layout(lines, page_width)
        complex_layout = any(
            page.get_images() or page.find_tables().tables or len(page.get_drawings()) >= 10
            for page in document
        )
        if lines and not special_faq and not complex_layout and backend != "docling":
            return ParsedDocument(
                filename=display_name, document_type="pdf",
                strategy_used="pdf_reading_order_to_markdown",
                markdown=_layout_markdown(lines), warnings=[],
            )
        if not special_faq:
            docling_available = find_spec("docling") is not None
            skip_docling = backend == "native" or backend == "auto" and len(document) > 12
            if skip_docling and backend == "auto" and docling_available:
                warnings.append(ParseWarning(
                    code="docling_skipped_large_pdf",
                    message="Large PDF used the faster native path; set GSME_PDF_BACKEND=docling for an offline high-cost conversion",
                ))
            if not docling_available and backend != "native":
                warnings.append(ParseWarning(
                    code="docling_unavailable",
                    message="Using native PDF extraction; install the pdf extra for layout and OCR processing",
                ))
            elif not skip_docling:
                try:
                    from gsme_rag.parsing.docling_pdf import parse_docling_pdf
                    converted = parse_docling_pdf(input_path, display_name)
                    converted = _supplement_image_ocr(document, converted, lines)
                    converted = _recover_sparse_docling_pages(converted, lines)
                    converted.markdown, furniture_removed = _remove_repeated_pdf_furniture(
                        converted.markdown, document
                    )
                    if furniture_removed:
                        converted.warnings.append(ParseWarning(
                            code="page_furniture_removed",
                            message=(f"Removed {furniture_removed} repeated header, footer, or page-number "
                                     "occurrences from retrieval evidence; source metadata is retained once"),
                        ))
                    if "Image text (OCR):" in converted.markdown:
                        converted.warnings = [
                            warning for warning in converted.warnings
                            if warning.code not in {"no_pdf_text", "ocr_page_empty"}
                        ]
                    if not any(w.code in {"no_pdf_text", "ocr_page_empty"} for w in converted.warnings):
                        return converted
                    warnings.append(ParseWarning(
                        code="docling_empty_fallback",
                        message="Docling missed image-page text; the native OCR/table path was used instead",
                    ))
                except Exception as error:
                    warnings.append(ParseWarning(
                        code="docling_failed",
                        message=f"Docling could not process this PDF ({type(error).__name__}: {str(error)[:180]}); native extraction needs review",
                    ))
            markdown, native_warnings = _native_markdown(document, lines)
            warnings.extend(native_warnings)
            if not markdown.replace("<!--", "").strip() or not lines and not any(w.code == "ocr_needs_review" for w in warnings):
                warnings.append(ParseWarning(code="no_pdf_text", message="No readable text was recovered"))
            return ParsedDocument(
                filename=display_name, document_type="pdf", strategy_used="pymupdf_table_fallback",
                markdown=markdown, warnings=warnings,
            )

    if special_faq:
        table, table_warnings = _table_markdown(lines, page_width)
        warnings.extend(table_warnings)
        warnings.append(
            ParseWarning(
                code="pdf_table_inferred",
                message=(
                    "Question and answer columns were inferred from PDF positions; "
                    "check the Markdown rows against the original pages"
                ),
            )
        )
        markdown = finish_markdown(["<!-- pdf-table-layout -->\n" + table])
        strategy = "pdf_table_layout_to_markdown"
    else:
        markdown = _layout_markdown(lines)
        strategy = "pdf_reading_order_to_markdown"

    result = ParsedDocument(
        filename=display_name,
        document_type="pdf",
        strategy_used=strategy,
        markdown=markdown,
        warnings=warnings,
    )
    if special_faq:
        with fitz.open(input_path) as image_pdf:
            return _supplement_image_ocr(image_pdf, result, lines)
    return result
