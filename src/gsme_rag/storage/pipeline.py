"""Local worker and evidence search"""

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from threading import Event, Thread
import time

from gsme_rag.chunking import chunk_markdown
from gsme_rag.indexing import prepare_candidate, CandidateValidationError
from gsme_rag.models import MarkdownChunk
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.schedules import parse_schedule_records
from gsme_rag.parsing.profile import source_profile
from gsme_rag.review import ReviewDecision, ReviewLedger
from gsme_rag.storage.config import Settings
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import ProviderOperationError
from gsme_rag.version import PARSER_VERSION, CHUNKER_VERSION


def _index_error_label(error: Exception, stage: str) -> str:
    detail = f"; {error}" if isinstance(error, (ProviderOperationError, CandidateValidationError)) else ""
    return f"{type(error).__name__}: index {stage} failed{detail}"


class _LeaseHeartbeat:
    def __init__(self, refresh, lease_seconds: int):
        self.refresh = refresh
        self.interval = max(10, lease_seconds // 3)
        self.stopped = Event()
        self.lost = Event()
        self.thread = Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stopped.wait(self.interval):
            try:
                if not self.refresh():
                    self.lost.set()
                    return
            except Exception:
                # A failed heartbeat must never let an old worker publish
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set():
            raise ValueError("Worker lease changed during processing")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.thread.join()


@contextmanager
def _approval_lock(store: EvidenceStore, jobs: list[dict]):
    """Keep the review and current version fixed during one embedding batch"""
    with store.connect() as db:
        for document_id in sorted({row["document_id"] for row in jobs}):
            db.execute("SELECT id FROM documents WHERE id=%s FOR SHARE", (document_id,)).fetchone()
        for chunk_id in sorted({row["chunk_id"] for row in jobs}):
            db.execute(
                "SELECT id FROM evidence_chunks WHERE id=%s FOR SHARE", (chunk_id,)
            ).fetchone()
        for job in jobs:
            valid = db.execute(
                """SELECT 1 FROM approved_current_evidence WHERE id=%s AND version_id=%s
                        AND tenant_id=%s""",
                (job["chunk_id"], job["version_id"], job["tenant_id"]),
            ).fetchone()
            if not valid:
                raise ValueError("Review/version changed before embedding")
        yield


def process_ingestion(
    store: EvidenceStore, settings: Settings, job_id: str | None = None
) -> dict | None:
    job = store.claim_ingestion(settings.lease_seconds, job_id)
    if not job:
        return None
    started = time.perf_counter()
    stage = {"name": "start"}
    with _LeaseHeartbeat(
        lambda: store.heartbeat_ingestion(job["id"], job["attempts"]),
        settings.lease_seconds,
    ) as lease:
        try:
            return _run_ingestion(store, settings, job, started, lease, stage)
        except Exception as error:
            label = f"{type(error).__name__}: ingestion {stage['name']} failed"
            with store.connect() as db:
                # Only the worker holding this attempt can fail the version
                changed = db.execute(
                    """UPDATE ingestion_jobs SET state='failed',last_error=%s,claimed_at=NULL,
                              updated_at=now() WHERE id=%s AND attempts=%s AND state='running'
                              RETURNING id""",
                    (label, job["id"], job["attempts"]),
                ).fetchone()
                if changed:
                    db.execute(
                        "UPDATE document_versions SET state='failed' WHERE id=%s",
                        (job["version_id"],),
                    )
            return {"job_id": str(job["id"]),
                    "state": "failed" if changed else "stale_attempt", "error": label}


def _run_ingestion(store, settings, job, started, lease, stage):
    stage["name"] = "parser_version_check"
    if job["parser_version"] != PARSER_VERSION or job["chunker_version"] != CHUNKER_VERSION:
        raise ValueError("Queued job uses a different parser/chunker version; resubmit the source")
    stage["name"] = "source_check"
    original = Path(job["original_path"])
    if not original.is_file() or original.is_symlink():
        raise ValueError("Durable original is missing")
    stage["name"] = "parser_environment_check"
    if source_profile(original, job["parse_options"].get("pdf_backend")) != job["parse_options"]:
        raise ValueError("Parser dependencies changed after submission; resubmit the source")
    stage["name"] = "parse"
    parsed = parse_to_markdown(original, job["source_file"], job["schedule_kind"],
                               pdf_backend=job["parse_options"].get("pdf_backend"))
    if parsed.metadata.classification_status != "ready":
        raise ValueError("Schedule kind is ambiguous. Submit with Full or Shared")
    # Schedule dates stay as typed rows for exact filtering
    if job["document_class"] == "schedule":
        stage["name"] = "schedule_rows"
        if parsed.metadata.document_class != "schedule":
            raise ValueError("CSV did not match the typed schedule schema")
        rows = parse_schedule_records(original)
        if not rows:
            raise ValueError("Schedule has no typed rows")
        chunks = []
    else:
        stage["name"] = "chunk"
        if parsed.metadata.document_class != "document":
            raise ValueError("Unexpected structured data")
        chunks = chunk_markdown(
            parsed.markdown, parsed.filename, metadata=parsed.metadata
        ).chunks
        rows = []
        if not chunks:
            raise ValueError("Parser produced no reviewable chunks")
    stage["name"] = "artifact_write"
    # A reclaimed worker must not overwrite the new attempt's files.
    artifact = settings.data_dir / "artifacts" / str(job["version_id"]) / str(job["attempts"])
    artifact.mkdir(parents=True, exist_ok=True)
    target = artifact / "normalized.md"
    temporary = artifact / ".normalized.tmp"
    temporary.write_text(parsed.markdown, encoding="utf-8")
    temporary.replace(target)
    # Keep parser warnings beside the normalized source for human review
    manifest = artifact / "review_manifest.json"
    manifest_tmp = artifact / ".review_manifest.tmp"
    manifest_tmp.write_text(
        json.dumps(
            {
                "source_sha256": parsed.metadata.content_sha256,
                "normalized_sha256": parsed.metadata.normalized_sha256,
                "parser_backend": parsed.strategy_used,
                "warnings": [asdict(w) for w in parsed.warnings],
                "evidence_diagnostics": [asdict(item) for item in parsed.evidence_diagnostics],
                "chunk_count": len(chunks),
                "schedule_row_count": len(rows),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest_tmp.replace(manifest)
    lease.check()
    stage["name"] = "database_write"
    store.complete_ingestion(
        job, parsed, chunks, rows, str(target), time.perf_counter() - started
    )
    return {
        "job_id": str(job["id"]),
        "version_id": str(job["version_id"]),
        "state": "ready" if rows else "review_needed",
        "chunks": len(chunks),
        "schedule_rows": len(rows),
        "seconds": round(time.perf_counter() - started, 3),
    }


def process_index(
    store: EvidenceStore, vectors, provider, settings: Settings, tenant_id: str | None = None,
    version_id: str | None = None,
) -> dict:
    if getattr(provider, "external", False) and (not tenant_id or not version_id):
        raise ValueError("External embeddings require an explicit tenant and version ID")
    jobs = store.claim_index_jobs(
        min(settings.batch_size, settings.worker_batch),
        settings.lease_seconds,
        provider.model_version,
        vectors.collection,
        tenant_id,
        version_id,
    )
    if not jobs:
        return {"indexed": 0, "embedding_calls": 0, "failed": 0}
    prepared = []
    started = time.perf_counter()
    stage = {"name": "start"}
    with _LeaseHeartbeat(
        lambda: store.heartbeat_index(jobs), settings.lease_seconds
    ) as lease:
        return _run_index(store, vectors, provider, settings, jobs, prepared, started, lease, stage)


def _run_index(store, vectors, provider, settings, jobs, prepared, started, lease, stage):
    candidate_errors = {}
    embedding_calls = 0
    try:
        stage["name"] = "approval_check"
        with _approval_lock(store, jobs):
            stage["name"] = "collection_check"
            vectors.ensure_collection()
            stage["name"] = "point_lookup"
            existing = vectors.existing([str(job["point_id"]) for job in jobs])
            stage["name"] = "token_budget"
            for job in jobs:
                found = existing.get(str(job["point_id"]))
                # A completed upsert can outlive a worker crash
                if (
                    found
                    and found.get("model_version") == job["model_version"]
                    and found.get("text_sha256")
                    == sha256(job["evidence_text"].encode()).hexdigest()
                ):
                    continue
                chunk = MarkdownChunk(
                    chunk_id=job["source_chunk_id"],
                    index=0,
                    text=job["evidence_text"],
                    chunk_type=job["chunk_type"],
                    source_file=job["source_file"],
                    heading_path=job["heading_path"],
                    category=job["category"],
                    faq_question=job["faq_question"],
                    table_row_number=job["table_row_number"],
                    source_page_start=job["source_page_start"],
                    source_page_end=job["source_page_end"],
                    content_sha256=job["source_sha256"],
                    review_required=job["review_required"],
                    evidence_status="approved",
                )
                # The database view already checked the latest source review
                # This ledger only adapts that decision to prepare_candidate
                ledger = ReviewLedger(
                    [
                        ReviewDecision(
                            chunk.content_sha256,
                            chunk.chunk_id,
                            "approved",
                            "postgres-review",
                            "source-verified",
                            True,
                        )
                    ]
                )
                try:
                    candidate = prepare_candidate(
                        chunk, ledger, tenant_id=job["tenant_id"], model_version=job["model_version"],
                        encode_tokens=provider.encode_tokens, max_tokens=settings.max_tokens,
                    )
                except CandidateValidationError as error:
                    # A too-large or invalid candidate must not poison its healthy neighbors.
                    candidate_errors[job["id"]] = _index_error_label(error, "candidate_validation")
                    continue
                prepared.append((job, candidate))
            stage["name"] = "document_embedding"
            embeddings = []
            if prepared:
                embedding_calls = 1
                try:
                    embeddings = provider.embed_documents([candidate.text for _, candidate in prepared])
                finally:
                    store.record_embedding_batch(provider.model_version, len(prepared), time.perf_counter() - started)
            if len(embeddings) != len(prepared):
                raise ValueError("Embedding provider returned wrong number of vectors")
            lease.check()
            stage["name"] = "vector_validation"
            payloads = []
            for (job, candidate), vector in zip(prepared, embeddings, strict=True):
                if len(vector) != settings.dimension:
                    raise ValueError("Embedding dimension mismatch")
                payloads.append(
                    (
                        str(job["point_id"]),
                        vector,
                        {
                            "text": candidate.text,
                            "tenant_id": job["tenant_id"],
                            "version_id": str(job["version_id"]),
                            "model_version": job["model_version"],
                            "text_sha256": sha256(candidate.text.encode()).hexdigest(),
                        },
                    )
                )
            # Reusing point IDs makes an interrupted upsert safe to repeat
            lease.check()
            stage["name"] = "vector_upsert"
            vectors.upsert(payloads)
            lease.check()
        stage["name"] = "job_completion"
        for job in jobs:
            error = candidate_errors.get(job["id"])
            store.index_done(job["id"], job["attempt_count"], error=error, retryable=not bool(error))
        store.mark_indexed_versions(list({job["version_id"] for job in jobs}))
        return {"indexed": len(jobs) - len(candidate_errors), "embedding_calls": embedding_calls,
                "failed": len(candidate_errors), "candidate_errors": list(candidate_errors.values())}
    except Exception as error:
        # Keep source text and secrets out of stored errors
        label = _index_error_label(error, stage["name"])
        for job in jobs:
            store.index_done(job["id"], job["attempt_count"], error=label)
        return {"indexed": 0, "embedding_calls": embedding_calls, "failed": len(jobs), "error": label}


def reconcile(store: EvidenceStore, vectors, model_version: str, limit: int = 100) -> int:
    # Hold these locks until Qdrant deletes the stale points
    with store.connect() as db:
        stale = db.execute(
            """SELECT j.id,j.point_id,c.id AS chunk_id,v.document_id
            FROM index_jobs j JOIN evidence_chunks c ON c.id=j.chunk_id
            JOIN document_versions v ON v.id=c.version_id
            WHERE j.model_version=%s AND j.collection_name=%s
              AND j.state IN ('indexed','superseded','failed')
              AND j.last_error IS DISTINCT FROM 'removed_from_derived_index'
              AND NOT EXISTS (SELECT 1 FROM approved_current_evidence a WHERE a.id=c.id)
            ORDER BY j.updated_at LIMIT %s""",
            (model_version, vectors.collection, limit),
        ).fetchall()
        if not stale:
            return 0
        for document_id in sorted({r["document_id"] for r in stale}):
            db.execute("SELECT id FROM documents WHERE id=%s FOR SHARE", (document_id,))
        for chunk_id in sorted({r["chunk_id"] for r in stale}):
            db.execute("SELECT id FROM evidence_chunks WHERE id=%s FOR SHARE", (chunk_id,))
        still_stale = []
        for row in stale:
            locked = db.execute(
                """SELECT id FROM index_jobs WHERE id=%s
                AND state IN ('indexed','superseded','failed')
                AND last_error IS DISTINCT FROM 'removed_from_derived_index'
                FOR UPDATE SKIP LOCKED""",
                (row["id"],),
            ).fetchone()
            if (
                locked
                and not db.execute(
                    "SELECT 1 FROM approved_current_evidence WHERE id=%s", (row["chunk_id"],)
                ).fetchone()
            ):
                still_stale.append(row)
        # Search already rejects these points through the database view
        vectors.delete([str(row["point_id"]) for row in still_stale])
        if still_stale:
            db.execute(
                """UPDATE index_jobs SET state='superseded',
                last_error='removed_from_derived_index' WHERE id=ANY(%s)""",
                ([row["id"] for row in still_stale],),
            )
        return len(still_stale)


def repair_missing_vectors(store: EvidenceStore, vectors, model_version: str,
                           tenant_id: str, version_id: str) -> int:
    """Queue missing derived points; embedding still needs a separate worker action."""
    after = None
    repaired = 0
    collection_exists = vectors.client.collection_exists(vectors.collection)
    while rows := store.indexed_points(tenant_id, version_id, model_version, vectors.collection, after):
        found = vectors.existing([str(row["point_id"]) for row in rows]) if collection_exists else {}
        repaired += store.requeue_missing_points([row["id"] for row in rows if str(row["point_id"]) not in found])
        after = rows[-1]["id"]
    return repaired


def search_evidence(
    store: EvidenceStore,
    vectors,
    provider,
    settings: Settings,
    tenant_id: str,
    question: str,
    limit: int = 5,
    hybrid: bool = False,
) -> list[dict]:
    if not tenant_id.strip() or not question.strip() or not 1 <= limit <= 20:
        raise ValueError("Tenant, question, and bounded limit are required")
    if hybrid and not settings.hybrid_enabled:
        raise ValueError("Enable GSME_HYBRID only after dense evaluation")
    if not store.has_indexed_evidence(tenant_id, provider.model_version, vectors.collection):
        return []
    if len(provider.encode_tokens(question)) > settings.max_tokens:
        raise ValueError("Query exceeds embedding token budget")
    query_vector = provider.embed_query(question)
    if len(query_vector) != settings.dimension:
        raise ValueError("Query embedding dimension mismatch")
    # Fetch more candidates when stale points take up the first page
    window = max(20, limit * 5)
    results = []
    while True:
        hits = vectors.query(tenant_id, query_vector, window, text=question if hybrid else None,
                             model_version=provider.model_version)
        ids = [str(hit.id) for hit in hits]
        # Return text and citations from PostgreSQL instead of Qdrant payloads
        allowed = store.allowed_hits(tenant_id, ids, provider.model_version, vectors.collection)
        results = []
        for hit in hits:
            row = allowed.get(str(hit.id))
            if row:
                results.append(
                    {
                        "score": float(hit.score),
                        "text": row["evidence_text"],
                        "document_id": str(row["document_id"]),
                        "version_id": str(row["version_id"]),
                        "chunk_id": row["source_chunk_id"],
                        "source_file": row["source_file"],
                        "source_sha256": row["source_sha256"],
                        "category": row["category"],
                        "page_start": row["source_page_start"],
                        "page_end": row["source_page_end"],
                        "table_row_number": row["table_row_number"],
                        "heading_path": row["heading_path"],
                        "faq_question": row["faq_question"],
                        "unit_id": row["unit_id"], "part_index": row["part_index"],
                        "part_count": row["part_count"], "review_required": row["review_required"],
                        "evidence_status": "approved", "model_version": provider.model_version,
                        "parser_backend": row["parser_backend"],
                        "parser_version": row["parser_version"], "chunker_version": row["chunker_version"],
                    }
                )
            if len(results) >= limit:
                return results
        if len(hits) < window or window >= 1000:
            return results
        window = min(window * 2, 1000)
