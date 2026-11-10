"""Explicitly local, authenticated operator UI for reviewed evidence.

This app is intentionally separate from the public parsing demo. Never mount it
in gsme_rag.api.app or bind it to a non-loopback interface.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import time
from pathlib import Path
import tempfile
from typing import Literal, Annotated
from uuid import UUID, uuid4
from datetime import date
from collections import deque
from contextlib import contextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, AfterValidator, StringConstraints
import psycopg
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from gsme_rag.storage.config import Settings, from_env
from gsme_rag.storage.pipeline import process_ingestion, process_index, reconcile, search_evidence, repair_missing_vectors
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import CohereEmbeddings, FakeEmbeddings, ProviderOperationError, embedding_model_version
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.storage.files import checksum, verified_markdown
from gsme_rag.version import __version__
from gsme_rag.parsing.ocr import tesseract_environment
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.reranking import CohereReranker, FakeReranker, missing_rerank_settings
from gsme_rag.generation.answers import generate_answer, require_answer_permission
from gsme_rag.retrieval.planning import plan_query
from gsme_rag.retrieval.schedules import ScheduleQuery, search_schedules, ScheduleSnapshotChanged
from gsme_rag.observability import REQUEST_ID, emit, operation, configure_console_logging


MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".pptx", ".xlsx", ".pdf", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
Identifier = Annotated[str, AfterValidator(lambda value: str(UUID(value)))]
Tenant = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class JobRequest(BaseModel):
    tenant: Tenant
    job_id: Identifier


class ReviewRequest(BaseModel):
    tenant: Tenant
    version_id: Identifier
    chunk_id: str
    decision: Literal["approved", "rejected"]
    reviewer: str
    rationale: str
    source_verified: bool = False


class VersionRequest(BaseModel):
    tenant: Tenant
    document_id: Identifier | None = None
    version_id: Identifier
    provider: Literal["fake", "cohere"] = "cohere"
    confirm_external: bool = False


class BatchReviewRequest(BaseModel):
    tenant: Tenant
    version_id: Identifier
    chunk_ids: list[str] = Field(min_length=1, max_length=100)
    decision: Literal["approved", "rejected"]
    reviewer: str
    rationale: str
    source_verified: bool = False


class SearchRequest(BaseModel):
    tenant: Tenant
    question: str = Field(min_length=1, max_length=20000)
    provider: Literal["fake", "cohere"] = "cohere"
    limit: int = Field(default=5, ge=1, le=20)
    hybrid: bool = False


class RetrievalRequest(BaseModel):
    tenant: Tenant
    question: str = Field(default="", max_length=20000)
    provider: Literal["fake", "cohere"] = "cohere"
    confirm_external: bool = False
    generate_answer: bool = False
    confirm_answer_external: bool = False
    route: Literal["documents", "schedules", "mixed", "auto"] = "documents"
    query_mode: Literal["original", "expanded", "multi"] = "original"
    strategy: Literal["dense", "hybrid"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_count: int = Field(default=30, ge=1, le=100)
    context_chars: int = Field(default=16000, ge=1800, le=64000)
    min_rerank_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    document_id: Identifier | None = None
    category: str | None = Field(default=None, min_length=1, max_length=300)
    source_file: str | None = Field(default=None, min_length=1, max_length=300)
    schedule_kind: Literal["full", "shared", "both"] | None = None
    schedule_limit: int = Field(default=100, ge=1, le=999)
    technology_nm: int | None = Field(default=None, ge=1, le=2147483647)
    flavor: str | None = Field(default=None, min_length=1, max_length=300)
    tapeout_on_or_after: date | None = None
    date_field: Literal["trial_deadline", "final_deadline", "tapeout_date", "estimated_ship_date"] = "tapeout_date"
    date_from: date | None = None
    date_to: date | None = None
    next_dates: int | None = Field(default=None, ge=1, le=100)
    deadline_status: Literal["all", "open", "closed"] = "all"
    timezone: str = "Asia/Karachi"
    as_of: date | None = None
    schedule_offset: int = Field(default=0, ge=0, le=1000000)
    schedule_snapshot: str | None = Field(default=None, min_length=64, max_length=64)


class ScheduleSearchRequest(ScheduleQuery):
    tenant: Tenant
    document_id: Identifier | None = None


def _provider(name: str, settings: Settings):
    return FakeEmbeddings(settings.dimension) if name == "fake" else CohereEmbeddings(settings.dimension)


def _vectors(name: str, settings: Settings):
    return QdrantEvidence(
        settings.qdrant_url,
        settings.collection + ("_synthetic" if name == "fake" else ""),
        settings.dimension,
        settings.hybrid_enabled,
    )


@contextmanager
def _managed_vectors(name, settings):
    """Release per-request connections even on an early 404 or provider error."""
    vectors = _vectors(name, settings)
    try:
        yield vectors
    finally:
        vectors.client.close()


def _safe(action):
    try:
        return action()
    except ScheduleSnapshotChanged as exc:
        raise HTTPException(409, str(exc)) from None
    except ProviderOperationError as exc:
        raise HTTPException(502, str(exc)) from None
    except (ValueError, FileNotFoundError) as exc:
        # Do not return provider, database, or file-system exception values.
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "Source unavailable") from None


def create_admin_app(settings: Settings | None = None, store: EvidenceStore | None = None) -> FastAPI:
    settings = settings or from_env()
    store = store or EvidenceStore(settings.database_url)
    secret = os.getenv("GSME_REVIEW_AUTH_TOKEN", "")
    if len(secret) < 20:
        raise ValueError("Set GSME_REVIEW_AUTH_TOKEN to a random secret of at least 20 characters")

    app = FastAPI(title="GSME Local Evidence Console", version=__version__)
    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(request, error):
        emit("storage.postgres.failed", error_type=type(error).__name__)
        return JSONResponse({"detail": "PostgreSQL unavailable; check Docker services and local database credentials"}, status_code=503)

    async def migration_required(request, error):
        emit("storage.schema.failed", error_type=type(error).__name__)
        return JSONResponse({"detail": "Storage schema needs migration; run python -m gsme_rag.storage.cli migrate"}, status_code=503)

    app.add_exception_handler(psycopg.errors.UndefinedTable, migration_required)
    app.add_exception_handler(psycopg.errors.UndefinedColumn, migration_required)

    async def vectors_unavailable(request, error):
        emit("storage.qdrant.failed", error_type=type(error).__name__)
        return JSONResponse({"detail": "Qdrant request failed; check its service and index configuration"}, status_code=503)

    app.add_exception_handler(ResponseHandlingException, vectors_unavailable)
    app.add_exception_handler(UnexpectedResponse, vectors_unavailable)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (AttributeError, ValueError):
            local = False
        if not local:
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        return await call_next(request)

    def authorize(request: Request, x_gsme_token: str | None = Header(default=None)):
        # Header authentication plus origin check prevents cross-site form requests.
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin request denied")
        if not x_gsme_token or not hmac.compare_digest(x_gsme_token.encode(), secret.encode()):
            raise HTTPException(401, "Local operator token required")

    @app.middleware("http")
    async def request_trace(request: Request, call_next):
        token = REQUEST_ID.set(str(uuid4()))
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-GSME-Request-ID"] = REQUEST_ID.get()
            if request.url.path.startswith("/api/") and (request.method == "POST" or response.status_code >= 400):
                emit("http.completed", method=request.method, route=getattr(request.scope.get("route"), "path", "unmatched"),
                     status=response.status_code, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            return response
        finally:
            REQUEST_ID.reset(token)

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(web / "search.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/admin", include_in_schema=False)
    def operator_console():
        return FileResponse(web / "index.html", media_type="text/html")

    @app.get("/api/health", dependencies=[Depends(authorize)])
    def health():
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM schema_migrations WHERE name='004_automatic_clean_text.sql'").fetchone():
                raise HTTPException(503, "Run python -m gsme_rag.storage.cli migrate before using the console")
        vectors = _vectors("fake", settings)
        try:
            vectors.client.get_collections()
        finally:
            vectors.client.close()
        return {"status": "ok", "version": __version__, "postgres": "reachable", "qdrant": "reachable", "dimension": settings.dimension,
                "ocr": tesseract_environment(),
                "collection": settings.collection, "hybrid_enabled": settings.hybrid_enabled,
                "authorized_reviewers": [value.strip() for value in os.getenv("GSME_AUTHORIZED_REVIEWERS", "").split(",") if value.strip()],
                "cohere_configured": bool(os.getenv("COHERE_API_KEY")) and
                os.getenv("GSME_EXTERNAL_CONTENT_APPROVED") == "yes",
                "cohere_rerank_configured": bool(os.getenv("COHERE_API_KEY")) and
                os.getenv("GSME_EXTERNAL_CONTENT_APPROVED") == "yes" and
                os.getenv("GSME_RERANK_CONTENT_APPROVED") == "yes",
                "cohere_rerank_missing_settings": missing_rerank_settings(),
                "answer_provider": os.getenv("GSME_ANSWER_PROVIDER", "cohere")}

    @app.get("/api/versions", dependencies=[Depends(authorize)])
    def versions(tenant: Tenant, limit: int = Query(default=50, ge=1, le=100)):
        return _safe(lambda: store.list_versions(tenant, limit))

    @app.get("/api/library", dependencies=[Depends(authorize)])
    def library(tenant: Tenant, provider: Literal["fake", "cohere"] = "cohere"):
        model = embedding_model_version(provider, settings.dimension)
        collection = settings.collection + ("_synthetic" if provider == "fake" else "")
        return _safe(lambda: store.library(tenant, model, collection))

    @app.get("/api/logs", dependencies=[Depends(authorize)])
    def recent_logs(limit: int = Query(default=80, ge=1, le=200)):
        path = settings.data_dir / "logs" / "console.log"
        if path.is_symlink() or not path.resolve().is_relative_to(settings.data_dir.resolve()):
            raise HTTPException(404, "Operator log unavailable")
        if not path.is_file():
            return {"entries": [], "note": "Logs appear after starting python -m gsme_rag.storage.admin"}
        with path.open(encoding="utf-8") as stream:
            lines = [line.rstrip("\n") for line in deque(stream, maxlen=limit)]
        return JSONResponse({"entries": lines}, headers={"Cache-Control": "no-store"})

    @app.get("/api/status/{job_id}", dependencies=[Depends(authorize)])
    def status(job_id: Identifier, tenant: Tenant):
        value = store.job_status(tenant, job_id)
        if not value:
            raise HTTPException(404, "Job not found for tenant")
        return value

    @app.get("/api/inspect/{version_id}", dependencies=[Depends(authorize)])
    def inspect(version_id: Identifier, tenant: Tenant, limit: int = Query(default=50, ge=1, le=100),
                offset: int = Query(default=0, ge=0)):
        value = _safe(lambda: store.inspect_version(tenant, version_id, limit, offset))
        if not value:
            raise HTTPException(404, "Version not found for tenant")
        value["index_jobs"] = store.index_status(tenant, version_id)
        return value

    @app.get("/api/markdown/{version_id}", dependencies=[Depends(authorize)])
    def markdown(version_id: Identifier, tenant: Tenant):
        value = store.inspect_version(tenant, version_id, 1)
        if not value or not value["version"]["artifact_path"]:
            raise HTTPException(404, "Normalized Markdown unavailable")
        path = Path(value["version"]["artifact_path"])
        if not path.is_file():
            raise HTTPException(404, "Normalized Markdown unavailable")
        try:
            content = verified_markdown(path, value["version"]["normalized_sha256"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return PlainTextResponse(content.decode("utf-8"))

    @app.get("/api/manifest/{version_id}", dependencies=[Depends(authorize)])
    def manifest(version_id: Identifier, tenant: Tenant):
        value = store.inspect_version(tenant, version_id, 1)
        if not value or not value["review_manifest_path"]:
            raise HTTPException(404, "Review manifest unavailable")
        path = Path(value["review_manifest_path"])
        if not path.is_file():
            raise HTTPException(404, "Review manifest unavailable")
        contents = json.loads(path.read_text(encoding="utf-8"))
        if contents.get("source_sha256") != value["version"]["source_sha256"]:
            raise HTTPException(409, "Manifest source hash does not match the stored version")
        return JSONResponse(contents)

    @app.get("/api/source/{version_id}", dependencies=[Depends(authorize)])
    def source(version_id: Identifier, tenant: Tenant):
        value = store.inspect_version(tenant, version_id, 1)
        if not value:
            raise HTTPException(404, "Version not found for tenant")
        path = Path(value["version"]["original_path"])
        if not path.is_file():
            raise HTTPException(404, "Original source unavailable")
        if checksum(path) != value["version"]["source_sha256"]:
            raise HTTPException(409, "Original source changed after submission; restore it or submit a new version")
        return FileResponse(path, filename=value["version"]["source_file"])

    @app.post("/api/submit", dependencies=[Depends(authorize)])
    def submit(file: UploadFile = File(...), tenant: str = Form(...),
               document_id: str | None = Form(default=None),
               schedule_kind: str | None = Form(default=None)):
        name = (file.filename or "upload").replace("\\", "/").split("/")[-1]
        if not name or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(415, "Unsupported source file")
        with tempfile.TemporaryDirectory(prefix="gsme-admin-") as temporary:
            path = Path(temporary) / name
            total = 0
            with path.open("wb") as output:
                while block := file.file.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Upload exceeds 200 MiB")
                    output.write(block)
            return _safe(lambda: store.submit(tenant, path, settings.data_dir,
                                              document_id or None, schedule_kind or None))

    @app.post("/api/ingest", dependencies=[Depends(authorize)])
    def ingest(body: JobRequest):
        if not store.job_status(body.tenant, body.job_id):
            raise HTTPException(404, "Job not found for tenant")
        return process_ingestion(store, settings, body.job_id) or {"state": "no_queued_job"}

    @app.post("/api/retry", dependencies=[Depends(authorize)])
    def retry(body: JobRequest):
        return {"requeued": _safe(lambda: store.retry_job(body.tenant, body.job_id))}

    @app.get("/api/schedules", dependencies=[Depends(authorize)])
    def schedules(tenant: Tenant, kind: Literal["full", "shared"],
                  technology_nm: int | None = Query(default=None, ge=1, le=2147483647), flavor: str | None = None,
                  tapeout_on_or_after: date | None = None):
        return store.schedules(tenant, kind, technology_nm=technology_nm, flavor=flavor,
                               tapeout_on_or_after=tapeout_on_or_after)

    @app.post("/api/schedule-search", dependencies=[Depends(authorize)])
    def schedule_search(body: ScheduleSearchRequest):
        return _safe(lambda: search_schedules(store, body.tenant, body, body.document_id))

    @app.get("/api/schedule-catalog", dependencies=[Depends(authorize)])
    def schedule_catalog(tenant: Tenant, document_id: Identifier | None = None):
        return _safe(lambda: store.schedule_catalog(tenant, document_id))

    @app.post("/api/review", dependencies=[Depends(authorize)])
    def review(body: ReviewRequest):
        reviewers = {item.strip() for item in os.getenv("GSME_AUTHORIZED_REVIEWERS", "").split(",")}
        if body.reviewer not in reviewers or not body.reviewer:
            raise HTTPException(403, "Reviewer is not on the local authorized list")
        _safe(lambda: store.record_review(body.tenant, body.version_id, body.chunk_id,
                                          body.decision, body.reviewer, body.rationale,
                                          body.source_verified))
        return {"recorded": True}

    @app.post("/api/review/batch", dependencies=[Depends(authorize)])
    def review_batch(body: BatchReviewRequest):
        reviewers = {item.strip() for item in os.getenv("GSME_AUTHORIZED_REVIEWERS", "").split(",")}
        if body.reviewer not in reviewers or not body.reviewer:
            raise HTTPException(403, "Reviewer is not on the local authorized list")
        count = _safe(lambda: store.record_reviews(
            body.tenant, body.version_id, body.chunk_ids, body.decision,
            body.reviewer, body.rationale, body.source_verified))
        return {"recorded": count}

    @app.post("/api/ingest-ready", dependencies=[Depends(authorize)])
    def ingest_ready(body: VersionRequest):
        """Normal workflow: accept clean text and index it without per-chunk forms."""
        if body.provider == "cohere" and not body.confirm_external:
            raise HTTPException(400, "Permit sending this document's accepted text to Cohere first")
        version = store.inspect_version(body.tenant, body.version_id, 1)
        if (not version or not body.document_id
                or str(version["version"]["document_id"]) != body.document_id):
            raise HTTPException(404, "Document version not found for tenant")
        with operation("ingestion.acceptance", version_id=body.version_id):
            acceptance = _safe(lambda: store.accept_clean_text(body.tenant, body.version_id))
        emit("ingestion.acceptance.counts", version_id=body.version_id,
             accepted=acceptance["accepted"], held=acceptance["held"])
        if not acceptance["accepted"]:
            return {**acceptance, "state": "held_for_review", "indexed": 0, "failed": 0,
                    "message": "Stored successfully, but no clean text is eligible for search. See extraction warnings."}
        provider = _safe(lambda: _provider(body.provider, settings))
        vectors = _vectors(body.provider, settings)
        try:
            _safe(lambda: store.publish(body.tenant, body.document_id, body.version_id,
                                        provider.model_version, vectors.collection))
            indexed = failed = 0
            for _ in range(20):
                result = process_index(store, vectors, provider, settings, body.tenant, body.version_id)
                indexed += result["indexed"]
                failed += result["failed"]
                if not result["indexed"] or result["failed"]:
                    break
            states = store.index_status(body.tenant, body.version_id)
            remaining = sum(int(row["chunks"]) for row in states if row["model_version"] == provider.model_version
                            and row["state"] in {"queued", "indexing", "failed"})
            return {**acceptance, "state": "index_failed" if failed else "index_pending" if remaining else "searchable",
                    "indexed": indexed, "failed": failed, "remaining": remaining,
                    "collection": vectors.collection, "model_version": provider.model_version}
        finally:
            vectors.client.close()

    @app.post("/api/publish", dependencies=[Depends(authorize)])
    def publish(body: VersionRequest):
        if not body.document_id:
            raise HTTPException(400, "Document ID required")
        model = _safe(lambda: embedding_model_version(body.provider, settings.dimension))
        # Publishing needs only the collection identity, not a Qdrant connection.
        collection = settings.collection + ("_synthetic" if body.provider == "fake" else "")
        count = _safe(lambda: store.publish(body.tenant, body.document_id, body.version_id,
                                            model, collection))
        return {"approved_candidates": count, "model_version": model,
                "collection": collection}

    @app.post("/api/index", dependencies=[Depends(authorize)])
    def index(body: VersionRequest):
        if body.provider == "cohere" and not body.confirm_external:
            raise HTTPException(400, "Confirm sending only approved evidence to Cohere")
        version = store.inspect_version(body.tenant, body.version_id, 1)
        if not version:
            raise HTTPException(404, "Version not found for tenant")
        provider = _safe(lambda: _provider(body.provider, settings))
        with _managed_vectors(body.provider, settings) as vectors:
            result = _safe(lambda: process_index(store, vectors, provider, settings, body.tenant, body.version_id))
            return {"index": result, "reconciled": _safe(lambda: reconcile(store, vectors, provider.model_version)),
                    "model_version": provider.model_version, "collection": vectors.collection}

    @app.post("/api/search", dependencies=[Depends(authorize)])
    def search(body: SearchRequest):
        provider = _safe(lambda: _provider(body.provider, settings))
        with _managed_vectors(body.provider, settings) as vectors:
            return _safe(lambda: search_evidence(store, vectors, provider, settings, body.tenant,
                                                 body.question, body.limit, body.hybrid))

    @app.post("/api/retrieve", dependencies=[Depends(authorize)])
    def retrieval(body: RetrievalRequest):
        """Explicit routes avoid guessing schedule kind, dates or technical values."""
        routing = None
        if body.route == "auto":
            routing = _safe(lambda: plan_query(body.question, body.query_mode))
            body = body.model_copy(update={"route": routing.route,
                "schedule_kind": body.schedule_kind or routing.schedule_kind,
                "generate_answer": body.generate_answer and routing.route != "schedules"})
        if body.generate_answer:
            if body.provider != "cohere" or body.route not in {"documents", "mixed"}:
                raise HTTPException(400, "Generated answers require real document retrieval; exact schedule rows stay separate")
            _safe(lambda: require_answer_permission(body.confirm_answer_external))
        if body.route == "schedules" and (body.category or body.source_file):
            raise HTTPException(400, "Category/source-file filters apply to document retrieval only")
        if body.route != "schedules" and (body.schedule_kind is not None or body.technology_nm is not None
                                          or body.flavor is not None or body.tapeout_on_or_after is not None
                                          or body.date_from is not None or body.date_to is not None
                                          or body.next_dates is not None or body.deadline_status != "all"
                                          or body.date_field != "tapeout_date" or body.schedule_offset):
            if body.route != "mixed":
                raise HTTPException(400, "Schedule filters require the schedules or mixed route")
        schedule_rows = []
        schedule_result = None
        if body.route in {"schedules", "mixed"}:
            fields = {key: getattr(body, key) for key in ScheduleQuery.model_fields
                      if key in body.model_fields_set and key not in {"limit", "offset", "snapshot"}}
            fields.update(question=body.question, limit=body.schedule_limit,
                          offset=body.schedule_offset, snapshot=body.schedule_snapshot)
            if body.tapeout_on_or_after:
                if (body.date_from and body.date_from != body.tapeout_on_or_after) or body.date_field != "tapeout_date":
                    raise HTTPException(400, "Conflicting legacy tapeout filter; use date_field/date_from")
                fields["date_from"] = body.tapeout_on_or_after
            schedule_result = _safe(lambda: search_schedules(store, body.tenant,
                ScheduleQuery(**fields), body.document_id))
            if schedule_result["status"] == "clarification_needed":
                return schedule_result
            schedule_rows = schedule_result["results"]
        schedule_has_more = bool(schedule_result and schedule_result["has_more"])
        if body.route == "schedules":
            return schedule_result
        if body.provider == "cohere" and not body.confirm_external:
            raise HTTPException(400, "Confirm sending the question and approved passages to Cohere for retrieval/reranking")
        options = _safe(lambda: RetrievalOptions(body.top_k, body.candidate_count, body.strategy,
                        body.document_id, body.category, body.source_file, body.context_chars, body.min_rerank_score, body.query_mode))
        provider = _safe(lambda: _provider(body.provider, settings))
        reranker = _safe(lambda: FakeReranker() if body.provider == "fake" else CohereReranker(
            settings.rerank_model, settings.rerank_timeout, settings.rerank_input_budget))
        # The new endpoint provisions the native keyword index when requested;
        # the old GSME_HYBRID switch still controls baseline-search experiments.
        vectors = QdrantEvidence(settings.qdrant_url,
            settings.collection + ("_synthetic" if body.provider == "fake" else ""),
            settings.dimension, hybrid=body.strategy == "hybrid")
        try:
            result = _safe(lambda: retrieve(store, vectors, provider, reranker, settings,
                                            body.tenant, body.question, options))
        finally:
            vectors.client.close()
        if body.generate_answer:
            result["answer"] = _safe(lambda: generate_answer(body.question, result,
                lambda part: store.is_source_current_approved(body.tenant, part["version_id"], part["chunk_id"])))
            timings = result.setdefault("timings_ms", {})
            timings["answer_generation"] = result["answer"].get("timings_ms", {}).get("total", 0)
            timings["total_including_answer"] = round(timings.get("total", 0) + timings["answer_generation"], 3)
        if body.route == "mixed":
            result.update(route="mixed", schedule_rows=schedule_rows,
                          schedule_has_more=schedule_has_more, schedule_limit=body.schedule_limit,
                          schedule_status="results" if schedule_rows else "no_matching_rows")
            result["schedule_search"] = schedule_result
        if routing:
            result["routing"] = {"mode": "local_rules", "route": body.route,
                                 "reasons": list(routing.reasons)}
        return result

    @app.get("/api/embedding/{source_chunk_id}", dependencies=[Depends(authorize)])
    def embedding(source_chunk_id: str, tenant: Tenant, version_id: Identifier,
                  provider: Literal["fake", "cohere"] = "cohere"):
        model = _safe(lambda: embedding_model_version(provider, settings.dimension))
        with _managed_vectors(provider, settings) as vectors:
            point = store.approved_point(tenant, version_id, source_chunk_id,
                                         model, vectors.collection)
            if not point:
                raise HTTPException(404, "No current approved indexed vector for this chunk")
            if not vectors.client.collection_exists(vectors.collection):
                raise HTTPException(404, "Vector collection is unavailable")
            found = vectors.client.retrieve(vectors.collection, ids=[str(point["point_id"])],
                                            with_payload=False, with_vectors=True)
            if not found or not isinstance(found[0].vector, list):
                raise HTTPException(404, "Vector point is unavailable")
            values = found[0].vector
            return {"source_chunk_id": source_chunk_id, "source_file": point["source_file"],
                    "page_start": point["source_page_start"], "page_end": point["source_page_end"],
                    "model_version": model, "collection": vectors.collection,
                    "point_id": str(point["point_id"]), "dimension": len(values),
                    "l2_norm": math.sqrt(sum(value * value for value in values)),
                    "first_8_values": values[:8],
                    "note": "Preview only; vector coordinates do not explain semantic meaning."}

    @app.post("/api/reconcile", dependencies=[Depends(authorize)])
    def cleanup(body: VersionRequest):
        model = _safe(lambda: embedding_model_version(body.provider, settings.dimension))
        with _managed_vectors(body.provider, settings) as vectors:
            return {"deleted": _safe(lambda: reconcile(store, vectors, model))}

    @app.post("/api/repair", dependencies=[Depends(authorize)])
    def repair(body: VersionRequest):
        model = _safe(lambda: embedding_model_version(body.provider, settings.dimension))
        with _managed_vectors(body.provider, settings) as vectors:
            return {"requeued_missing_points": _safe(lambda: repair_missing_vectors(store, vectors, model,
                                                                       body.tenant, body.version_id))}

    return app


def main():
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(override=False)
    settings = from_env()
    configure_console_logging(settings.data_dir)
    emit("console.started", version=__version__, missing_settings=missing_rerank_settings())
    app = create_admin_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("GSME_ADMIN_PORT", "8010")))


if __name__ == "__main__":
    main()
