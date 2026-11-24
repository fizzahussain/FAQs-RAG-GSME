"""Atomically publish validated public schedules and retain all older snapshots."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from gsme_rag.observability import emit
from gsme_rag.parsing.schedules import SCHEDULE_COLUMNS
from .muse import MuseWebsite, SOURCES, ScheduleSourceUnavailable, write_evidence

NAMESPACE = UUID("38c8086a-c8cc-459d-873f-d6d6ff4aa155")


def enabled():
    return os.getenv("GSME_MUSE_SYNC_ENABLED", "no") == "yes"


class MuseScheduleSync:
    def __init__(self, store, tenant, data_dir=None, website=None):
        self.store, self.tenant = store, tenant
        self.root = Path(data_dir or os.getenv("GSME_DATA_DIR", "./data")).resolve()
        self.website = website or MuseWebsite()

    def status(self):
        with self.store.connect() as db:
            return db.execute("""SELECT s.schedule_kind,s.source_url,s.document_id,s.checked_at,
                s.last_attempt_at,s.last_error,s.trusted_rows,s.quarantined_rows,d.current_version_id AS version_id
                FROM muse_schedule_sources s JOIN documents d ON d.id=s.document_id
                WHERE s.tenant_id=%s ORDER BY s.schedule_kind""", (self.tenant,)).fetchall()

    def _init(self, db):
        for kind, url in SOURCES.items():
            document_id = uuid5(NAMESPACE, self.tenant + "|" + kind)
            db.execute("INSERT INTO documents(id,tenant_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (document_id,self.tenant))
            db.execute("""INSERT INTO muse_schedule_sources(tenant_id,schedule_kind,document_id,source_url)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (self.tenant,kind,document_id,url))

    def ensure_fresh(self, kind=None):
        wanted = {kind} if kind in SOURCES else set(SOURCES)
        age = int(os.getenv("GSME_MUSE_MAX_AGE_SECONDS", "900"))
        if not 0 <= age <= 86400:
            raise ValueError("Muse freshness age must be 0..86400 seconds")
        now = datetime.now(timezone.utc)
        status = [r for r in self.status() if r["schedule_kind"] in wanted]
        if (len(status) == len(wanted) and all(r["version_id"] and r["checked_at"]
                and not r["last_error"] and (now-r["checked_at"]).total_seconds() <= age for r in status)):
            return status
        if any(r["last_error"] and r["last_attempt_at"] and now-r["last_attempt_at"] < timedelta(seconds=60) for r in status):
            raise ScheduleSourceUnavailable("Muse schedule refresh failed recently; retry after one minute. No older dates were returned.")
        self.refresh()
        return [r for r in self.status() if r["schedule_kind"] in wanted]

    def run_due(self):
        status = self.status()
        now = datetime.now(timezone.utc)
        attempts = [r["last_attempt_at"] for r in status if r["last_attempt_at"]]
        # Daily polling also works after process restarts. Failed attempts back
        # off for 15 minutes; lookup refresh has its own one-minute cooldown.
        retry_after = 900 if any(r["last_error"] for r in status) else 86400
        if len(status) < 2 or not attempts or now-max(attempts) >= timedelta(seconds=retry_after):
            return self.refresh()

    def refresh(self, *, accept_large_change=False):
        now = datetime.now(timezone.utc)
        try:
            with self.store.connect() as db:
                locked = db.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked",
                                    ("muse-schedule:"+self.tenant,)).fetchone()["locked"]
                if not locked:
                    raise ScheduleSourceUnavailable("Muse schedules are refreshing; retry shortly")
                self._init(db)
                snapshots = [self.website.fetch(kind) for kind in ("full","shared")]
                summaries = [self._publish(db, snapshot, now, accept_large_change) for snapshot in snapshots]
            emit("schedule.sync.completed", tenant_hash=sha256(self.tenant.encode()).hexdigest()[:12],
                 rows=sum(s["rows"] for s in summaries), changed=sum(s["changed"] for s in summaries))
            return summaries
        except Exception as error:
            # Preserve the last good snapshots but never label them current
            # after a failed mandatory refresh. No provider tokens enter logs.
            if str(error) != "Muse schedules are refreshing; retry shortly":
                with self.store.connect() as db:
                    self._init(db)
                    db.execute("UPDATE muse_schedule_sources SET last_attempt_at=%s,last_error=%s WHERE tenant_id=%s AND (last_attempt_at IS NULL OR last_attempt_at<=%s)",
                               (now,type(error).__name__+": "+str(error)[:240],self.tenant,now))
            emit("schedule.sync.failed", error_type=type(error).__name__)
            if isinstance(error, ScheduleSourceUnavailable):
                raise
            raise ScheduleSourceUnavailable("Muse schedule publication failed; operator inspection required") from None

    def _publish(self, db, snapshot, checked_at, accept_large_change):
        kind = snapshot.kind
        source = db.execute("SELECT * FROM muse_schedule_sources WHERE tenant_id=%s AND schedule_kind=%s FOR UPDATE",
                            (self.tenant,kind)).fetchone()
        document_id = source["document_id"]
        previous = db.execute("""SELECT v.* FROM documents d JOIN document_versions v ON v.id=d.current_version_id
            WHERE d.id=%s AND d.tenant_id=%s FOR UPDATE OF d""", (document_id,self.tenant)).fetchone()
        old_rows = db.execute("SELECT * FROM schedule_rows WHERE version_id=%s", (previous["id"],)).fetchall() if previous else []
        old_set = {tuple(str(row[k]) for k in SCHEDULE_COLUMNS) for row in old_rows}
        new_set = {tuple(str(row[k]) for k in SCHEDULE_COLUMNS) for row in snapshot.rows}
        if old_set and len(new_set) < len(old_set)*0.75 and not accept_large_change:
            raise ScheduleSourceUnavailable("Muse row count dropped over 25%; inspect source before publishing")
        digest = sha256(snapshot.csv_bytes).hexdigest()
        changed = not previous or previous["source_sha256"] != digest
        check_id = uuid4()
        base = self.root / "muse-schedules" / sha256(self.tenant.encode()).hexdigest()[:16] / kind
        directory = base / "versions" / str(check_id)
        raw, raw_hash = write_evidence(base / "raw", snapshot)
        if changed:
            directory.mkdir(parents=True,exist_ok=True)
            version_id = uuid4()
            original = directory / ("muse_"+kind+"_schedules.csv")
            original.write_bytes(snapshot.csv_bytes)
            def cell(value):
                return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
            markdown = "| " + " | ".join(SCHEDULE_COLUMNS) + " |\n| " + " | ".join("---" for _ in SCHEDULE_COLUMNS) + " |\n"
            markdown += "".join("| "+" | ".join(cell(r[k]) for k in SCHEDULE_COLUMNS)+" |\n" for r in snapshot.rows)
            normalized_hash = sha256(markdown.encode()).hexdigest()
            artifact = directory / "normalized.md"
            artifact.write_bytes(markdown.encode())
            manifest = {"source_sha256":digest,"normalized_sha256":normalized_hash,
                "parser_backend":"muse_public_widget_v1", "source_url":snapshot.source_url,
                "raw_sha256":raw_hash,"warnings":[{"code":"website_row_quarantined","message":q["reason"],"location":"source row "+str(q["source_row_number"])} for q in snapshot.quarantined],
                "quarantined":snapshot.quarantined,
                "chunk_count":0,"schedule_row_count":len(snapshot.rows),"checked_at":checked_at.isoformat()}
            artifact.with_name("review_manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
            db.execute("""INSERT INTO document_versions(id,document_id,parser_version,chunker_version,normalized_sha256,
                artifact_path,state,source_sha256,source_file,original_path,document_class,schedule_kind,parse_options)
                VALUES(%s,%s,'muse_public_widget_v1','not_applicable',%s,%s,'approved',%s,%s,%s,'schedule',%s,%s)""",
                (version_id,document_id,normalized_hash,str(artifact),digest,
                 original.name,str(original),kind,json.dumps({"source":"muse_public_widget_v1","source_url":snapshot.source_url})))
            db.cursor().executemany("""INSERT INTO schedule_rows(version_id,tenant_id,document_id,source_row_number,
                schedule_kind,technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(version_id,self.tenant,document_id,r["source_row_number"],kind,*(r[k] for k in SCHEDULE_COLUMNS)) for r in snapshot.rows])
            db.execute("INSERT INTO ingestion_jobs(id,tenant_id,document_id,version_id,state) VALUES(%s,%s,%s,%s,'ready')",
                       (uuid4(),self.tenant,document_id,version_id))
            self.store._publish(db,self.tenant,document_id,version_id)
        else:
            version_id = previous["id"]
        # Promotion and public access pointer commit together for both sources.
        db.execute("""INSERT INTO document_access(document_id,tenant_id,visibility,public_version_id,deletion_protected)
            VALUES(%s,%s,'company_schedule',%s,true) ON CONFLICT(document_id) DO UPDATE
            SET visibility='company_schedule',public_version_id=excluded.public_version_id,deletion_protected=true,updated_at=now()""",
            (document_id,self.tenant,version_id))
        db.execute("UPDATE muse_schedule_sources SET checked_at=%s,last_attempt_at=%s,last_error=NULL,trusted_rows=%s,quarantined_rows=%s WHERE tenant_id=%s AND schedule_kind=%s",
                   (checked_at,checked_at,len(snapshot.rows),len(snapshot.quarantined),self.tenant,kind))
        db.execute("""INSERT INTO muse_schedule_checks(id,tenant_id,schedule_kind,version_id,checked_at,raw_path,
            raw_sha256,row_count,added,removed,changed) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (check_id,self.tenant,kind,version_id,checked_at,str(raw),raw_hash,len(snapshot.rows),len(new_set-old_set),len(old_set-new_set),changed))
        return {"kind":kind,"version_id":str(version_id),"rows":len(snapshot.rows),"changed":changed,
                "quarantined_rows":len(snapshot.quarantined),
                "added":len(new_set-old_set),"removed":len(old_set-new_set),"source_url":snapshot.source_url}


def main():
    from argparse import ArgumentParser
    from gsme_rag.storage.config import from_env
    from gsme_rag.storage.postgres import EvidenceStore
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--accept-large-change",action="store_true",help="Operator source-checked a >25%% row-count reduction")
    args = parser.parse_args()
    settings = from_env()
    store = EvidenceStore(settings.database_url)
    store.migrate()
    print(json.dumps(MuseScheduleSync(store,os.getenv("GSME_HOSTED_TENANT","company"),settings.data_dir)
                     .refresh(accept_large_change=args.accept_large_change),indent=2))


if __name__ == "__main__":
    main()
