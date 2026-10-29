"""PostgreSQL owns versions reviews jobs and schedule rows"""

from datetime import date
from pathlib import Path
import json
import csv
import re
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from gsme_rag.chunking.chunker import CHUNKER_VERSION
from gsme_rag.models import MarkdownChunk
from gsme_rag.storage.files import checksum, save_original
from gsme_rag.parsing.schedules import resolve_schedule_kind, is_schedule_header
from gsme_rag.version import PARSER_VERSION
from gsme_rag.parsing.profile import source_profile


POINT_NAMESPACE = UUID("09e8281a-b989-43ae-ac31-f6b6990f3a14")


def _page_warning_codes(parsed, chunk: MarkdownChunk) -> list[str]:
    """Attach only page-located warnings; the manifest retains every raw warning."""
    if chunk.source_page_start is None:
        return []
    last = chunk.source_page_end or chunk.source_page_start
    codes = {
        warning.code
        for warning in parsed.warnings
        if (match := re.match(r"page (\d+)(?:\b|;)", warning.location or ""))
        and chunk.source_page_start <= int(match.group(1)) <= last
    }
    return sorted(codes)


class EvidenceStore:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)

    def migrate(self) -> list[str]:
        applied = []
        # SQL travels with the package, so wheel installs can migrate too.
        directory = Path(__file__).with_name("migrations")
        migrations = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
        if not migrations:
            raise ValueError("No SQL migrations found in the project folder")
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(644081525)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in migrations:
                if db.execute(
                    "SELECT 1 FROM schema_migrations WHERE name=%s", (path.name,)
                ).fetchone():
                    continue
                db.execute(path.read_text(encoding="utf-8"))
                db.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        return applied

    @staticmethod
    def _same_source_version(db, document_id, digest: str, schedule_kind: str | None, profile: dict):
        # The digest belongs to a version and never becomes the document ID
        return db.execute(
            """SELECT id, state FROM document_versions
            WHERE document_id=%s AND source_sha256=%s AND parser_version=%s
              AND chunker_version=%s AND schedule_kind IS NOT DISTINCT FROM %s AND parse_options=%s::jsonb
            ORDER BY created_at DESC LIMIT 1""",
            (document_id, digest, PARSER_VERSION, CHUNKER_VERSION, schedule_kind, json.dumps(profile)),
        ).fetchone()

    def submit(
        self,
        tenant_id: str,
        source: Path,
        data_dir: Path,
        document_id: str | None = None,
        schedule_kind: str | None = None,
    ) -> dict:
        if not tenant_id.strip() or source.suffix.lower() not in {
            ".docx",
            ".pdf",
            ".csv",
            ".png",
            ".jpg",
            ".jpeg",
            ".tif",
            ".tiff",
            ".bmp",
        }:
            raise ValueError("Tenant and a supported file are required")
        if schedule_kind not in (None, "full", "shared"):
            raise ValueError("schedule_kind must be full or shared")
        if not source.is_file() or source.is_symlink():
            raise ValueError("Source must be a regular local file")
        is_schedule = False
        if source.suffix.lower() == ".csv":
            with source.open("r", encoding="utf-8-sig", newline="") as incoming:
                header = next((row for row in csv.reader(incoming, strict=True)
                               if any(value.strip() for value in row)), [])
            is_schedule = is_schedule_header(header)
        if is_schedule:
            schedule_kind = resolve_schedule_kind(source.name, schedule_kind)
            if not schedule_kind:
                raise ValueError("Schedule CSV needs Full or Shared selection")
        elif schedule_kind:
            raise ValueError("Full/Shared is only for schedule CSVs")
        logical_id = UUID(document_id) if document_id else uuid4()
        profile = source_profile(source)
        if document_id:
            # Check unchanged bytes before copying a large file again
            digest = checksum(source)
            with self.connect() as db:
                owned = db.execute(
                    "SELECT 1 FROM documents WHERE id=%s AND tenant_id=%s", (logical_id, tenant_id)
                ).fetchone()
                if not owned:
                    raise ValueError("Document does not belong to this tenant")
                previous = self._same_source_version(db, logical_id, digest, schedule_kind, profile)
                if previous and previous["state"] not in ("failed", "superseded"):
                    return {
                        "document_id": str(logical_id),
                        "version_id": str(previous["id"]),
                        "state": previous["state"],
                        "no_op": True,
                    }

        version_id = uuid4()
        saved, digest = save_original(source, data_dir, tenant_id, str(version_id))
        committed = False
        try:
            with self.connect() as db:
                if document_id:
                    existing = db.execute(
                        "SELECT id FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE",
                        (logical_id, tenant_id),
                    ).fetchone()
                    if not existing:
                        raise ValueError("Document does not belong to this tenant")
                else:
                    db.execute(
                        "INSERT INTO documents(id,tenant_id) VALUES (%s,%s)",
                        (logical_id, tenant_id),
                    )
                # Repeat the check under the document lock for concurrent submissions
                previous = self._same_source_version(db, logical_id, digest, schedule_kind, profile)
                if previous and previous["state"] not in ("failed", "superseded"):
                    return {
                        "document_id": str(logical_id),
                        "version_id": str(previous["id"]),
                        "state": previous["state"],
                        "no_op": True,
                    }
                db.execute(
                    """INSERT INTO document_versions
                    (id,document_id,parser_version,chunker_version,normalized_sha256,artifact_path,
                     state,source_sha256,source_file,original_path,document_class,schedule_kind,parse_options)
                    VALUES (%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s)""",
                    (
                        version_id,
                        logical_id,
                        PARSER_VERSION,
                        CHUNKER_VERSION,
                        "0" * 64,
                        "",
                        digest,
                        source.name,
                        str(saved),
                        "schedule" if is_schedule else "document",
                        schedule_kind,
                        json.dumps(profile),
                    ),
                )
                job_id = uuid4()
                db.execute(
                    """INSERT INTO ingestion_jobs(id,tenant_id,document_id,version_id,state)
                              VALUES (%s,%s,%s,%s,'queued')""",
                    (job_id, tenant_id, logical_id, version_id),
                )
            committed = True
            return {
                "document_id": str(logical_id),
                "version_id": str(version_id),
                "job_id": str(job_id),
                "state": "queued",
                "no_op": False,
            }
        finally:
            # A failed or duplicate submission has no version that owns this copy
            if not committed:
                saved.unlink(missing_ok=True)
                try:
                    saved.parent.rmdir()
                except OSError:
                    pass

    def job_status(self, tenant_id: str, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT id, document_id, version_id, state, attempts, last_error
                   FROM ingestion_jobs WHERE tenant_id=%s AND id=%s""",
                (tenant_id, job_id),
            ).fetchone()
            return (
                {k: str(v) if isinstance(v, UUID) else v for k, v in row.items()} if row else None
            )

    def inspect_version(self, tenant_id: str, version_id: str, limit: int = 50,
                        offset: int = 0) -> dict | None:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Inspection limit must be between 1 and 100; offset must be nonnegative")
        with self.connect() as db:
            version = db.execute(
                """SELECT v.id,v.document_id,v.source_file,v.source_sha256,v.state,
                v.artifact_path,v.original_path,v.document_class,v.schedule_kind,
                v.parser_version,v.chunker_version,v.normalized_sha256,v.parse_options,
                (d.current_version_id=v.id) AS is_current
                FROM document_versions v JOIN documents d ON d.id=v.document_id
                WHERE d.tenant_id=%s AND v.id=%s""",
                (tenant_id, version_id),
            ).fetchone()
            if not version:
                return None
            chunks = db.execute(
                """SELECT c.source_chunk_id,c.evidence_text,c.source_page_start,
                c.source_page_end,c.source_anchor,c.warning_codes,c.parser_backend,
                c.category,c.chunk_type,c.review_required,c.evidence_status,c.heading_path,
                c.chunk_index,c.unit_id,c.part_index,c.part_count,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('model_version',j.model_version,
                  'collection',j.collection_name,'state',j.state,'last_error',j.last_error,
                  'retryable',j.retryable,'attempts',j.attempt_count,'next_attempt_at',j.next_attempt_at))
                  FROM index_jobs j WHERE j.chunk_id=c.id),'[]'::jsonb) AS index_jobs,
                r.decision AS latest_decision,r.source_verified,r.reviewer
                FROM evidence_chunks c LEFT JOIN LATERAL (
                    SELECT decision,source_verified,reviewer FROM review_decisions
                    WHERE chunk_id=c.id ORDER BY decision_order DESC LIMIT 1) r ON true
                WHERE c.version_id=%s ORDER BY c.chunk_index NULLS LAST,c.created_at,c.id LIMIT %s OFFSET %s""",
                (version_id, limit, offset),
            ).fetchall()
            total = db.execute("SELECT count(*) AS count FROM evidence_chunks WHERE version_id=%s",
                               (version_id,)).fetchone()["count"]
            return {"version": version, "chunks": chunks, "total_chunks": total,
                    "offset": offset, "next_offset": offset + limit if offset + limit < total else None,
                    "review_manifest_path": str(Path(version["artifact_path"]).with_name(
                        "review_manifest.json")) if version["artifact_path"] else None}

    def list_versions(self, tenant_id: str, limit: int = 50) -> list[dict]:
        if not tenant_id.strip() or not 1 <= limit <= 100:
            raise ValueError("Tenant and bounded limit are required")
        with self.connect() as db:
            return db.execute(
                """SELECT v.id, v.document_id, v.source_file, v.source_sha256,
                   v.state, v.created_at, v.document_class,j.id AS job_id,
                   (d.current_version_id = v.id) AS is_current,
                   (SELECT count(*) FROM evidence_chunks c WHERE c.version_id=v.id) AS chunks,
                   (SELECT count(*) FROM evidence_chunks c WHERE c.version_id=v.id
                    AND c.evidence_status='approved') AS approved_chunks
                   FROM document_versions v JOIN documents d ON d.id=v.document_id
                   LEFT JOIN ingestion_jobs j ON j.version_id=v.id
                   WHERE d.tenant_id=%s ORDER BY v.created_at DESC, v.id DESC LIMIT %s""",
                (tenant_id, limit),
            ).fetchall()

    def index_status(self, tenant_id: str, version_id: str) -> list[dict]:
        with self.connect() as db:
            return db.execute(
                """SELECT j.model_version,j.collection_name,j.state,count(*) AS chunks,
                   max(j.updated_at) AS updated_at
                   FROM index_jobs j JOIN evidence_chunks c ON c.id=j.chunk_id
                   JOIN document_versions v ON v.id=c.version_id
                   JOIN documents d ON d.id=v.document_id
                   WHERE d.tenant_id=%s AND v.id=%s
                   GROUP BY j.model_version,j.collection_name,j.state
                   ORDER BY j.model_version,j.state""",
                (tenant_id, version_id),
            ).fetchall()

    def approved_point(
        self, tenant_id: str, version_id: str, source_chunk_id: str,
        model_version: str, collection_name: str,
    ) -> dict | None:
        """Only currently published, approved and indexed evidence may expose a vector."""
        with self.connect() as db:
            return db.execute(
                """SELECT j.point_id,c.source_chunk_id,c.source_page_start,c.source_page_end,
                   a.source_file,a.source_sha256
                   FROM approved_current_evidence a
                   JOIN evidence_chunks c ON c.id=a.id
                   JOIN index_jobs j ON j.chunk_id=c.id
                   WHERE a.tenant_id=%s AND a.version_id=%s AND c.source_chunk_id=%s
                     AND j.model_version=%s AND j.collection_name=%s AND j.state='indexed'""",
                (tenant_id, version_id, source_chunk_id, model_version, collection_name),
            ).fetchone()

    def retry_job(self, tenant_id: str, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                """SELECT id,version_id FROM ingestion_jobs WHERE tenant_id=%s
                 AND id=%s AND state='failed' FOR UPDATE""",
                (tenant_id, job_id),
            ).fetchone()
            if not row:
                return False
            if db.execute(
                """SELECT 1 FROM review_decisions r JOIN evidence_chunks c ON c.id=r.chunk_id
                WHERE c.version_id=%s LIMIT 1""",
                (row["version_id"],),
            ).fetchone():
                raise ValueError("Cannot retry a reviewed version")
            db.execute(
                "UPDATE ingestion_jobs SET state='queued',last_error=NULL,updated_at=now() WHERE id=%s",
                (row["id"],),
            )
            db.execute(
                "UPDATE document_versions SET state='queued' WHERE id=%s", (row["version_id"],)
            )
            return True

    def claim_ingestion(self, lease_seconds: int, job_id: str | None = None) -> dict | None:
        # Expired leases can be claimed again after a worker stops
        with self.connect() as db:
            row = db.execute(
                """SELECT j.id,j.attempts,j.tenant_id,j.document_id,j.version_id,v.original_path,
                     v.source_file,v.schedule_kind,v.source_sha256,v.document_class,v.parse_options,
                     v.parser_version,v.chunker_version
                FROM ingestion_jobs j JOIN document_versions v ON v.id=j.version_id
                WHERE (j.id=%s::uuid OR %s::uuid IS NULL)
                  AND (j.state='queued' OR (j.state='running' AND
                    j.claimed_at < now() - (%s * interval '1 second')))
                ORDER BY j.updated_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED""",
                (job_id, job_id, lease_seconds),
            ).fetchone()
            if row:
                db.execute(
                    """UPDATE ingestion_jobs SET state='running',attempts=attempts+1,
                    claimed_at=now(),updated_at=now(),last_error=NULL WHERE id=%s""",
                    (row["id"],),
                )
                db.execute(
                    "UPDATE document_versions SET state='parsing' WHERE id=%s", (row["version_id"],)
                )
                row["attempts"] += 1
            return row

    def heartbeat_ingestion(self, job_id, attempts: int) -> bool:
        with self.connect() as db:
            return bool(db.execute(
                """UPDATE ingestion_jobs SET claimed_at=now() WHERE id=%s AND attempts=%s
                   AND state='running' RETURNING id""", (job_id, attempts)
            ).fetchone())

    def complete_ingestion(
        self,
        job: dict,
        parsed,
        chunks: list[MarkdownChunk],
        schedule_rows: list[dict],
        artifact_path: str,
        duration_seconds: float = 0.0,
    ) -> None:
        version_id = job["version_id"]
        with self.connect() as db:
            lease = db.execute(
                """SELECT 1 FROM ingestion_jobs WHERE id=%s AND attempts=%s
                   AND state='running' FOR UPDATE""", (job["id"], job["attempts"])
            ).fetchone()
            if not lease:
                raise ValueError("Ingestion lease changed during processing")
            # A retry can replace partial rows only before any human review
            if db.execute(
                "SELECT 1 FROM review_decisions r JOIN evidence_chunks c ON c.id=r.chunk_id WHERE c.version_id=%s LIMIT 1",
                (version_id,),
            ).fetchone():
                raise ValueError("Cannot reparse a version with recorded review decisions")
            db.execute(
                "DELETE FROM index_jobs WHERE chunk_id IN (SELECT id FROM evidence_chunks WHERE version_id=%s)",
                (version_id,),
            )
            db.execute("DELETE FROM evidence_chunks WHERE version_id=%s", (version_id,))
            db.execute("DELETE FROM schedule_rows WHERE version_id=%s", (version_id,))
            if parsed.metadata.content_sha256 != job["source_sha256"]:
                raise ValueError("Stored original changed since submission")
            db.execute(
                """UPDATE document_versions SET normalized_sha256=%s,artifact_path=%s,
                         schedule_kind=%s,state=%s WHERE id=%s""",
                (
                    parsed.metadata.normalized_sha256,
                    artifact_path,
                    parsed.metadata.schedule_kind,
                    "approved" if schedule_rows else "review_needed",
                    version_id,
                ),
            )
            if job["document_class"] == "schedule":
                if parsed.metadata.schedule_kind not in ("full", "shared"):
                    raise ValueError("Schedule kind requires explicit Full/Shared classification")
                # Send schedule rows in one database batch
                db.cursor().executemany(
                    """INSERT INTO schedule_rows(version_id,tenant_id,document_id,
                        source_row_number,schedule_kind,technology_nm,flavor,trial_deadline,final_deadline,
                        tapeout_date,estimated_ship_date) VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        (
                            version_id,
                            job["tenant_id"],
                            job["document_id"],
                            row["source_row_number"],
                            parsed.metadata.schedule_kind,
                            row["technology_nm"],
                            row["flavor"],
                            row["trial_deadline"],
                            row["final_deadline"],
                            row["tapeout_date"],
                            row["estimated_ship_date"],
                        )
                        for row in schedule_rows
                    ),
                )
                self._publish(db, job["tenant_id"], job["document_id"], version_id)
                status = "ready"
            else:
                # The database ID includes the version so two documents can share source bytes
                db.cursor().executemany(
                    """INSERT INTO evidence_chunks(id,version_id,source_chunk_id,
                        source_page_start,source_page_end,heading_path,warning_codes,chunk_type,
                        evidence_status,review_required,evidence_text,source_anchor,parser_backend,
                        parser_version,chunker_version,category,faq_question,table_row_number,
                        chunk_index,unit_id,part_index,part_count)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        (
                            str(uuid5(POINT_NAMESPACE, f"{version_id}|{chunk.chunk_id}")),
                            version_id,
                            chunk.chunk_id,
                            chunk.source_page_start,
                            chunk.source_page_end,
                            json.dumps(chunk.heading_path),
                            json.dumps(_page_warning_codes(parsed, chunk)),
                            chunk.chunk_type,
                            chunk.evidence_status,
                            chunk.review_required,
                            chunk.text,
                            chunk.unit_id or chunk.chunk_id,
                            parsed.strategy_used,
                            parsed.metadata.parser_version,
                            chunk.chunker_version,
                            chunk.category,
                            chunk.faq_question,
                            chunk.table_row_number,
                            chunk.index,
                            chunk.unit_id,
                            chunk.part_index,
                            chunk.part_count,
                        )
                        for chunk in chunks
                    ),
                )
                status = "review_needed"
            db.execute(
                """UPDATE ingestion_jobs SET state=%s,updated_at=now(),claimed_at=NULL,
                          duration_seconds=%s WHERE id=%s""",
                (status, duration_seconds, job["id"]),
            )

    def record_embedding_batch(
        self, model_version: str, chunk_count: int, duration_seconds: float
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO worker_metrics(id,kind,model_version,embedding_calls,
                chunk_count,duration_seconds) VALUES (%s,'embedding_batch',%s,1,%s,%s)""",
                (uuid4(), model_version, chunk_count, duration_seconds),
            )

    def metrics(self) -> dict:
        with self.connect() as db:
            return db.execute("""SELECT
                (SELECT count(*) FROM ingestion_jobs WHERE state IN ('ready','review_needed')) AS ingested_jobs,
                (SELECT coalesce(sum(duration_seconds),0) FROM ingestion_jobs) AS ingestion_seconds,
                (SELECT coalesce(sum(embedding_calls),0) FROM worker_metrics) AS embedding_calls,
                (SELECT coalesce(sum(chunk_count),0) FROM worker_metrics) AS embedded_chunks,
                (SELECT pg_total_relation_size('documents') + pg_total_relation_size('document_versions')
                      + pg_total_relation_size('evidence_chunks') + pg_total_relation_size('schedule_rows')
                      + pg_total_relation_size('review_decisions') + pg_total_relation_size('index_jobs'))
                  AS postgres_bytes""").fetchone()

    @staticmethod
    def _publish(db, tenant_id: str, document_id, version_id):
        # The current pointer changes in the same transaction as supersession
        doc = db.execute(
            "SELECT current_version_id FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE",
            (document_id, tenant_id),
        ).fetchone()
        if not doc:
            raise ValueError("Document does not belong to this tenant")
        old = doc["current_version_id"]
        if old != version_id:
            db.execute(
                "UPDATE documents SET current_version_id=%s WHERE id=%s AND tenant_id=%s",
                (version_id, document_id, tenant_id),
            )
            if old:
                db.execute("UPDATE document_versions SET state='superseded' WHERE id=%s", (old,))
                db.execute(
                    """UPDATE index_jobs SET state='superseded' WHERE chunk_id IN
                    (SELECT id FROM evidence_chunks WHERE version_id=%s)""",
                    (old,),
                )

    def record_review(
        self,
        tenant_id: str,
        version_id: str,
        source_chunk_id: str,
        decision: str,
        reviewer: str,
        rationale: str,
        source_verified: bool,
    ) -> None:
        if (
            decision not in ("approved", "rejected")
            or not reviewer.strip()
            or not rationale.strip()
        ):
            raise ValueError("Decision, reviewer, and rationale are required")
        if decision == "approved" and not source_verified:
            raise ValueError("Source verification is required for indexing")
        # Lock the chunk so indexing cannot race a changed review decision
        with self.connect() as db:
            # All publication/review operations lock the document before its chunks.
            # Matching the worker's lock order avoids deadlocks during revocation.
            db.execute("""SELECT d.id FROM documents d JOIN document_versions v ON v.document_id=d.id
                       WHERE d.tenant_id=%s AND v.id=%s FOR UPDATE OF d""",
                       (tenant_id, version_id)).fetchone()
            row = db.execute(
                """SELECT c.id,c.evidence_text,c.evidence_status,d.current_version_id
                FROM evidence_chunks c JOIN document_versions v ON v.id=c.version_id
                JOIN documents d ON d.id=v.document_id WHERE d.tenant_id=%s AND v.id=%s
                AND c.source_chunk_id=%s FOR UPDATE OF c""",
                (tenant_id, version_id, source_chunk_id),
            ).fetchone()
            if not row or row["evidence_status"] == "excluded":
                raise ValueError("Chunk unavailable for this tenant or excluded")
            if (
                decision == "approved"
                and "Visual evidence on page" in row["evidence_text"]
                and "no verified description" in row["evidence_text"]
            ):
                raise ValueError("Visual-gap placeholders cannot be approved")
            db.execute(
                """INSERT INTO review_decisions(id,chunk_id,decision,source_verified,reviewer,rationale)
                          VALUES (%s,%s,%s,%s,%s,%s)""",
                (uuid4(), row["id"], decision, source_verified, reviewer, rationale),
            )
            db.execute(
                "UPDATE evidence_chunks SET evidence_status=%s WHERE id=%s", (decision, row["id"])
            )
            if row["current_version_id"] == UUID(version_id):
                if decision == "approved":
                    db.execute(
                        "UPDATE index_jobs SET state='queued',retryable=true,attempt_count=0,next_attempt_at=now() WHERE chunk_id=%s AND state='superseded'",
                        (row["id"],),
                    )
                else:
                    db.execute(
                        "UPDATE index_jobs SET state='superseded' WHERE chunk_id=%s", (row["id"],)
                    )

    def publish(
        self,
        tenant_id: str,
        document_id: str,
        version_id: str,
        model_version: str,
        collection_name: str = "gsme_evidence_v1",
    ) -> int:
        with self.connect() as db:
            version = db.execute(
                """SELECT v.id FROM document_versions v JOIN documents d ON d.id=v.document_id
                WHERE d.tenant_id=%s AND d.id=%s AND v.id=%s AND v.document_class='document'
                  AND v.state IN ('review_needed','approved','indexing','indexed') FOR UPDATE OF d""",
                (tenant_id, document_id, version_id),
            ).fetchone()
            if not version:
                raise ValueError("Version unavailable for this tenant")
            approved = db.execute(
                """SELECT c.id FROM evidence_chunks c JOIN LATERAL
                (SELECT decision,source_verified FROM review_decisions WHERE chunk_id=c.id
                 ORDER BY decision_order DESC LIMIT 1) r ON true
                WHERE c.version_id=%s AND c.evidence_status='approved'
                  AND r.decision='approved' AND r.source_verified
                  AND NOT (position('Visual evidence on page' in c.evidence_text)>0
                      AND position('no verified description' in c.evidence_text)>0)""",
                (version_id,),
            ).fetchall()
            if not approved:
                raise ValueError("At least one source-verified approved chunk is required")
            # Publishing is separate from review and moves one logical pointer
            self._publish(db, tenant_id, UUID(document_id), UUID(version_id))
            db.execute("UPDATE document_versions SET state='approved' WHERE id=%s", (version_id,))
            for item in approved:
                prior = db.execute(
                    """SELECT collection_name FROM index_jobs
                    WHERE chunk_id=%s AND model_version=%s""",
                    (item["id"], model_version),
                ).fetchone()
                if prior and prior["collection_name"] != collection_name:
                    raise ValueError("Collection migration requires a new model/index version")
                # A repeat publish gets the same point ID and index job
                point_id = uuid5(
                    POINT_NAMESPACE, f"{tenant_id}|{version_id}|{item['id']}|{model_version}"
                )
                db.execute(
                    """INSERT INTO index_jobs(id,chunk_id,model_version,point_id,state,collection_name)
                    VALUES (%s,%s,%s,%s,'queued',%s) ON CONFLICT (chunk_id,model_version) DO UPDATE
                    SET state=CASE WHEN index_jobs.state IN ('indexed','indexing')
                              THEN index_jobs.state ELSE 'queued' END,
                        attempt_count=CASE WHEN index_jobs.state IN ('failed','superseded')
                                      THEN 0 ELSE index_jobs.attempt_count END,
                        retryable=true,last_error=NULL,next_attempt_at=now()""",
                    (uuid4(), item["id"], model_version, point_id, collection_name),
                )
            return len(approved)

    def claim_index_jobs(
        self,
        limit: int,
        lease_seconds: int,
        model_version: str,
        collection_name: str,
        tenant_id: str | None = None,
        version_id: str | None = None,
    ) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT j.id,j.attempt_count,j.chunk_id,j.point_id,j.model_version,c.source_chunk_id,
                   c.evidence_text,c.source_page_start,c.source_page_end,c.category,c.heading_path,
                   c.faq_question,c.table_row_number,c.chunk_type,c.review_required,
                   v.id AS version_id,v.source_sha256,v.source_file,
                   d.id AS document_id,d.tenant_id
                FROM index_jobs j JOIN approved_current_evidence c ON c.id=j.chunk_id
                JOIN document_versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
                WHERE j.model_version=%s AND j.collection_name=%s
                  AND (%s::text IS NULL OR d.tenant_id=%s)
                   AND (%s::uuid IS NULL OR v.id=%s::uuid)
                  AND j.state IN ('queued','failed','indexing')
                  AND j.retryable AND j.attempt_count < 5
                  AND (j.state!='indexing' OR j.claimed_at < now()-(%s*interval '1 second'))
                  AND j.next_attempt_at <= now() ORDER BY j.updated_at
                LIMIT %s FOR UPDATE OF j SKIP LOCKED""",
                (model_version, collection_name, tenant_id, tenant_id,
                 version_id, version_id, lease_seconds, limit),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE index_jobs SET state='indexing',claimed_at=now(),attempt_count=attempt_count+1,updated_at=now() WHERE id=%s",
                    (row["id"],),
                )
                row["attempt_count"] += 1
            return rows

    def heartbeat_index(self, jobs: list[dict]) -> bool:
        with self.connect() as db:
            for job in jobs:
                valid = db.execute(
                    """UPDATE index_jobs SET claimed_at=now() WHERE id=%s AND attempt_count=%s
                       AND state='indexing' RETURNING id""",
                    (job["id"], job["attempt_count"]),
                ).fetchone()
                if not valid:
                    return False
        return True

    def is_source_current_approved(
        self, tenant_id: str, version_id: str, source_chunk_id: str
    ) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    """SELECT 1 FROM approved_current_evidence WHERE tenant_id=%s
                 AND version_id=%s AND source_chunk_id=%s""",
                    (tenant_id, version_id, source_chunk_id),
                ).fetchone()
            )

    def index_done(self, job_id, attempt_count: int, *, error: str | None = None,
                   retryable: bool = True):
        with self.connect() as db:
            if error:
                db.execute(
                    """UPDATE index_jobs SET state='failed',last_error=%s,retryable=%s,claimed_at=NULL,
                  next_attempt_at=now()+(LEAST(3600, POWER(2,LEAST(attempt_count,10))) * interval '1 second'),
                  updated_at=now() WHERE id=%s AND attempt_count=%s AND state='indexing'""",
                    (error[:500], retryable, job_id, attempt_count),
                )
            else:
                db.execute(
                    """UPDATE index_jobs SET state='indexed',last_error=NULL,claimed_at=NULL,
                  updated_at=now() WHERE id=%s AND attempt_count=%s AND state='indexing'""",
                    (job_id, attempt_count),
                )

    def mark_indexed_versions(self, version_ids: list):
        # Run after job-completion transactions release their locks. Publication
        # locks the version before its jobs, so reversing that order can deadlock.
        with self.connect() as db:
            db.execute("""UPDATE document_versions v SET state='indexed'
                WHERE v.id=ANY(%s) AND v.state IN ('approved','indexing','indexed')
                  AND EXISTS (SELECT 1 FROM index_jobs j JOIN evidence_chunks c ON c.id=j.chunk_id
                              WHERE c.version_id=v.id AND j.state='indexed')
                  AND NOT EXISTS (SELECT 1 FROM index_jobs j JOIN evidence_chunks c ON c.id=j.chunk_id
                                  WHERE c.version_id=v.id AND j.state IN ('queued','indexing','failed'))""",
                (version_ids,))

    def allowed_hits(
        self, tenant_id: str, point_ids: list[str], model_version: str, collection_name: str
    ) -> dict[str, dict]:
        if not point_ids:
            return {}
        with self.connect() as db:
            rows = db.execute(
                """SELECT j.point_id,c.evidence_text,c.source_chunk_id,
                 c.source_page_start,c.source_page_end,c.category,c.table_row_number,
                 c.document_id,c.version_id,c.source_file,c.source_sha256,c.heading_path,
                 c.faq_question,c.unit_id,c.part_index,c.part_count,c.review_required,
                 c.parser_backend,c.parser_version,c.chunker_version
                 FROM approved_current_evidence c JOIN index_jobs j ON j.chunk_id=c.id
                 WHERE c.tenant_id=%s AND j.point_id=ANY(%s) AND j.model_version=%s
                   AND j.collection_name=%s AND j.state='indexed'""",
                (tenant_id, [UUID(i) for i in point_ids], model_version, collection_name),
            ).fetchall()
            return {str(row["point_id"]): row for row in rows}

    def has_indexed_evidence(self, tenant_id: str, model_version: str, collection_name: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("""SELECT 1 FROM approved_current_evidence c
                JOIN index_jobs j ON j.chunk_id=c.id WHERE c.tenant_id=%s
                AND j.model_version=%s AND j.collection_name=%s AND j.state='indexed' LIMIT 1""",
                (tenant_id, model_version, collection_name)).fetchone())

    def indexed_points(self, tenant_id: str, version_id: str, model_version: str,
                       collection_name: str, after=None) -> list[dict]:
        with self.connect() as db:
            return db.execute("""SELECT j.id,j.point_id FROM index_jobs j
                JOIN approved_current_evidence c ON c.id=j.chunk_id
                WHERE c.tenant_id=%s AND c.version_id=%s AND j.model_version=%s
                  AND j.collection_name=%s AND j.state='indexed'
                  AND (%s::uuid IS NULL OR j.id>%s::uuid) ORDER BY j.id LIMIT 100""",
                (tenant_id, version_id, model_version, collection_name, after, after)).fetchall()

    def requeue_missing_points(self, job_ids: list) -> int:
        if not job_ids:
            return 0
        with self.connect() as db:
            return db.execute("""UPDATE index_jobs SET state='queued',attempt_count=0,retryable=true,
                last_error='missing_derived_point',next_attempt_at=now(),updated_at=now()
                WHERE id=ANY(%s) AND state='indexed'
                  AND chunk_id IN (SELECT id FROM approved_current_evidence)""", (job_ids,)).rowcount

    def schedules(
        self,
        tenant_id: str,
        schedule_kind: str,
        *,
        technology_nm: int | None = None,
        flavor: str | None = None,
        tapeout_on_or_after: date | None = None,
        document_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if (
            not tenant_id.strip()
            or schedule_kind not in ("full", "shared")
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Tenant, Full/Shared kind and bounded limit are required")
        # Only add filters the caller uses so the exact indexes stay useful
        query = """SELECT r.document_id,r.version_id,r.source_row_number,r.schedule_kind,
                    r.technology_nm,r.flavor,r.trial_deadline,r.final_deadline,r.tapeout_date,
                    r.estimated_ship_date,v.source_file,v.source_sha256
                FROM schedule_rows r JOIN documents d ON d.id=r.document_id
                JOIN document_versions v ON v.id=r.version_id
                WHERE r.tenant_id=%s AND d.tenant_id=%s AND d.current_version_id=r.version_id
                  AND r.schedule_kind=%s"""
        values: list = [tenant_id, tenant_id, schedule_kind]
        if document_id:
            query += " AND r.document_id=%s::uuid"
            values.append(document_id)
        if technology_nm is not None:
            query += " AND r.technology_nm=%s"
            values.append(technology_nm)
        if flavor:
            query += " AND lower(r.flavor)=lower(%s)"
            values.append(flavor)
        if tapeout_on_or_after:
            query += " AND r.tapeout_date >= %s"
            values.append(tapeout_on_or_after)
        query += " ORDER BY r.tapeout_date,r.document_id,r.source_row_number LIMIT %s"
        values.append(limit)
        with self.connect() as db:
            return db.execute(query, values).fetchall()

    def estimated_vector_bytes(self, dimension: int) -> int:
        with self.connect() as db:
            row = db.execute("""SELECT count(*) AS n,coalesce(sum(octet_length(c.evidence_text)),0) AS text_bytes
                FROM index_jobs j JOIN evidence_chunks c ON c.id=j.chunk_id
                WHERE j.state='indexed'""").fetchone()
            return row["n"] * dimension * 4 + row["text_bytes"]
