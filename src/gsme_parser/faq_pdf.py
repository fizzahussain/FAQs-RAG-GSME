from dataclasses import dataclass
from pathlib import Path
import re
from statistics import median

import pymupdf as fitz

from gsme_parser.models import FAQRecord, ParseResult, ParseWarning
from gsme_parser.text import make_faq_id, normalize_inline, normalize_multiline, split_question_number


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
            if "lines" not in block:
                continue
            for raw_line in block["lines"]:
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


def _layout_lines_to_text(lines: list[_Line]) -> str:
    if not lines:
        return ""
    paragraphs: list[str] = []
    current: list[str] = []
    previous: _Line | None = None
    for line in lines:
        new_paragraph = False
        if previous is not None:
            vertical_gap = line.y0 - previous.y0 if line.page == previous.page else float("inf")
            expected_gap = max(line.size, previous.size, 1.0)
            new_paragraph = line.page != previous.page or vertical_gap > expected_gap * 1.45
        if new_paragraph and current:
            paragraphs.append(normalize_inline(" ".join(current)))
            current = []
        current.append(line.text)
        previous = line
    if current:
        paragraphs.append(normalize_inline(" ".join(current)))
    return normalize_multiline("\n\n".join(paragraphs))


def _parse_qa_layout(path: Path, lines: list[_Line]) -> ParseResult:
    result = ParseResult()
    sizes = [line.size for line in lines if 7 <= line.size <= 18]
    body_size = median(sizes) if sizes else 11.0
    category = "Uncategorized"
    current_question: str | None = None
    current_number: str | None = None
    current_answer: list[_Line] = []
    start_page: int | None = None
    end_page: int | None = None
    pending_question: list[str] = []
    pending_number: str | None = None
    seen_ids: set[str] = set()

    def flush() -> None:
        nonlocal current_question, current_number, current_answer, start_page, end_page
        if current_question is None:
            return
        answer = _layout_lines_to_text(current_answer)
        if answer:
            faq_id = make_faq_id(category, current_number, current_question)
            if faq_id in seen_ids:
                faq_id = make_faq_id(category, None, current_question)
            seen_ids.add(faq_id)
            result.faq_records.append(
                FAQRecord(
                    faq_id=faq_id,
                    faq_number=current_number,
                    category=category,
                    question=current_question,
                    answer=answer,
                    source_file=path.name,
                    source_kind="pdf_qa_layout",
                    source_location=f"pages {start_page}-{end_page}",
                    page_start=start_page,
                    page_end=end_page,
                )
            )
        else:
            result.warnings.append(
                ParseWarning(
                    code="missing_answer",
                    message=f"No answer found for question: {current_question}",
                    location=f"page {start_page}",
                )
            )
        current_question = None
        current_number = None
        current_answer = []
        start_page = None
        end_page = None

    for line in lines:
        text = line.text
        number, question_text = split_question_number(text)
        numbered_bold = bool(number and line.bold_share >= 0.6)

        if numbered_bold:
            flush()
            pending_question = [question_text]
            pending_number = number
            if question_text.endswith("?"):
                current_question = normalize_inline(" ".join(pending_question))
                current_number = pending_number
                start_page = line.page
                end_page = line.page
                pending_question = []
                pending_number = None
            continue

        if pending_question:
            if line.bold_share >= 0.6:
                pending_question.append(text)
                if text.endswith("?"):
                    current_question = normalize_inline(" ".join(pending_question))
                    current_number = pending_number
                    start_page = line.page
                    end_page = line.page
                    pending_question = []
                    pending_number = None
                continue
            result.warnings.append(
                ParseWarning(
                    code="incomplete_question",
                    message="A numbered question did not end with a question mark",
                    location=f"page {line.page}",
                )
            )
            pending_question = []
            pending_number = None

        is_category = (
            line.bold_share >= 0.6
            and line.size >= body_size * 1.12
            and not re.match(r"^\d{1,3}\s+", text)
            and "?" not in text
            and len(text) <= 80
        )
        if is_category:
            if line.size > body_size * 1.7 and category == "Uncategorized":
                continue
            flush()
            category = text
            continue

        if current_question is not None:
            current_answer.append(line)
            end_page = line.page

    flush()
    return result


