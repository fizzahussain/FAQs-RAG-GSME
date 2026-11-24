"""Public widget fidelity, spelling boundaries and source freshness regressions."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import uuid4

from gsme_rag.retrieval.spelling import correct_query
from gsme_rag.schedules.muse import normalize_snapshot, ScheduleSourceUnavailable, write_evidence
from gsme_rag.schedules.sync import MuseScheduleSync


def payload(kind="full", extra=None):
    row = {"Tech (nm)":"180","Flavor":"MS RF G","Trial":"11/18/2026","Final":"11/25/2026",
           "Tapeout":"12/02/2026","Est. Ship":"02/03/2027"}
    return {"success":"true","data":[row]+(extra or [])}


def snapshot(data=None,kind="full"):
    data = data or payload()
    return normalize_snapshot(kind,data,[list(r.values()) for r in data.get("data",[])])


class MuseFidelityTests(TestCase):
    def test_every_rendered_cell_must_match_not_just_the_first_technology(self):
        good = payload(extra=[{**payload()["data"][0],"Tech (nm)":"28","Flavor":"HPC+ RF"}])
        result = snapshot(good)
        self.assertEqual({r["technology_nm"] for r in result.rows},{28,180})
        with self.assertRaises(ScheduleSourceUnavailable):
            normalize_snapshot("full",good,[list(good["data"][0].values())])
        visible = [list(r.values()) for r in good["data"]]
        visible[0][-1] = "02/04/2027"
        with self.assertRaises(ScheduleSourceUnavailable):
            normalize_snapshot("full",good,visible)

    def test_live_full_source_bad_year_is_preserved_and_quarantined(self):
        bad = {**payload()["data"][0],"Tech (nm)":"22","Flavor":"ULP","Est. Ship":"03/17/0206"}
        result = snapshot(payload(extra=[bad]))
        self.assertEqual(len(result.rows),1)
        self.assertEqual(result.quarantined[0]["original"]["Est. Ship"],"03/17/0206")
        self.assertIn("03/17/0206",result.csv_bytes.decode())
        self.assertNotIn("03/17/2027",result.csv_bytes.decode())

    def test_empty_duplicate_unknown_column_and_all_invalid_sources_fail(self):
        bad = payload(); bad["data"][0]["Tapeout"]="not a date"
        duplicate = payload(extra=[payload()["data"][0]])
        renamed = payload(); renamed["data"][0]["Changed header"] = renamed["data"][0].pop("Flavor")
        for data in ({"success":"true","data":[]},duplicate,renamed,bad,{"success":"false"}):
            with self.subTest(data=data), self.assertRaises(ScheduleSourceUnavailable):
                snapshot(data)

    def test_content_addressed_evidence_is_verified_when_reused(self):
        with TemporaryDirectory() as directory:
            current = snapshot()
            path,digest = write_evidence(Path(directory),current)
            self.assertEqual(write_evidence(Path(directory),current),(path,digest))
            path.write_bytes(b"tampered")
            with self.assertRaises(ScheduleSourceUnavailable):
                write_evidence(Path(directory),current)

    def test_freshness_expiration_failure_cooldown_and_zero_age(self):
        sync = MuseScheduleSync(Mock(),"tenant")
        now = datetime.now(timezone.utc)
        fresh = dict(schedule_kind="full",version_id=uuid4(),document_id=uuid4(),checked_at=now,last_error=None,last_attempt_at=now)
        with patch.object(sync,"status",return_value=[fresh]),patch.object(sync,"refresh") as refresh:
            self.assertEqual(sync.ensure_fresh("full"),[fresh]); refresh.assert_not_called()
        stale = {**fresh,"checked_at":now-timedelta(days=2)}
        with patch.object(sync,"status",side_effect=[[stale],[fresh]]),patch.object(sync,"refresh") as refresh:
            sync.ensure_fresh("full"); refresh.assert_called_once()
        failed = {**stale,"last_error":"Source invalid"}
        with patch.object(sync,"status",return_value=[failed]),patch.object(sync,"refresh") as refresh:
            with self.assertRaises(ScheduleSourceUnavailable): sync.ensure_fresh("full")
            refresh.assert_not_called()
        with patch.dict(os.environ,{"GSME_MUSE_MAX_AGE_SECONDS":"0"}),patch.object(sync,"status",return_value=[fresh]),patch.object(sync,"refresh") as refresh:
            sync.ensure_fresh("full"); refresh.assert_called_once()

    def test_query_typos_preserve_values_identifiers_negation_and_flavors(self):
        for before,after in (("When was Muse foudned?","When was Muse founded?"),
                             ("What is a technolgy node?","What is a technology node?"),
                             ("semicondutor manufaturing stages","semiconductor manufacturing stages"),
                             ("fabelss vs IDM","fabless vs IDM"),
                             ("next shared tape outs","next shared tapeouts")):
            self.assertEqual(correct_query(before)[0],after)
        for question in ('not 28nm; LOT A17; IDM; 2026-11-24',
                         'flavor "technolgy"', 'flavor tapout', '`tapout`',
                         'https://example.test/tapout', '"tape out"', 'IDP is not IDM'):
            self.assertEqual(correct_query(question),(question,[]))

    def test_failed_mandatory_refresh_never_queries_old_schedule_rows(self):
        from gsme_rag.retrieval.schedules import ScheduleQuery, search_schedules
        store = Mock()
        with patch.dict(os.environ,{"GSME_MUSE_SYNC_ENABLED":"yes"}), patch.object(
                MuseScheduleSync,"ensure_fresh",side_effect=ScheduleSourceUnavailable("Synthetic outage")):
            with self.assertRaises(ScheduleSourceUnavailable):
                search_schedules(store,"tenant",ScheduleQuery(question="Show full schedules"))
        store.schedule_search.assert_not_called()

    def test_live_search_discloses_quarantine_and_rejects_manual_snapshot(self):
        from gsme_rag.retrieval.schedules import ScheduleQuery, search_schedules
        document_id,version_id = uuid4(),uuid4()
        fresh = dict(document_id=document_id,version_id=version_id,schedule_kind="full",
                     source_url="https://www.musesemi.com/full-block-tapeout-schedule",
                     checked_at=datetime.now(timezone.utc),quarantined_rows=2)
        store = Mock()
        store.schedule_search.return_value = {"rows":[],"total":0,"sources":[{"document_id":document_id}]}
        with patch.dict(os.environ,{"GSME_MUSE_SYNC_ENABLED":"yes"}), patch.object(
                MuseScheduleSync,"ensure_fresh",return_value=[fresh]):
            result = search_schedules(store,"tenant",ScheduleQuery(question="Show full schedules"))
            self.assertIn("2 website rows",result["warnings"][-1])
            self.assertEqual(result["sources"][0]["checked_at"],fresh["checked_at"])
            store.schedule_search.reset_mock()
            with self.assertRaises(ScheduleSourceUnavailable):
                search_schedules(store,"tenant",ScheduleQuery(question="Show full schedules"),document_id=uuid4())
            store.schedule_search.assert_not_called()

    def test_human_date_phrasing_preserves_calendar_constraints(self):
        from gsme_rag.retrieval.schedules import ScheduleQuery,plan_schedule
        for question,start,end in (
            ("Show shared schedules in December 2026","2026-12-01","2026-12-31"),
            ("Show full schedules between 1 December 2026 and 31 December 2026","2026-12-01","2026-12-31"),
            ("Show shared schedules after December 1, 2026","2026-12-02",None),
            ("Show feuel scedules after 1 Dec 2026","2026-12-02",None)):
            result,_,reasons=plan_schedule(ScheduleQuery(question=question))
            self.assertEqual(reasons,[],question)
            self.assertEqual(result.date_from.isoformat(),start)
            self.assertEqual(result.date_to.isoformat() if result.date_to else None,end)
        for question in ("Show shared schedules after 31 February 2026", "Show shared schedules not in December 2026"):
            self.assertTrue(plan_schedule(ScheduleQuery(question=question))[2])


DB = os.getenv("GSME_TEST_DATABASE_URL","")


@skipUnless(urlsplit(DB).path.startswith("/gsme_rag_test"),"Requires isolated gsme_rag_test PostgreSQL")
class MusePostgresTests(TestCase):
    def test_atomic_replacement_noop_history_and_failed_refresh_preserve_authority(self):
        from gsme_rag.storage.postgres import EvidenceStore
        from gsme_rag.retrieval.schedules import ScheduleQuery
        store = EvidenceStore(DB); store.migrate()
        tenant = "muse-test-"+uuid4().hex
        website = Mock()
        website.fetch.side_effect = lambda kind:snapshot(kind=kind)
        with TemporaryDirectory() as directory:
            sync = MuseScheduleSync(store,tenant,directory,website)
            try:
                first = sync.refresh()
                second = sync.refresh()
                self.assertTrue(all(s["changed"] for s in first))
                self.assertTrue(all(not s["changed"] for s in second))
                self.assertEqual([s["version_id"] for s in first],[s["version_id"] for s in second])
                with store.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) AS n FROM muse_schedule_checks WHERE tenant_id=%s",(tenant,)).fetchone()["n"],4)
                website.fetch.side_effect = ScheduleSourceUnavailable("Synthetic widget outage")
                with self.assertRaises(ScheduleSourceUnavailable): sync.refresh()
                self.assertEqual([str(s["version_id"]) for s in sync.status()],[s["version_id"] for s in first])
                with self.assertRaises(ScheduleSourceUnavailable): sync.ensure_fresh("full")
            finally:
                with store.connect() as db:
                    db.execute("DELETE FROM muse_schedule_checks WHERE tenant_id=%s",(tenant,))
                    db.execute("DELETE FROM muse_schedule_sources WHERE tenant_id=%s",(tenant,))
                    db.execute("UPDATE document_access SET deletion_protected=false WHERE tenant_id=%s",(tenant,))
                    db.execute("DELETE FROM document_access WHERE tenant_id=%s",(tenant,))
                    db.execute("UPDATE documents SET current_version_id=NULL WHERE tenant_id=%s",(tenant,))
                    db.execute("DELETE FROM ingestion_jobs WHERE tenant_id=%s",(tenant,))
                    db.execute("DELETE FROM schedule_rows WHERE tenant_id=%s",(tenant,))
                    db.execute("DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE tenant_id=%s)",(tenant,))
                    db.execute("DELETE FROM documents WHERE tenant_id=%s",(tenant,))
