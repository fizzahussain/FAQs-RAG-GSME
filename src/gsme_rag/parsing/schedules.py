"""Read schedule CSV rows as typed records for a structured preview."""

import csv
import hashlib
from datetime import date, datetime
from pathlib import Path
import re


SCHEDULE_COLUMNS = (
    "technology_nm",
    "flavor",
    "trial_deadline",
    "final_deadline",
    "tapeout_date",
    "estimated_ship_date",
)
DATE_COLUMNS = SCHEDULE_COLUMNS[2:]
SCHEDULE_HEADER_ALIASES = {
    "tech (nm)": "technology_nm",
    "flavor": "flavor",
    "trial": "trial_deadline",
    "final": "final_deadline",
    "tapeout": "tapeout_date",
    "est. ship": "estimated_ship_date",
}


def normalize_schedule_header(value: str) -> str:
    """Recognize only the two known exports, without guessing new columns."""
    heading = " ".join(value.strip().casefold().split())
    if heading in SCHEDULE_COLUMNS:
        return heading
    return SCHEDULE_HEADER_ALIASES.get(heading, heading)


def is_schedule_header(values: list[str]) -> bool:
    return set(SCHEDULE_COLUMNS).issubset(
        {normalize_schedule_header(value) for value in values}
    )


def make_source_id(content_sha256: str, schedule_kind: str | None = None) -> str:
    """Distinguish identical schedule bytes assigned different business labels."""
    key = f"{content_sha256}|{schedule_kind or 'unclassified'}"
    return f"doc-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def resolve_schedule_kind(filename: str, selected: str | None = None) -> str | None:
    """Use a supplied label or a clear filename, never the row count."""
    if selected not in {None, "auto", "full", "shared"}:
        raise ValueError("schedule_kind must be auto, full, or shared")
    name = Path(filename).stem.casefold()
    labels = {
        kind for kind in ("shared", "full")
        if re.search(rf"(?<![a-z]){kind}(?![a-z])", name)
    }
    if len(labels) > 1:
        raise ValueError("filename contains both Full and Shared; rename it unambiguously")
    for kind in ("shared", "full"):
        if re.search(rf"(?<![a-z]){kind}(?![a-z])", name):
            if selected in {"full", "shared"} and selected != kind:
                raise ValueError(f"filename says {kind} schedules but {selected} was selected")
            return kind
    return selected if selected in {"full", "shared"} else None


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
        reader = csv.reader(source, strict=True)
        raw_headers = next((row for row in reader if any(value.strip() for value in row)), [])
        headings = [normalize_schedule_header(name) for name in raw_headers]
        while headings and not headings[-1]:
            headings.pop()
        if len(headings) != len(set(headings)):
            raise ValueError("Schedule CSV has duplicate column names")
        missing = [name for name in SCHEDULE_COLUMNS if name not in headings]
        if missing:
            raise ValueError(f"Schedule CSV is missing columns: {', '.join(missing)}")
        extra = [name for name in headings if name not in SCHEDULE_COLUMNS]
        if extra:
            raise ValueError(f"Schedule CSV has unsupported columns: {', '.join(extra)}")

        records = []
        while True:
            # line_num points past the last record so add one before reading
            row_number = reader.line_num + 1
            try:
                raw = next(reader)
            except StopIteration:
                break
            if not any(value.strip() for value in raw):
                continue
            if any(value.strip() for value in raw[len(headings):]):
                raise ValueError(f"row {row_number}: more cells than column names")
            row = dict(zip(headings, [value.strip() for value in raw[:len(headings)]], strict=False))
            row = {name: row.get(name, "") for name in SCHEDULE_COLUMNS}
            if (not re.fullmatch(r"[0-9]+", row["technology_nm"])
                    or int(row["technology_nm"]) > 2_147_483_647):
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
