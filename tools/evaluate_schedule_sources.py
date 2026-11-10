"""Local-only schedule acceptance audit against exact CSV/XLSX source values.

Uses a gsme_rag_test* database, never the development catalog or a cloud model.
Private copied originals and report remain under ignored validation/ by default.
The oracle reads CSV independently of the production schedule parser.
"""

from argparse import ArgumentParser
from datetime import date, datetime
from hashlib import sha256
import csv
import json
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
from dataclasses import replace
from openpyxl import load_workbook

from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import from_env
from gsme_rag.storage.pipeline import process_ingestion
from gsme_rag.storage.postgres import EvidenceStore


def source_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("Unexpected date in benchmark source")


def canonical(row):
    return (int(row[0]), str(row[1]).strip(), *(source_date(value) for value in row[2:6]))


def main():
    cli = ArgumentParser(description=__doc__)
    cli.add_argument("--source-dir", type=Path, required=True)
    cli.add_argument("--output", type=Path, default=Path("validation/schedule-acceptance"))
    args = cli.parse_args()
    started = perf_counter()
    settings = from_env()
    address = urlsplit(settings.database_url)
    test_url = os.getenv("GSME_TEST_DATABASE_URL") or urlunsplit(address._replace(path="/gsme_rag_test_retrieval_20261103"))
    if not urlsplit(test_url).path.startswith("/gsme_rag_test"):
        raise ValueError("Schedule acceptance requires an isolated gsme_rag_test database")
    args.output.mkdir(parents=True, exist_ok=True)
    settings = replace(settings, database_url=test_url, data_dir=args.output / "store")
    store = EvidenceStore(test_url)
    store.migrate()
    tenant = "schedule-acceptance-" + uuid4().hex
    originals, expected, submitted = {}, {}, {}
    for kind in ("full", "shared"):
        path = args.source_dir / (kind + "_schedules.csv")
        with path.open(encoding="utf-8-sig", newline="") as incoming:
            reader = csv.reader(incoming)
            next(reader)
            expected[kind] = [canonical(row) for row in reader if any(v.strip() for v in row)]
        originals[kind] = path
    workbook = args.source_dir / "Schedules unsupported file straight from web.xlsx"
    book = load_workbook(workbook, data_only=False, keep_links=False)
    try:
        for sheet in book:
            kind = sheet.title.strip().casefold()
            values = [canonical(row) for row in list(sheet.values)[1:] if any(v is not None for v in row)]
            if sorted(values) != sorted(expected[kind]):
                raise ValueError("Workbook and CSV differ; inspect source values before proceeding")
    finally:
        book.close()
    originals["workbook"] = workbook
    for label, source in originals.items():
        item = store.submit(tenant, source, settings.data_dir)
        ingestion = process_ingestion(store, settings, item["job_id"])
        if ingestion["state"] != "ready":
            raise ValueError("Source schedule ingestion failed")
        submitted[label] = dict(item, source_sha256=sha256(source.read_bytes()).hexdigest(),
                                row_count=ingestion["schedule_rows"], seconds=ingestion["seconds"])
    # Question, independently specified exact field expectations. Calendar is
    # pinned so the same acceptance run remains reproducible next month.
    cases = [
        ("Show shared schedules", "shared", {}),
        ("Show full schedules", "full", {}),
        ("List shared 28nm schedules", "shared", {"technology": 28}),
        ('Shared schedules flavor "HPC+ RF"', "shared", {"flavor": "HPC+ RF"}),
        ("Next shared tapeout", "shared", {"start": "2026-11-10", "nearest": 1}),
        ("Next 2 full tapeout dates", "full", {"start": "2026-11-10", "nearest": 2}),
        ("Shared schedules next month", "shared", {"start": "2026-12-01", "end": "2026-12-31"}),
        ("Full schedules this month", "full", {"start": "2026-11-01", "end": "2026-11-30"}),
        ("Shared schedules this week", "shared", {"start": "2026-11-09", "end": "2026-11-15"}),
        ("Full schedules next week", "full", {"start": "2026-11-16", "end": "2026-11-22"}),
        ("Full schedules today", "full", {"start": "2026-11-10", "end": "2026-11-10"}),
        ("Shared schedules tomorrow", "shared", {"start": "2026-11-11", "end": "2026-11-11"}),
        ("Shared schedules next 30 days", "shared", {"start": "2026-11-10", "end": "2026-12-09"}),
        ("Full schedules after 2026-11-10", "full", {"start": "2026-11-11"}),
        ("Full schedules on or after 2026-11-10", "full", {"start": "2026-11-10"}),
        ("Shared schedules before 2026-11-11", "shared", {"end": "2026-11-10"}),
        ("Shared trial dates between 2026-11-01 and 2026-11-30", "shared", {"field": 2, "start": "2026-11-01", "end": "2026-11-30"}),
        ("Full final deadlines on or before 2026-11-10", "full", {"field": 3, "end": "2026-11-10"}),
        ("Full estimated ship dates next month", "full", {"field": 5, "start": "2026-12-01", "end": "2026-12-31"}),
        ("Shared open schedules", "shared", {"open": True}),
        ("Full closed schedules", "full", {"open": False}),
        ("Shared schedules 999nm", "shared", {"technology": 999}),
    ]
    report = {"version": "0.18.0", "as_of": "2026-11-10", "external_calls": 0,
              "csv_workbook_exact_values_match": True, "sources": submitted, "cases": []}
    token = "synthetic-local-schedule-acceptance-token"
    with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token}), patch("gsme_rag.storage.admin._provider") as provider:
        with TestClient(create_admin_app(settings, store), base_url="http://127.0.0.1:8010", client=("127.0.0.1", 1234)) as client:
            headers = {"X-GSME-Token": token}
            for source_label in ("csv", "workbook"):
                for question, kind, filters in cases:
                    oracle = expected[kind]
                    field = filters.get("field", 4)
                    oracle = [r for r in oracle if
                              ("technology" not in filters or r[0] == filters["technology"]) and
                              ("flavor" not in filters or r[1] == filters["flavor"]) and
                              ("start" not in filters or r[field] >= date.fromisoformat(filters["start"])) and
                              ("end" not in filters or r[field] <= date.fromisoformat(filters["end"])) and
                              ("open" not in filters or (r[3] >= date(2026, 11, 10)) == filters["open"])]
                    if filters.get("nearest"):
                        dates = sorted({r[field] for r in oracle})[:filters["nearest"]]
                        oracle = [r for r in oracle if r[field] in dates]
                    body = dict(tenant=tenant, question=question, as_of="2026-11-10", route="auto",
                                document_id=submitted[kind if source_label == "csv" else "workbook"]["document_id"])
                    response = client.post("/api/retrieve", headers=headers, json=body)
                    value = response.json()
                    actual = [tuple([r["technology_nm"], r["flavor"]] + [date.fromisoformat(r[k]) for k in
                              ("trial_deadline", "final_deadline", "tapeout_date", "estimated_ship_date")])
                              for r in value.get("results", [])]
                    passed = response.status_code == 200 and value.get("route") == "schedules" and sorted(actual) == sorted(oracle) and value.get("total") == len(oracle)
                    report["cases"].append(dict(source=source_label, question=question, passed=passed,
                        expected_rows=len(oracle), actual_rows=len(actual), status=value.get("status"),
                        http_status=response.status_code, message=value.get("message", value.get("detail")),
                        timings_ms=value.get("timings_ms"), source_records=[r["record_id"] for r in value.get("results", [])]))
            provider.assert_not_called()
    report["passed"] = sum(c["passed"] for c in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    report["elapsed_seconds"] = round(perf_counter() - started, 3)
    (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("passed", "failed", "elapsed_seconds", "external_calls", "csv_workbook_exact_values_match")}))
    raise SystemExit(bool(report["failed"]))


if __name__ == "__main__":
    main()
