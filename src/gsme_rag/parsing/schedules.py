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
    # A workbook may store real Excel dates rather than display strings.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"row {row_number}: {column} must be a valid date")
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"row {row_number}: {column} must be a valid date")


def _record(row, row_number):
    technology = str(row.get("technology_nm", "")).strip()
    if not re.fullmatch(r"[0-9]+", technology) or not 1 <= int(technology) <= 2_147_483_647:
        raise ValueError(f"row {row_number}: technology_nm must be a positive whole number")
    flavor = str(row.get("flavor", "") or "").strip()
    if not flavor or len(flavor) > 300:
        raise ValueError(f"row {row_number}: flavor is required and must not exceed 300 characters")
    from gsme_rag.parsing.fidelity import _BAD_UNICODE, _UNMAPPED_CID
    # A trailing '+' is legitimate in process names such as HPC+, not an
    # incomplete mathematical expression. Only font corruption applies here.
    if _BAD_UNICODE.search(flavor) or _UNMAPPED_CID.search(flavor):
        raise ValueError(f"row {row_number}: damaged flavor text requires source review")
    record = dict(source_row_number=row_number, technology_nm=int(technology), flavor=flavor)
    record.update({name: _date(row.get(name, ""), row_number, name) for name in DATE_COLUMNS})
    # These exports define successive milestones. A malformed chronology must
    # not be published as a trustworthy schedule merely because dates parse.
    if any(record[a] > record[b] for a, b in zip(DATE_COLUMNS, DATE_COLUMNS[1:])):
        raise ValueError(f"row {row_number}: schedule milestones are out of chronological order")
    return record


def parse_schedule_workbook(path, selected=None):
    """Return typed rows, or None for an ordinary workbook. Never evaluate cells.

    The original workbook is retained; sheet names and physical rows locate each
    record. All populated sheets must match the known schema once recognized.
    Mixed Full/Shared workbooks keep the kind on each row, not a guessed label.
    """
    from openpyxl import load_workbook
    from gsme_rag.parsing.office import check_package
    check_package(path)
    book = load_workbook(path, data_only=False, keep_links=False)
    try:
        if len(book.worksheets) > 100 or sum(s.max_row * s.max_column for s in book) > 500000:
            raise ValueError("Workbook exceeds the bounded schedule extraction limit")
        populated = []
        for sheet in book:
            rows = [row for row in sheet if any(c.value is not None for c in row)]
            if rows:
                header = [str(c.value or "").strip() for c in rows[0]]
                populated.append((sheet, rows, header))
        if not any(is_schedule_header(header) for _, _, header in populated):
            return None
        records = []
        for sheet, rows, header in populated:
            if not is_schedule_header(header):
                raise ValueError(f"sheet {sheet.title}: mixed schedule/non-schedule sheets require separate files")
            if sheet.sheet_state != "visible" or sheet.merged_cells.ranges:
                raise ValueError(f"sheet {sheet.title}: hidden or merged schedule data requires source review")
            if sheet.row_dimensions[rows[0][0].row].hidden or any(
                    sheet.column_dimensions[c.column_letter].hidden for c in rows[0] if c.value is not None):
                raise ValueError(f"sheet {sheet.title}: hidden schedule headers require source review")
            kind = resolve_schedule_kind(sheet.title.strip(), selected)
            if not kind:
                kind = resolve_schedule_kind(Path(path).name, selected)
            if not kind:
                raise ValueError(f"sheet {sheet.title}: select an explicit Full/Shared schedule kind")
            headings = [normalize_schedule_header(value) for value in header]
            while headings and not headings[-1]:
                headings.pop()
            if len(set(headings)) != len(headings) or set(headings) != set(SCHEDULE_COLUMNS):
                raise ValueError(f"sheet {sheet.title}: duplicate or unsupported schedule columns")
            for cells in rows[1:]:
                if sheet.row_dimensions[cells[0].row].hidden or any(
                    sheet.column_dimensions[c.column_letter].hidden for c in cells if c.value is not None):
                    raise ValueError(f"sheet {sheet.title}: hidden schedule records require source review")
                if any(c.data_type in {"f", "e"} for c in cells):
                    raise ValueError(f"sheet {sheet.title}: formula/error cells are not verified schedule values")
                if any(c.value is not None and str(c.value).strip() for c in cells[len(headings):]):
                    raise ValueError(f"sheet {sheet.title}: nonempty cells outside schedule columns")
                row = {name: (cells[i].value if cells[i].value is not None else "")
                       for i, name in enumerate(headings)}
                record = _record(row, cells[0].row)
                records.append(dict(record, source_sheet=sheet.title, schedule_kind=kind))
        return records
    finally:
        book.close()


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
            records.append(_record(row, row_number))
            if len(records) > 100000:
                raise ValueError("Schedule exceeds 100,000 records; split the source")
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
