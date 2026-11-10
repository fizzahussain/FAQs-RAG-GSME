"""Preserve source link targets without crawling them or guessing missing URLs."""

import re
from urllib.parse import urlsplit

from gsme_rag.models import ParseWarning


def safe_link_target(target: str) -> bool:
    if not target or re.search(r"[\s<>|\x00-\x1f\x7f]", target):
        return False
    try:
        parsed = urlsplit(target)
        return ((parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None)
                or parsed.scheme.lower() == "mailto" and bool(parsed.path))
    except ValueError:
        return False


def markdown_link(label: str, target: str) -> str:
    if not safe_link_target(target):
        return label
    label = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    # Angle-delimited destinations preserve parentheses and query parameters.
    return f"[{label}](<{target}>)"


_LINK = re.compile(r"\[(?:\\.|[^\]\\])*\]\((?:<[^>\n]+>|[^)\n]+)\)")
_MISSING = re.compile(r"\b(?:(?:can\s+be\s+)?found|available|click)\s+(?:\*{1,2})?here\b", re.I)


def missing_link_reference(text: str) -> bool:
    # Real source hyperlinks and literal fenced examples are not missing links.
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    for match in _LINK.finditer(text):
        target = re.search(r"\]\((?:<([^>]+)>|([^)]+))\)$", match.group())
        if target and not safe_link_target(target.group(1) or target.group(2)):
            return True
    return bool(_MISSING.search(_LINK.sub("source-link", text)))


def missing_link_warnings(markdown: str) -> list[ParseWarning]:
    from gsme_rag.parsing.markdown import _transform_prose, split_table_row
    warnings = []
    page = None
    page_column = None
    def inspect(prose):
        nonlocal page, page_column
        for line in prose.splitlines():
            marker = re.fullmatch(r"<!-- page: (\d+) -->", line.strip())
            if marker:
                page = int(marker.group(1))
            row_page = page
            if line.lstrip().startswith("|"):
                cells = split_table_row(line)
                for column, value in enumerate(cells):
                    if value.casefold() in {"page", "source page"}:
                        page_column = column
                if page_column is not None and page_column < len(cells) and cells[page_column].isdigit():
                    row_page = int(cells[page_column])
            if missing_link_reference(line):
                location = f"page {row_page}" if row_page else "document"
                if not any(w.location == location for w in warnings):
                    warnings.append(ParseWarning("link_target_missing",
                        "A reference has no usable source link destination; provide the original hyperlink, not a guessed URL",
                        location))
        return prose
    _transform_prose(markdown, inspect)
    return warnings


def preserve_pdf_links(document, result):
    """Place URI annotations by source-line text, only when alignment is unique."""
    import pymupdf as fitz
    segments = re.split(r"(?=<!-- page: \d+ -->)", result.markdown)
    for number, page in enumerate(document, 1):
        links = [item for item in page.get_links() if item.get("uri")]
        if not links:
            continue
        lines = [line for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])]
        for link in links:
            target = link["uri"]
            if not safe_link_target(target):
                result.warnings.append(ParseWarning("unsafe_link_target", "Unsupported or unsafe source link retained in the original PDF only", f"page {number}"))
                continue
            rect = fitz.Rect(link["from"])
            anchor = " ".join(page.get_textbox(rect).split())
            source_lines = [line for line in lines if fitz.Rect(line["bbox"]).intersects(rect)]
            source_text = " ".join("".join(span["text"] for span in line["spans"]) for line in source_lines).strip()
            candidates = [i for i, segment in enumerate(segments)
                          if segment.startswith(f"<!-- page: {number} -->")]
            if not candidates and len(document) == 1:
                candidates = list(range(len(segments)))
            if not candidates and "<!-- pdf-table-layout -->" in result.markdown:
                candidates = list(range(len(segments)))
            placed = False
            if anchor and source_text and len(candidates) == 1:
                index = candidates[0]
                pattern = r"\s+".join(re.escape(word) for word in source_text.split())
                matches = list(re.finditer(pattern, segments[index]))
                if len(matches) == 1:
                    match = matches[0]
                    text = match.group()
                    if text.count(anchor) == 1:
                        replacement = text.replace(anchor, markdown_link(anchor, target), 1)
                        segments[index] = segments[index][:match.start()] + replacement + segments[index][match.end():]
                        placed = True
                elif markdown_link(anchor, target) in segments[index]:
                    placed = True  # Backend already preserved this exact source link.
            if not placed:
                # Do not attach an ambiguous 'here' to the wrong FAQ. Keep an
                # explicitly reviewed source-region note instead of losing it.
                index = candidates[0] if len(candidates) == 1 else len(segments) - 1
                box = ", ".join(f"{value:.1f}" for value in rect)
                page_marker = "" if len(candidates) == 1 and segments[index].startswith(f"<!-- page: {number} -->") else f"<!-- page: {number} -->\n"
                segments[index] += (f"\n\n{page_marker}<!-- review-required: pdf-link-placement -->\n"
                    f"Source hyperlink on page {number}, box ({box}): {markdown_link(anchor or target, target)}\n"
                    "<!-- review-end -->\n")
                result.warnings.append(ParseWarning("pdf_link_placement_review",
                    "Source hyperlink destination recovered but exact text placement needs review", f"page {number}"))
    result.markdown = "".join(segments)
    return result
