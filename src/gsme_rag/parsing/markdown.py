import re


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
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n" if text.strip() else ""
