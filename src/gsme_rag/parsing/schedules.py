"""Read schedule CSV rows as typed records for a structured preview."""

import csv
from datetime import date, datetime
from pathlib import Path


SCHEDULE_COLUMNS = (
    "technology_nm",
    "flavor",
    "trial_deadline",
    "final_deadline",
    "tapeout_date",
    "estimated_ship_date",
)
DATE_COLUMNS = SCHEDULE_COLUMNS[2:]


def _date(value: str, row_number: int, column: str) -> date:
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"row {row_number}: {column} must be a valid date")


def parse_schedule_records(path: str | Path) -> list[dict]:
    """Validate the known schedule schema and retain each CSV source row."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headings = [name.strip() for name in reader.fieldnames or [] if name]
        if len(headings) != len(set(headings)):
            raise ValueError("Schedule CSV has duplicate column names")
        missing = [name for name in SCHEDULE_COLUMNS if name not in headings]
        if missing:
            raise ValueError(f"Schedule CSV is missing columns: {', '.join(missing)}")

        records = []
        for row_number, raw in enumerate(reader, start=2):
            if not any((value or "").strip() for value in raw.values() if isinstance(value, str)):
                continue
            if None in raw:
                raise ValueError(f"row {row_number}: more cells than column names")
            row = {name: (raw.get(name) or "").strip() for name in SCHEDULE_COLUMNS}
            if not row["technology_nm"].isdigit():
                raise ValueError(f"row {row_number}: technology_nm must be a whole number")
            if not row["flavor"]:
                raise ValueError(f"row {row_number}: flavor is required")
            record = {
                "source_row_number": row_number,
                "technology_nm": int(row["technology_nm"]),
                "flavor": row["flavor"],
            }
            record.update({name: _date(row[name], row_number, name) for name in DATE_COLUMNS})
            records.append(record)
    return records


def filter_schedule_records(
    records: list[dict],
    technology_nm: int | None = None,
    flavor: str | None = None,
    tapeout_on_or_after: str | None = None,
) -> list[dict]:
    """Apply exact field and date filters without storing the records."""
    if tapeout_on_or_after:
        try:
            tapeout_on_or_after = _date(tapeout_on_or_after, 0, "tapeout_on_or_after")
        except ValueError as error:
            raise ValueError("tapeout_on_or_after must be a valid date") from error
    return [
        record for record in records
        if (technology_nm is None or record["technology_nm"] == technology_nm)
        and (not flavor or record["flavor"].casefold() == flavor.strip().casefold())
        and (not tapeout_on_or_after or record["tapeout_date"] >= tapeout_on_or_after)
    ]
