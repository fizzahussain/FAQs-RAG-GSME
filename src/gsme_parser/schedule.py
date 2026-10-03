import csv
from datetime import date, datetime
from pathlib import Path
import re

from gsme_parser.models import ParseResult, ParseWarning, ScheduleRecord


HEADER_ALIASES = {
    "technology_nm": {"technology_nm", "technology", "tech_nm", "tech (nm)", "tech"},
    "flavor": {"flavor", "process_flavor", "process"},
    "trial_deadline": {"trial_deadline", "trial", "trial date"},
    "final_deadline": {"final_deadline", "final", "final date"},
    "tapeout_date": {"tapeout_date", "tapeout", "tapeout date"},
    "estimated_ship_date": {
        "estimated_ship_date",
        "estimated shipment",
        "estimated ship",
        "est. ship",
        "est ship",
    },
}


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    normalized = {_normalize_header(name): name for name in fieldnames if name}
    result = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                result[canonical] = normalized[alias]
                break
    return result


def _parse_date(value: str) -> date:
    cleaned = value.strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported date format: {value!r}")


def _infer_schedule_type(path: Path) -> str | None:
    name = path.stem.casefold()
    if "shared" in name:
        return "shared"
    if "full" in name:
        return "full"
    return None


def parse_schedule_csv(
    path: str | Path,
    schedule_type: str | None = None,
) -> ParseResult:
    input_path = Path(path)
    chosen_type = (schedule_type or _infer_schedule_type(input_path) or "").strip().lower()
    if chosen_type not in {"shared", "full"}:
        raise ValueError("schedule_type must be shared or full")

    result = ParseResult()
    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            result.warnings.append(
                ParseWarning(code="missing_header", message="CSV file has no header row")
            )
            return result

        mapping = _header_map(reader.fieldnames)
        missing = [name for name in HEADER_ALIASES if name not in mapping]
        if missing:
            result.warnings.append(
                ParseWarning(
                    code="missing_columns",
                    message=f"Missing required columns: {', '.join(missing)}",
                    location="header",
                )
            )
            return result

        for row_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                technology_nm = int((row[mapping["technology_nm"]] or "").strip())
                flavor = (row[mapping["flavor"]] or "").strip()
                trial = _parse_date(row[mapping["trial_deadline"]] or "")
                final = _parse_date(row[mapping["final_deadline"]] or "")
                tapeout = _parse_date(row[mapping["tapeout_date"]] or "")
                estimated_ship = _parse_date(row[mapping["estimated_ship_date"]] or "")
                if technology_nm <= 0:
                    raise ValueError("technology_nm must be positive")
                if not flavor:
                    raise ValueError("flavor is empty")
                if not trial <= final <= tapeout <= estimated_ship:
                    raise ValueError(
                        "dates must satisfy trial <= final <= tapeout <= estimated shipment"
                    )
            except (TypeError, ValueError) as error:
                result.warnings.append(
                    ParseWarning(
                        code="invalid_schedule_row",
                        message=str(error),
                        location=f"row {row_number}",
                    )
                )
                continue

            result.schedule_records.append(
                ScheduleRecord(
                    schedule_type=chosen_type,
                    technology_nm=technology_nm,
                    flavor=flavor,
                    trial_deadline=trial,
                    final_deadline=final,
                    tapeout_date=tapeout,
                    estimated_ship_date=estimated_ship,
                    source_file=input_path.name,
                    source_row=row_number,
                )
            )

    return result


def write_schedule_csv(records: list[ScheduleRecord], output_path: str | Path) -> None:
    fields = [
        "schedule_id",
        "schedule_type",
        "technology_nm",
        "flavor",
        "trial_deadline",
        "final_deadline",
        "tapeout_date",
        "estimated_ship_date",
        "source_file",
        "source_row",
    ]
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "schedule_id": record.schedule_id,
                    "schedule_type": record.schedule_type,
                    "technology_nm": record.technology_nm,
                    "flavor": record.flavor,
                    "trial_deadline": record.trial_deadline.isoformat(),
                    "final_deadline": record.final_deadline.isoformat(),
                    "tapeout_date": record.tapeout_date.isoformat(),
                    "estimated_ship_date": record.estimated_ship_date.isoformat(),
                    "source_file": record.source_file,
                    "source_row": record.source_row,
                }
            )
