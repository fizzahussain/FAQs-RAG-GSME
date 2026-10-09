import csv
from pathlib import Path

from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table
from gsme_rag.parsing.schedules import is_schedule_header, resolve_schedule_kind
from gsme_rag.models import ParseWarning, ParsedDocument


def parse_csv_to_markdown(
    path: str | Path,
    source_name: str | None = None,
    schedule_kind: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    display_name = source_name or input_path.name
    warnings: list[ParseWarning] = []

    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source)
        nonempty = [
            (reader.line_num, [value.strip() for value in row])
            for row in reader if any(value.strip() for value in row)
        ]

    rows = [row for _, row in nonempty]
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
    while expected_width and all(
        len(row) < expected_width or not row[expected_width - 1]
        for row in rows
    ):
        for row in rows:
            if len(row) == expected_width:
                row.pop()
        expected_width -= 1
    normalized_rows: list[list[str]] = []
    for (row_number, _), row in zip(nonempty, rows):
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

    recognized_schedule = is_schedule_header(normalized_rows[0])
    if not recognized_schedule and schedule_kind in {"full", "shared"}:
        raise ValueError("The selected schedule type requires the known schedule columns")
    kind = resolve_schedule_kind(display_name, schedule_kind) if recognized_schedule else None
    if kind is None and recognized_schedule:
        warnings.append(
            ParseWarning(
                code="schedule_kind_unknown",
                message="Select Full or Shared schedule for a generically named schedule CSV",
            )
        )
    title = (
        f"{kind.title()} Schedules"
        if kind else Path(display_name).stem.replace("_", " ").replace("-", " ").strip().title()
    )
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
        csv_source_rows=[row_number for row_number, _ in nonempty[1:]],
    )