def _parse_table_layout(path: Path, lines: list[_Line], page_width: float) -> ParseResult:
    result = ParseResult()
    split_x = page_width * 0.33
    seen_ids: set[str] = set()
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
                "number": number,
                "parts": [stripped],
                "category": category,
            }
            continue

        if question_event is not None and line.bold_share >= 0.6:
            if normalize_inline(" ".join(question_event["parts"])).endswith("?"):
                events.append(question_event)
                question_event = None
            else:
                question_event["parts"].append(line.text)
                continue

        is_category = (
            line.bold_share >= 0.6
            and "?" not in line.text
            and len(line.text) <= 80
        )
        if is_category:
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

    question_events = [event for event in events if event["kind"] == "question"]
    boundary_events = sorted(events, key=lambda event: (event["page"], event["y"]))

    for question in question_events:
        start_key = (question["page"], question["y"])
        next_boundary = next(
            (
                event
                for event in boundary_events
                if (event["page"], event["y"]) > start_key
            ),
            None,
        )
        end_key = (
            (next_boundary["page"], next_boundary["y"])
            if next_boundary is not None
            else (10**9, float("inf"))
        )
        answer_lines = [
            line
            for line in right_lines
            if start_key <= (line.page, line.y0) < end_key
        ]
        answer_pages = [
            line.page
            for line in right_lines
            if start_key <= (line.page, line.y0) < end_key
        ]
        question_text = normalize_inline(" ".join(question["parts"]))
        answer = _layout_lines_to_text(answer_lines)

        if not question_text.endswith("?"):
            result.warnings.append(
                ParseWarning(
                    code="incomplete_question",
                    message=f"Question does not end with a question mark: {question_text}",
                    location=f"page {question['page']}",
                )
            )
        if not answer:
            result.warnings.append(
                ParseWarning(
                    code="missing_answer",
                    message=f"No answer found for question: {question_text}",
                    location=f"page {question['page']}",
                )
            )
            continue

        faq_id = make_faq_id(question["category"], question["number"], question_text)
        if faq_id in seen_ids:
            faq_id = make_faq_id(question["category"], None, question_text)
        seen_ids.add(faq_id)
        end_page = max(answer_pages, default=question["page"])
        result.faq_records.append(
            FAQRecord(
                faq_id=faq_id,
                faq_number=question["number"],
                category=question["category"],
                question=question_text,
                answer=answer,
                source_file=path.name,
                source_kind="pdf_table_layout",
                source_location=f"pages {question['page']}-{end_page}",
                page_start=question["page"],
                page_end=end_page,
            )
        )
    return result


def parse_faq_pdf(path: str | Path, strategy: str = "auto") -> ParseResult:
    input_path = Path(path)
    if strategy not in {"auto", "qa", "table"}:
        raise ValueError("PDF strategy must be auto, qa, or table")

    with fitz.open(input_path) as document:
        lines = _extract_lines(document)
        if not lines:
            return ParseResult(
                warnings=[
                    ParseWarning(
                        code="no_pdf_text",
                        message=(
                            "No selectable PDF text was found. Scanned PDFs are outside "
                            "the current demo scope."
                        ),
                    )
                ]
            )
        page_width = float(document[0].rect.width)

    selected = strategy
    if selected == "auto":
        selected = "table" if _detect_table_layout(lines, page_width) else "qa"

    result = (
        _parse_table_layout(input_path, lines, page_width)
        if selected == "table"
        else _parse_qa_layout(input_path, lines)
    )
    if not result.faq_records:
        result.warnings.append(
            ParseWarning(
                code="no_faq_records",
                message="No FAQ records could be identified from the PDF",
            )
        )
    return result
