import csv
from pathlib import Path

from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table
from gsme_rag.models import ParseWarning, ParsedDocument


def parse_csv_to_markdown(
    path: str | Path,
    source_name: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    display_name = source_name or input_path.name
    warnings: list[ParseWarning] = []

    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = [[value.strip() for value in row] for row in csv.reader(source)]

    rows = [row for row in rows if any(value for value in row)]
    if not rows:
        return ParsedDocument(
            filename=display_name,
            document_type="csv",
            strategy_used="csv_to_markdown_table",
            markdown="",
            warnings=[
                ParseWarning(code="empty_csv", message="The CSV file contains no data")
            ],
        )

    expected_width = max(len(row) for row in rows)
    normalized_rows: list[list[str]] = []
    for row_number, row in enumerate(rows, start=1):
        if len(row) != expected_width:
            warnings.append(
                ParseWarning(
                    code="uneven_csv_row",
                    message=(
                        f"Expected {expected_width} columns but found {len(row)}; "
                        "empty cells were added without discarding data"
                    ),
                    location=f"row {row_number}",
                )
            )
        normalized_rows.append(row + [""] * (expected_width - len(row)))

    for index, heading in enumerate(normalized_rows[0]):
        if not heading:
            normalized_rows[0][index] = f"Column {index + 1}"
            warnings.append(
                ParseWarning(
                    code="missing_csv_header",
                    message="A blank or missing column name was given a neutral label",
                    location=f"column {index + 1}",
                )
            )

    title = Path(display_name).stem.replace("_", " ").replace("-", " ").strip().title()
    markdown = finish_markdown(
        [
            f"# {title or 'CSV Data'}",
            "<!-- csv-table -->\n" + render_markdown_table(normalized_rows),
        ]
    )
    return ParsedDocument(
        filename=display_name,
        document_type="csv",
        strategy_used="csv_to_markdown_table",
        markdown=markdown,
        warnings=warnings,
    )
