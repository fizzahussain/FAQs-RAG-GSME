import re


CONTROL_MARKER = re.compile(
    r"<!--\s*(?:page:\s*\d+|review-required:\s*[-\w]+|review-end|section-break|"
    r"table:\s*\d+;\s*first_data_row:\s*\d+)\s*-->"
)


def normalize_control_markers(markdown: str) -> str:
    """Put provenance and review comments on standalone lines."""
    return CONTROL_MARKER.sub(lambda match: f"\n{match.group(0).strip()}\n", markdown)


def table_requires_review(markdown: str) -> bool:
    """Identify formula tables whose symbols need checking against the source."""
    header = markdown.splitlines()[0].casefold() if markdown else ""
    return "formula" in header or bool(re.search(r"[=±≤≥∑∫\U0001d400-\U0001d7ff]", markdown))


def escape_table_cell(value: str) -> str:
    cleaned = value.replace("\r", "").strip()
    cleaned = re.sub(r"\n+", "<br>", cleaned)
    return cleaned.replace("|", "\\|")


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
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n" if text.strip() else ""
