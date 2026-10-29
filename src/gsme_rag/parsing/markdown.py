import re


CONTROL_MARKER = re.compile(
    r"<!--\s*(?:page:\s*\d+|review-required:\s*[-\w]+|review-end|section-break|"
    r"table:\s*\d+;\s*first_data_row:\s*\d+)\s*-->"
)


def _transform_prose(markdown: str, transform) -> str:
    """Leave fenced source code untouched by Markdown cleanup."""
    output, pending = [], []
    fence = None
    for line in markdown.splitlines(keepends=True):
        candidate = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence is not None:
            output.append(line)
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line.strip()):
                fence = None
        elif candidate:
            output.append(transform("".join(pending)))
            pending = []
            fence = candidate.group(1)
            output.append(line)
        else:
            pending.append(line)
    output.append(transform("".join(pending)))
    return "".join(output)


def normalize_control_markers(markdown: str) -> str:
    """Put provenance and review comments on standalone lines."""
    return _transform_prose(markdown, lambda text: CONTROL_MARKER.sub(
        lambda match: f"\n{match.group(0).strip()}\n", text))


def review_pdf_pages(markdown: str, pages: set[int], reason: str = "text-fidelity") -> str:
    """Wrap affected pages without changing their source markers or content order."""
    segments = re.split(r"(?=<!-- page: \d+ -->)", normalize_control_markers(markdown))
    for index, segment in enumerate(segments):
        match = re.match(r"(<!-- page: (\d+) -->)(.*)", segment, re.DOTALL)
        if match and int(match.group(2)) in pages:
            segments[index] = (f"{match.group(1)}\n\n<!-- review-required: {reason} -->\n\n"
                               f"{match.group(3).strip()}\n\n<!-- review-end -->\n\n")
    return finish_markdown(["".join(segments)])


def table_requires_review(markdown: str) -> bool:
    """Identify formula tables whose symbols need checking against the source."""
    header = markdown.splitlines()[0].casefold() if markdown else ""
    return "formula" in header or bool(re.search(r"[=±≤≥∑∫\U0001d400-\U0001d7ff]", markdown))


def escape_table_cell(value: str) -> str:
    cleaned = value.replace("\r", "").strip()
    cleaned = re.sub(r"\n+", "<br>", cleaned)
    return cleaned.replace("\\", "\\\\").replace("|", "\\|")


def split_table_row(line: str) -> list[str]:
    """Read cell boundaries without treating an escaped pipe as a column."""
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value) and value[index + 1] in {"\\", "|"}:
            current.append(value[index + 1])
            index += 2
            continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    if current or not value.endswith("|"):
        cells.append("".join(current).strip())
    return cells


def render_markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    body = padded[1:]

    output = [
        "| " + " | ".join(escape_table_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    output.extend(
        "| " + " | ".join(escape_table_cell(cell) for cell in row) + " |"
        for row in body
    )
    return "\n".join(output)


def finish_markdown(parts: list[str]) -> str:
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    text = normalize_control_markers(text)
    text = _transform_prose(text, lambda value: re.sub(r"\n{3,}", "\n\n", value))
    return text.strip() + "\n" if text.strip() else ""
