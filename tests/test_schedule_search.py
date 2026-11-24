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
from gsme_rag.retrieval.schedules import (ScheduleFilters, ScheduleQuery, plan_schedule,
                                         search_schedules, ScheduleSnapshotChanged)
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

    def test_obvious_schedule_kind_typos_are_corrected_with_explicit_assumption(self):
        for question, expected in (("Show ful schedules", "full"),
                                   ("Show feuel schedules", "full"),
                                   ("Show feuel 180nm schedules", "full"),
                                   ("Show F-U-L schedules", "full"),
                                   ("Show F-U-L-L schedules", "full"),
                                   ("Show shraed schedules", "shared"),
                                   ("Show btoh schedules", "both")):
            planned, _, errors = self.plan(question)
            self.assertEqual(errors, [], question)
            self.assertEqual(planned.schedule_kind, expected, question)

        # Preserve typo-like text in a flavor field; it is an exact identifier.
        planned, _, errors = self.plan('Show shared schedules flavor "feuel"')
        self.assertEqual(errors, [])
        self.assertEqual(planned.flavor, "feuel")
        for question in ('Show shared schedules flavor feuel', 'Show shared schedules flavor "shraed"'):
            planned, _, errors = self.plan(question)
            self.assertEqual(errors, [], question)
            self.assertEqual(planned.schedule_kind, "shared")
        for question in ("Show fuel schedules", "Show fill schedules", "Show full schedules not 180nm"):
            self.assertTrue(self.plan(question)[2], question)
        previous = ScheduleFilters(schedule_kind="shared", technology_nm=180)
        planned, _, errors = self.plan("What about feuel?", previous_filters=previous)
        self.assertEqual(errors, [])
        self.assertEqual((planned.schedule_kind, planned.technology_nm), ("full", 180))

    def test_schedule_search_reports_assumed_correction_to_user(self):
        store = Mock()
        store.schedule_search.return_value = {"rows": [], "total": 0, "sources": ["v"]}
        result = search_schedules(store, "t", ScheduleQuery(question="Show feuel schedules"))
        self.assertEqual(result["assumptions"], ['Assuming “feuel” meant “Full”.'])
        self.assertEqual(result["filters"]["schedule_kind"], "full")
        self.assertEqual(result["status"], "no_matching_rows")

    def context(self, question, previous=None, **kwargs):
        planned, _, errors = self.plan(question, previous_filters=previous, **kwargs)
        self.assertEqual(errors, [], question)
        return planned, planned.model_dump(include=set(ScheduleFilters.model_fields))

    def test_schedule_followups_change_only_requested_filters(self):
        _, previous = self.context("Show full schedules between 2026-11-01 and 2026-12-31")
        p, previous = self.context("Only 180nm", previous)
        self.assertEqual((p.schedule_kind, p.technology_nm, p.date_from, p.date_to),
                         ("full", 180, date(2026, 11, 1), date(2026, 12, 31)))
        p, previous = self.context("What about Shared?", previous)
        self.assertEqual((p.schedule_kind, p.technology_nm), ("shared", 180))
        p, previous = self.context("And with flavor MS RF G", previous)
        self.assertEqual((p.schedule_kind, p.technology_nm, p.flavor), ("shared", 180, "MS RF G"))
        p, previous = self.context("And only final deadlines", previous)
        self.assertEqual(p.date_field, "final_deadline")
        p, previous = self.context("All flavors", previous)
        self.assertIsNone(p.flavor)
        p, previous = self.context("All technologies", previous)
        self.assertIsNone(p.technology_nm)
        p, previous = self.context("All dates", previous)
        self.assertEqual((p.date_from, p.date_to, p.next_dates), (None, None, None))
        # A standalone question deliberately starts fresh despite supplied history.
        p, _ = self.context("Show full schedules", previous)
        self.assertEqual((p.schedule_kind, p.technology_nm, p.flavor, p.date_field),
                         ("full", None, None, "tapeout_date"))

    def test_followup_date_replacement_and_explicit_filter_conflicts(self):
        _, previous = self.context("Show shared 180nm schedules between 2026-11-01 and 2026-11-30")
        for question in ("After 2026-12-01", "What about next month?", "Next two dates"):
            p, _ = self.context(question, previous)
            self.assertEqual((p.schedule_kind, p.technology_nm), ("shared", 180))
            if question.startswith("After"):
                self.assertEqual((p.date_from, p.date_to), (date(2026, 12, 2), None))
            elif "month" in question:
                self.assertEqual((p.date_from, p.date_to), (date(2026, 12, 1), date(2026, 12, 31)))
            else:
                self.assertEqual((p.next_dates, p.date_to), (2, None))
        for question in ("Only 180nm", "What about Shared?"):
            self.assertTrue(self.plan(question, previous_filters=previous,
                                      schedule_kind="full", technology_nm=28)[2])
        self.assertTrue(self.plan("Only 180nm")[2])
        self.assertTrue(self.plan("Only 28nm or 65nm", previous_filters=previous)[2])
        self.assertTrue(self.plan("And not 28nm", previous_filters=previous)[2])
        with self.assertRaises(ValueError):
            ScheduleQuery(question="Only 180nm", previous_filters={"tenant_id": "another-company"})
        with self.assertRaises(ValueError):
            ScheduleQuery(previous_filters={"date_from": "2026-12-31", "date_to": "2026-12-01"})

    def test_clear_specific_filters_preserves_the_other_filters(self):
        _, previous = self.context('Show shared 180nm schedules flavor "MS RF G" after 2026-11-01')
        for question, field in (("Remove flavor filter", "flavor"),
                                ("Remove the technology filter", "technology_nm"),
                                ("Remove date filter", "date_from")):
            p, _ = self.context(question, previous)
            self.assertIsNone(getattr(p, field))
            self.assertEqual(p.schedule_kind, "shared")

    def test_followup_can_replace_status_and_reuse_explicit_milestone_context(self):
        _, previous = self.context("Show full open final deadlines after 2026-11-01")
        p, previous = self.context("Only closed deadlines", previous)
        self.assertEqual((p.deadline_status, p.date_field), ("closed", "final_deadline"))
        p, _ = self.context("All statuses", previous)
        self.assertEqual(p.deadline_status, "all")
        self.assertTrue(self.plan("Only open and closed deadlines", previous_filters=previous)[2])

    def test_unquoted_flavors_preserve_identifiers_and_following_constraints(self):
        for flavor in ("MS RF G", "HPC+ RF", "HV BCD Gen2", "FFC TUFP"):
            for suffix in ("", " between 2026-11-01 and 2026-12-31"):
                with self.subTest(flavor=flavor, suffix=suffix):
                    question = f"Show shared 180nm schedules with flavor {flavor}{suffix}"
                    plain, _, errors = self.plan(question)
                    quoted, _, quoted_errors = self.plan(
                        f'Show shared 180nm schedules with flavor "{flavor}"{suffix}')
                    self.assertEqual(errors, [])
                    self.assertEqual(quoted_errors, [])
                    self.assertEqual(plain.flavor, flavor)
                    self.assertEqual(plain.model_dump(exclude={"question"}),
                                     quoted.model_dump(exclude={"question"}))
        p, _, errors = self.plan("Show shared schedules flavor MS RF G final deadlines before 2026-12-01")
        self.assertEqual(errors, [])
        self.assertEqual((p.flavor, p.date_field, p.date_to),
                         ("MS RF G", "final_deadline", date(2026, 11, 30)))
        # Exclusions and conflicting explicit filters must remain visible.
        for suffix in (" except 28nm", " or 65nm", " not 28nm"):
            self.assertTrue(self.plan("Show shared 180nm schedules flavor MS RF G" + suffix)[2])
        self.assertTrue(self.plan("Shared schedules flavor MS RF G", flavor="ULL")[2])
        self.assertTrue(self.plan('Shared schedules flavor "MS RF G')[2])

    def test_schedule_paraphrases_resolve_to_the_same_filters(self):
        groups = [
            ("Next shared tapeout", [
                "When is the next Shared Block run?",
                "Could you show me the nearest shared shuttle?",
                "What is the soonest shared tape-out?",
                "When is the earliest shared tape out?",
                "When does the next shared tapeout happen?",
                "Are there any next shared runs?",
            ], {}),
            ("Next 3 full tapeout dates after 2026-11-30", [
                "Could you list the next three Full Block runs after 2026-11-30?",
                "What are the nearest three full tape-outs after 2026-11-30?",
            ], {}),
            ("Shared schedules 180nm", [
                "What options do you have for the shared 180-nanometer process?",
                "Could you find shared runs for the 180 nanometre technology node?",
            ], {}),
            ("Shared schedules 180nm", [
                "What options do you have for 180nm?",
            ], {"schedule_kind": "shared"}),
            ("Shared schedules 180nm flavor MS RF G", [
                "Could you show shared 180nm options with flavor MS RF G?",
                "Which shared 180nm schedules have flavour MS RF G?",
                "Show shared 180nm schedules with flavor is MS RF G?",
                "I'm looking for shared 180 nanometre runs with flavor MS RF G",
                "Which shared 180nm flavor MS RF G dates are there?",
            ], {}),
        ]
        for canonical, variants, kwargs in groups:
            expected, _, errors = self.plan(canonical, **kwargs)
            self.assertFalse(errors)
            for question in variants:
                with self.subTest(question=question):
                    actual, _, errors = self.plan(question, **kwargs)
                    self.assertEqual(errors, [])
                    self.assertEqual(actual.model_dump(exclude={"question"}),
                                     expected.model_dump(exclude={"question"}))
                    self.assertEqual(actual.question, question)

    def test_paraphrases_keep_unknown_constraints_and_flavor_text(self):
        for question in (
            "Could you show shared runs without 180nm?",
            "What shared options do you have under 100 dollars?",
            "Could you show the next two shared runs except 28nm?",
            "What are the fastest shared runs?",
            "Could you book a shared shuttle?",
            "Could you show the next shared runs in December?",
            "What options do you have for 180nm?",
        ):
            with self.subTest(question=question):
                self.assertTrue(self.plan(question)[2])
        p, _, errors = self.plan('Show shared schedules flavor "Next Gen2"')
        self.assertEqual(errors, [])
        self.assertEqual(p.flavor, "Next Gen2")
        self.assertIsNone(p.next_dates)
        self.assertTrue(self.plan("When is the next shared run?", schedule_kind="full")[2])

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
                         "Shared schedules flavor", "Shared schedules next 101 dates",
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
    def test_hosted_followup_requeries_current_scope_without_models(self):
        from gsme_rag.hosted.app import create_hosted_app
        from unittest.mock import MagicMock
        store, scoped = Mock(), Mock()
        store.connect.return_value = MagicMock()
        scoped.schedule_search.return_value = {"rows": [], "total": 0, "sources": ["current-version"]}
        with patch("gsme_rag.hosted.app.AccessStore", return_value=scoped), \
             patch("gsme_rag.hosted.app.CohereEmbeddings") as embedding, \
             patch("gsme_rag.hosted.app.generate_answer") as answer:
            with TestClient(create_hosted_app(Settings("unused", "unused", Path("data")),
                                             store=store, auto_worker=False),
                            base_url="http://127.0.0.1:8020") as client:
                first = client.post("/api/schedules/search", json={
                    "question": "Show full 180nm schedules", "as_of": "2026-11-20"}).json()
                previous = {key: first["filters"][key] for key in ScheduleFilters.model_fields}
                followup = client.post("/api/schedules/search", json={
                    "question": "What about Shared?", "previous_filters": previous,
                    "as_of": "2026-11-20"})
                self.assertEqual(followup.status_code, 200)
                self.assertTrue(followup.json()["context_applied"])
                self.assertEqual(followup.json()["filters"]["schedule_kind"], "shared")
                self.assertEqual(followup.json()["filters"]["technology_nm"], 180)
                self.assertNotIn("previous_filters", followup.json()["filters"])
                self.assertEqual(scoped.schedule_search.call_count, 2)
                scoped.schedule_search.reset_mock()
                ambiguous = client.post("/api/schedules/search", json={
                    "question": "Only 28nm or 65nm", "previous_filters": previous})
                self.assertEqual(ambiguous.json()["status"], "clarification_needed")
                scoped.schedule_search.assert_not_called()
                invalid = client.post("/api/schedules/search", json={
                    "question": "Only 180nm", "previous_filters": {"tenant": "other"}})
                self.assertEqual(invalid.status_code, 422)
        embedding.assert_not_called(); answer.assert_not_called()

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
