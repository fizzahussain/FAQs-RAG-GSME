"""Deterministic schedule grammar, provenance and local API regressions."""

from datetime import date, datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.schedules import parse_schedule_workbook, parse_schedule_records
from gsme_rag.retrieval.schedules import ScheduleQuery, plan_schedule, search_schedules, ScheduleSnapshotChanged
from gsme_rag.storage.admin import create_admin_app
from gsme_rag.storage.config import Settings


HEADERS = ["Tech (nm)", "Flavor", "Trial", "Final", "Tapeout", "Est. Ship"]
ROW = [28, "HPC+ RF", "2026-10-01", "2026-10-15", "2026-11-01", "2026-12-01"]


class SchedulePlanningTests(TestCase):
    def plan(self, question="", **kwargs):
        return plan_schedule(ScheduleQuery(question=question, as_of=date(2026, 11, 10), **kwargs))

    def test_exact_technology_flavor_and_date_range(self):
        p, today, errors = self.plan('Show shared 28 nm schedules flavor "HPC+ RF" between 2026-11-01 and 2026-12-31')
        self.assertEqual(errors, [])
        self.assertEqual((p.schedule_kind, p.technology_nm, p.flavor), ("shared", 28, "HPC+ RF"))
        self.assertEqual((p.date_from, p.date_to), (date(2026, 11, 1), date(2026, 12, 31)))

    def test_relative_calendar_windows_and_year_leap_boundaries(self):
        for phrase, start, end in [("today", "2026-11-10", "2026-11-10"),
                                   ("tomorrow", "2026-11-11", "2026-11-11"),
                                   ("this week", "2026-11-09", "2026-11-15"),
                                   ("next week", "2026-11-16", "2026-11-22"),
                                   ("this month", "2026-11-01", "2026-11-30"),
                                   ("next month", "2026-12-01", "2026-12-31"),
                                   ("next 7 days", "2026-11-10", "2026-11-16")]:
            with self.subTest(phrase=phrase):
                p, _, reasons = self.plan("Show full schedules " + phrase)
                self.assertEqual(reasons, [])
                self.assertEqual((p.date_from.isoformat(), p.date_to.isoformat()), (start, end))
                self.assertIsNone(p.next_dates)
        p, _, errors = plan_schedule(ScheduleQuery(question="Full schedules next month", as_of=date(2024, 1, 31)))
        self.assertFalse(errors)
        self.assertEqual(p.date_to, date(2024, 2, 29))
        p, _, errors = plan_schedule(ScheduleQuery(question="Full schedules next month", as_of=date(2026, 12, 31)))
        self.assertEqual(p.date_from, date(2027, 1, 1))

    def test_next_dates_preserve_explicit_start_and_milestone(self):
        p, _, reasons = self.plan("Next 3 shared trial dates after 2026-11-10")
        self.assertFalse(reasons)
        self.assertEqual((p.next_dates, p.date_field, p.date_from), (3, "trial_deadline", date(2026, 11, 11)))
        p, _, reasons = self.plan("Show full estimated ship dates on or before 2026-12-31")
        self.assertFalse(reasons)
        self.assertEqual((p.date_field, p.date_to), ("estimated_ship_date", date(2026, 12, 31)))
        p, _, reasons = self.plan(schedule_kind="both", next_dates=1)
        self.assertEqual(p.date_from, date(2026, 11, 10))

    def test_ambiguous_invalid_negated_or_contradictory_filters_clarify(self):
        for question in ["List tapeout dates", "Show shared 28nm or 65nm schedules",
                         "Shared schedules after 11/10/2026", "Shared schedules in December",
                         "Shared schedules not 28nm", "Shared schedules HPC+ RF",
                         "Shared schedules between 2026-12-01 and 2026-11-01",
                         "Shared schedules on 2026-02-30", "Shared schedules next 0 days",
                         "Shared schedules today and tomorrow", "Shared trial and final deadlines",
                         "Shared schedules flavor HPC", "Shared schedules next 101 dates",
                         "Shared schedules before 0001-01-01", "Shared deadlines"]:
            with self.subTest(question=question):
                _, _, reasons = self.plan(question)
                self.assertTrue(reasons)
        self.assertTrue(self.plan("Shared schedules", schedule_kind="full")[2])
        self.assertTrue(self.plan("Shared schedules 28nm", technology_nm=65)[2])
        self.assertTrue(self.plan("Shared schedules on or after 2026-12-01 except 65nm")[2])

    def test_timezone_validation_and_status_boundary(self):
        with self.assertRaises(ValueError):
            ScheduleQuery(timezone="not-a-timezone")
        for status in ("open", "closed"):
            p, _, errors = self.plan("Show full " + status + " schedules")
            self.assertFalse(errors)
            self.assertEqual(p.deadline_status, status)

    def test_stable_ids_snapshot_and_no_cloud_calls(self):
        store = Mock()
        def response(*args, **kwargs):
            return {"rows": [dict(version_id="v", source_sheet="shared", source_row_number=2)], "total": 2, "sources": ["v"]}
        store.schedule_search.side_effect = response
        first = search_schedules(store, "t", ScheduleQuery(schedule_kind="shared", limit=1))
        second = search_schedules(store, "t", ScheduleQuery(schedule_kind="shared", limit=1, snapshot=first["snapshot"]))
        self.assertEqual(first["results"][0]["record_id"], second["results"][0]["record_id"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 1)
        with self.assertRaises(ScheduleSnapshotChanged):
            search_schedules(store, "t", ScheduleQuery(schedule_kind="full", snapshot=first["snapshot"]))
        store.retrieval_scope.assert_not_called()


class ScheduleWorkbookTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "schedules.xlsx"

    def book(self):
        book = Workbook()
        book.active.title = "shared"
        book.active.append(HEADERS + [None])
        book.active.append(ROW)
        sheet = book.create_sheet("full ")
        sheet.append(HEADERS)
        sheet.append(ROW)
        return book

    def test_mixed_workbook_keeps_kind_sheet_row_and_source_hash(self):
        book = self.book()
        book["full "]["C2"] = datetime(2026, 10, 1)
        book.save(self.path)
        rows = parse_schedule_workbook(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["schedule_kind"] for r in rows}, {"full", "shared"})
        self.assertEqual([r["source_row_number"] for r in rows], [2, 2])
        self.assertEqual(rows[1]["source_sheet"], "full ")
        parsed = parse_to_markdown(self.path)
        self.assertEqual(parsed.metadata.document_class, "schedule")
        self.assertEqual(parsed.metadata.classification_status, "ready")
        self.assertEqual(len(parsed.metadata.content_sha256), 64)
        with self.assertRaisesRegex(ValueError, "filename says full"):
            parse_schedule_workbook(self.path, "shared")

    def test_formula_hidden_merge_extra_column_and_invalid_chronology_rejected(self):
        changes = [lambda b: setattr(b.active["E2"], "value", "=TODAY()"),
                   lambda b: setattr(b.active, "sheet_state", "hidden"),
                   lambda b: setattr(b.active.row_dimensions[2], "hidden", True),
                   lambda b: b.active.merge_cells("A2:B2"),
                   lambda b: setattr(b.active["G2"], "value", "not discarded"),
                   lambda b: setattr(b.active["D2"], "value", "2026-01-01"),
                   lambda b: setattr(b.active["C2"], "value", 42)]
        for change in changes:
            with self.subTest(change=change):
                book = self.book()
                change(book)
                book.save(self.path)
                with self.assertRaises(ValueError):
                    parse_schedule_workbook(self.path)

    def test_unknown_populated_sheet_not_silently_dropped(self):
        book = self.book()
        book.create_sheet("notes").append(["Data that must not disappear"])
        book.save(self.path)
        with self.assertRaisesRegex(ValueError, "separate files"):
            parse_schedule_workbook(self.path)

    def test_csv_milestone_and_positive_technology_validation(self):
        path = self.path.with_suffix(".csv")
        for technology, final in [("0", "2026-10-15"), ("28", "2026-01-01")]:
            path.write_text(",".join(HEADERS) + "\n" + ",".join(map(str, [technology, ROW[1], ROW[2], final, ROW[4], ROW[5]])), encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_schedule_records(path)
        path.write_text(",".join(HEADERS) + "\n" + ",".join(map(str, [28, "HPC+", *ROW[2:]])), encoding="utf-8")
        self.assertEqual(parse_schedule_records(path)[0]["flavor"], "HPC+")


class ScheduleApiTests(TestCase):
    def test_auth_local_search_catalog_validation_and_snapshot_conflict(self):
        store = Mock()
        store.schedule_search.return_value = {"rows": [], "total": 0, "sources": []}
        store.schedule_catalog.return_value = {"facets": [], "sources": []}
        token = "synthetic-schedule-token-at-least-20"
        with patch.dict(os.environ, {"GSME_REVIEW_AUTH_TOKEN": token}), patch("gsme_rag.storage.admin._provider") as provider:
            with TestClient(create_admin_app(Settings("unused", "unused", Path("data")), store),
                            base_url="http://127.0.0.1:8010", client=("127.0.0.1", 12345)) as client:
                body = {"tenant": "t", "question": "Next shared 28nm tapeout", "as_of": "2026-11-10"}
                self.assertEqual(client.post("/api/schedule-search", json=body).status_code, 401)
                headers = {"X-GSME-Token": token}
                result = client.post("/api/schedule-search", headers=headers, json=body)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["filters"]["technology_nm"], 28)
                self.assertEqual(result.json()["filters"]["date_from"], "2026-11-10")
                self.assertEqual(client.get("/api/schedule-catalog?tenant=t", headers=headers).status_code, 200)
                self.assertEqual(client.post("/api/schedule-search", headers=headers, json={**body, "snapshot": "a" * 64}).status_code, 409)
                self.assertEqual(client.post("/api/schedule-search", headers=headers, json={**body, "date_field": "evil"}).status_code, 422)
                self.assertEqual(client.post("/api/schedule-search", headers=headers, json={**body, "timezone": "invalid"}).status_code, 422)
                provider.assert_not_called()
