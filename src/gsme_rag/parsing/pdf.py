from dataclasses import dataclass
from pathlib import Path
import re
from statistics import median

import pymupdf as fitz

from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table
from gsme_rag.models import ParseWarning, ParsedDocument
from gsme_rag.text import normalize_inline, normalize_multiline, split_question_number


@dataclass(slots=True)
class _Line:
    page: int
    text: str
    x0: float
    y0: float
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
                        size=max(sizes, default=0.0),
                        bold_share=bold_chars / total_chars if total_chars else 0.0,
                    )
                )
    return sorted(lines, key=lambda line: (line.page, round(line.y0, 1), line.x0))


def _detect_table_layout(lines: list[_Line], page_width: float) -> bool:
    numbered_left = sum(
        1
        for line in lines
        if line.x0 < page_width * 0.42
        and line.bold_share >= 0.6
        and re.match(r"^\d{1,3}\s+", line.text)
    )
    right_text = sum(1 for line in lines if line.x0 >= page_width * 0.33)
    return numbered_left >= 3 and right_text >= 6


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
    category = "Uncategorized"

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


def _layout_markdown(lines: list[_Line]) -> str:
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

    for line in lines:
        if line.page != active_page:
            active_page = line.page
            parts.append(f"<!-- page: {active_page} -->")

        is_heading = (
            line.bold_share >= 0.6
            and line.size >= body_size * 1.12
            and "?" not in line.text
            and len(line.text) <= 100
        )
        if is_heading:
            level = 1 if not has_document_title or not first_heading_written else 2
            parts.append(f"{'#' * level} {line.text}")
            first_heading_written = True
        elif line.bold_share >= 0.6:
            parts.append(f"**{line.text}**")
        else:
            parts.append(line.text)

    return finish_markdown(parts)


def parse_pdf_to_markdown(
    path: str | Path,
    source_name: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    display_name = source_name or input_path.name
    warnings: list[ParseWarning] = []

    with fitz.open(input_path) as document:
        lines = _extract_lines(document)
        if not lines:
            return ParsedDocument(
                filename=display_name,
                document_type="pdf",
                strategy_used="pdf_layout_to_markdown",
                markdown="",
                warnings=[
                    ParseWarning(
                        code="no_pdf_text",
                        message=(
                            "No selectable PDF text was found. Scanned PDFs and OCR "
                            "are outside the current scope."
                        ),
                    )
                ],
            )
        page_width = float(document[0].rect.width)

    title = Path(display_name).stem.replace("_", " ").replace("-", " ").strip().title()
    if _detect_table_layout(lines, page_width):
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
        markdown = finish_markdown(
            [f"# {title or 'PDF Document'}", "<!-- pdf-table-layout -->\n" + table]
        )
        strategy = "pdf_table_layout_to_markdown"
    else:
        markdown = _layout_markdown(lines)
        strategy = "pdf_reading_order_to_markdown"

    return ParsedDocument(
        filename=display_name,
        document_type="pdf",
        strategy_used=strategy,
        markdown=markdown,
        warnings=warnings,
    )
