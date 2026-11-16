"""Hosted chatbot API. Never expose the separate local operator console.

Deploy behind HTTPS with an explicit origin. No browser-supplied
workspace, reviewer, visibility or identity is accepted. Uploads are private.
"""

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from hashlib import sha256
import os
import math
from pathlib import Path
import tempfile
from threading import BoundedSemaphore, Lock
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
import psycopg

from gsme_rag.generation.answers import generate_answer, require_answer_permission
from gsme_rag.observability import emit, REQUEST_ID, configure_console_logging
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.schedules import ScheduleQuery, search_schedules, ScheduleSnapshotChanged
from gsme_rag.storage.config import from_env
from gsme_rag.storage.files import checksum, verified_markdown
from gsme_rag.storage.pipeline import process_ingestion, process_index
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import CohereEmbeddings, ProviderOperationError
from gsme_rag.retrieval.reranking import CohereReranker
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.version import __version__
from .access import AccessStore, Principal
from .identity import google_principal, IdentityUnavailable, InvalidIdentity
from .sessions import browser_principal, create_browser_session, SESSION_SECONDS


@dataclass(frozen=True)
class HostedSettings:
    origin: str = "http://127.0.0.1:8020"
    tenant: str = "company"
    google_client_id: str = ""
    workspace_domains: tuple[str, ...] = ()
    public_faq_guests: bool = True
    guest_uploads: bool = True
    upload_global_day: int = 30
    chat_per_user_minute: int = 6
    chat_global_minute: int = 8
    chat_global_day: int = 50
    min_rerank_score: float | None = None

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path or url.scheme not in {"http", "https"}
                or (url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1"})
                or not self.tenant.strip() or len(self.tenant) > 120):
            raise ValueError("Configure an HTTPS application origin and fixed company workspace")
        if not 1 <= self.chat_per_user_minute <= 60 or not 1 <= self.chat_global_minute <= 600:
            raise ValueError("Invalid hosted request limits")
        if not 1 <= self.chat_global_day <= 100000:
            raise ValueError("Set a bounded daily chat allowance")
        if not 1 <= self.upload_global_day <= 10000:
            raise ValueError("Set a bounded daily upload allowance")
        if self.min_rerank_score is not None and (not math.isfinite(self.min_rerank_score) or not 0 <= self.min_rerank_score <= 1):
            raise ValueError("Invalid relevance threshold")
        if self.google_client_id and (not self.google_client_id.endswith(".apps.googleusercontent.com") or not self.workspace_domains):
            raise ValueError("Workspace sign-in needs a Google web client ID and allowed hosted domains")

    @classmethod
    def from_env(cls):
        return cls(origin=os.getenv("GSME_HOSTED_ORIGIN", cls.origin),
            tenant=os.getenv("GSME_HOSTED_TENANT", cls.tenant),
            google_client_id=os.getenv("GSME_GOOGLE_CLIENT_ID", ""),
            workspace_domains=tuple(d.strip().casefold() for d in os.getenv("GSME_GOOGLE_WORKSPACE_DOMAINS", "").split(",") if d.strip()),
            public_faq_guests=os.getenv("GSME_PUBLIC_FAQ_GUEST_ACCESS", "yes") == "yes",
            guest_uploads=os.getenv("GSME_GUEST_UPLOADS_ENABLED", "yes") == "yes",
            upload_global_day=int(os.getenv("GSME_UPLOAD_GLOBAL_DAY", "30")),
            chat_per_user_minute=int(os.getenv("GSME_CHAT_PER_USER_MINUTE", "6")),
            chat_global_minute=int(os.getenv("GSME_CHAT_GLOBAL_MINUTE", "8")),
            chat_global_day=int(os.getenv("GSME_CHAT_GLOBAL_DAY", "50")),
            min_rerank_score=float(os.environ["GSME_MIN_RERANK_SCORE"]) if os.getenv("GSME_MIN_RERANK_SCORE") else None)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    document_id: UUID | None = None
    generate_answer: bool = True


class HostedScheduleQuery(ScheduleQuery):
    document_id: UUID | None = None


def cited_source_groups(answer, results):
    """Display only validated cited quotes; keep all original source identities.

    Identical display quotes are folded, not rewritten. Distinct passages from
    one original are grouped under one file instead of pretending to be files.
    """
    if not answer or answer.get("status") != "answered":
        return []
    parts = {(str(p["version_id"]), p["chunk_id"]): p
             for r in results for p in r.get("context_parts", [])}
    groups = {}
    for citation in answer.get("citations", []):
        part = parts.get((str(citation["version_id"]), citation["chunk_id"]))
        if part is None:
            continue
        label = citation["source_id"]
        quotes = list(dict.fromkeys(s["quote"] for claim in answer.get("claims", [])
                      for s in claim.get("supports", []) if s["source_id"] == label))
        quotes = [q for q in quotes if q and q in part["text"]]
        if not quotes:
            continue
        identity = part.get("source_sha256") or str(part["version_id"])
        group = groups.setdefault(identity, {"source_file": part["source_file"], "passages": []})
        for quote in quotes:
            match = next((p for p in group["passages"] if p["quote"] == quote), None)
            if match is None:
                match = {"quote": quote, "references": []}
                group["passages"].append(match)
            reference = {"source_id": label, "version_id": str(part["version_id"]),
                         "chunk_id": part["chunk_id"], "page_start": part.get("page_start"),
                         "page_end": part.get("page_end")}
            if reference not in match["references"]:
                match["references"].append(reference)
    return list(groups.values())


def create_hosted_app(settings=None, hosted=None, store=None, verifier=None, providers=None):
    settings, hosted = settings or from_env(), hosted or HostedSettings.from_env()
    if store is None and providers is None:
        configure_console_logging(settings.data_dir)
    store = store or EvidenceStore(settings.database_url)
    verifier = verifier or (lambda token: google_principal(token, hosted.google_client_id, hosted.workspace_domains))

    @contextmanager
    def backend(need_reranker=True):
        # SDK clients also own connection pools. Register each as it is created,
        # including when a later constructor/preflight fails.
        with ExitStack() as resources:
            if providers is not None:  # Python-only synthetic test injection.
                provider, reranker, vectors = providers()
            else:
                provider = CohereEmbeddings(settings.dimension)
                resources.enter_context(provider.client)
                reranker = None
                if need_reranker:
                    reranker = CohereReranker(settings.rerank_model, settings.rerank_timeout, settings.rerank_input_budget)
                    resources.enter_context(reranker.client)
                vectors = QdrantEvidence(settings.qdrant_url, settings.collection, settings.dimension, hybrid=True)
            resources.callback(vectors.client.close)
            yield provider, reranker, vectors
    app = FastAPI(title="GSME Muse", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(hosted.origin).hostname])
    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")
    chat_slots, upload_slots = BoundedSemaphore(2), BoundedSemaphore(4)
    processing_versions, processing_lock = set(), Lock()
    session_cookie = "__Host-gsme_guest" if hosted.origin.startswith("https:") else "gsme_guest"

    def rate(bucket, limit, seconds=60):
        minute = int(time.time() // seconds) * (seconds // 60)
        key = sha256((hosted.tenant + "|" + bucket + "|" + str(seconds)).encode()).hexdigest()
        with store.connect() as db:
            row = db.execute("""INSERT INTO hosted_request_limits(bucket_key,minute,requests) VALUES (%s,%s,1)
                ON CONFLICT(bucket_key,minute) DO UPDATE SET requests=hosted_request_limits.requests+1
                WHERE hosted_request_limits.requests < %s RETURNING requests""", (key, minute, limit)).fetchone()
            if not row:
                retry = max(1, int(seconds - time.time() % seconds))
                raise HTTPException(429, "Request allowance reached; retry after the current window", headers={"Retry-After": str(retry)})
            db.execute("DELETE FROM hosted_request_limits WHERE minute < %s", (int(time.time() // 60) - 2880,))

    async def request_trace(request, call_next):
        token = REQUEST_ID.set(str(uuid4()))
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-GSME-Request-ID"] = REQUEST_ID.get()
            if request.url.path.startswith("/api/") and (request.method == "POST" or response.status_code >= 400):
                emit("hosted.http.completed", method=request.method,
                     route=getattr(request.scope.get("route"), "path", "unmatched"),
                     status=response.status_code, elapsed_ms=round((time.perf_counter()-started)*1000, 2))
            return response
        except Exception as error:
            emit("hosted.http.failed", method=request.method,
                 route=getattr(request.scope.get("route"), "path", "unmatched"),
                 status=500, error_type=type(error).__name__)
            raise
        finally:
            REQUEST_ID.reset(token)

    @app.middleware("http")
    async def boundary(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD"} and origin and origin != hosted.origin:
            return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' https://accounts.google.com/gsi/client; "
                "connect-src 'self' https://accounts.google.com; frame-src 'self' blob: https://accounts.google.com; "
                "style-src 'self' https://accounts.google.com/gsi/style; img-src 'self' data: blob:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        if hosted.origin.startswith("https:"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # Trace even boundary rejections; never accept a caller-provided trace ID.
    app.middleware("http")(request_trace)

    @app.exception_handler(ValueError)
    async def invalid_request(request, error):
        emit("hosted.request.invalid", error_type=type(error).__name__)
        return JSONResponse({"detail": "Request or provider configuration is invalid; contact the operator"}, status_code=400)

    @app.exception_handler(ProviderOperationError)
    async def provider_unavailable(request, error):
        emit("hosted.provider.unavailable", level=40, error_type=type(error).__name__)
        return JSONResponse({"detail": "Search provider unavailable; no unreranked answer returned"}, status_code=502)

    @app.exception_handler(ScheduleSnapshotChanged)
    async def schedule_changed(request, error):
        return JSONResponse({"detail": "Schedule sources or filters changed; restart the search"}, status_code=409)

    @app.exception_handler(psycopg.OperationalError)
    async def storage_unavailable(request, error):
        emit("hosted.storage.unavailable", level=40, error_type=type(error).__name__)
        return JSONResponse({"detail": "Storage is temporarily unavailable"}, status_code=503)

    @app.exception_handler(psycopg.errors.UndefinedTable)
    async def setup_missing(request, error):
        emit("hosted.schema.incomplete", level=40, error_type=type(error).__name__)
        return JSONResponse({"detail": "Database setup is incomplete. Restart the chatbot launcher or run python -m gsme_rag.storage.cli migrate"}, status_code=503)

    def identity(request: Request):
        # Limit unverified requests before Google certificate retrieval too.
        rate("ip:" + (request.client.host if request.client else "unknown"), 120)
        value = request.headers.get("authorization")
        if not value:
            token = request.cookies.get(session_cookie) if hosted.guest_uploads else None
            if token:
                # A custom header plus SameSite=Strict prevents other sites from
                # submitting authenticated forms using this browser's cookie.
                if request.method not in {"GET", "HEAD"} and request.headers.get("x-gsme-session") != "1":
                    raise HTTPException(403, "Browser workspace header required")
                principal = browser_principal(store, hosted.tenant, token)
                if principal is None:
                    raise HTTPException(401, "Browser workspace expired. Reload the page to start a new workspace")
                return principal
            if not hosted.public_faq_guests:
                raise HTTPException(401, "Company sign-in required")
            return Principal(None)
        if not value.startswith("Bearer "):
            raise HTTPException(401, "Invalid sign-in credential")
        try:
            principal = verifier(value[7:])
            if not isinstance(principal, Principal) or not principal.subject:
                raise InvalidIdentity("Invalid sign-in credential")
            return principal
        except IdentityUnavailable:
            raise HTTPException(503, "Company sign-in is not configured") from None
        except InvalidIdentity:
            raise HTTPException(401, "Company sign-in could not be verified") from None

    def access(principal=Depends(identity)):
        return AccessStore(store, hosted.tenant, principal, hosted.public_faq_guests)

    @app.get("/")
    def home():
        return FileResponse(web / "index.html")

    @app.get("/api/config")
    def config():
        return {"version": __version__, "google_client_id": hosted.google_client_id,
                "private_uploads_enabled": bool(hosted.google_client_id and hosted.workspace_domains),
                "guest_uploads_enabled": hosted.guest_uploads,
                "uploads_enabled": hosted.guest_uploads or bool(hosted.google_client_id and hosted.workspace_domains),
                "document_actions": ["retry", "delete_own_upload"],
                "public_faqs": hosted.public_faq_guests}

    @app.post("/api/session")
    def browser_session(request: Request):
        if not hosted.guest_uploads:
            raise HTTPException(403, "Browser uploads are disabled")
        if request.headers.get("x-gsme-session") != "1":
            raise HTTPException(403, "Browser workspace header required")
        rate("session:" + (request.client.host if request.client else "unknown"), 10)
        token = request.cookies.get(session_cookie)
        principal = browser_principal(store, hosted.tenant, token)
        response = JSONResponse({"status": "ready", "session_lifetime_seconds": SESSION_SECONDS,
            "message": "Uploads stay in this browser workspace. Clearing cookies or seven-day expiry loses browser access; this is not a company account."})
        if principal is None:
            token = create_browser_session(store, hosted.tenant)
            response.set_cookie(session_cookie, token, max_age=SESSION_SECONDS, httponly=True,
                                secure=hosted.origin.startswith("https:"), samesite="strict", path="/")
        return response

    @app.get("/api/documents")
    def documents(scoped=Depends(access)):
        rows = scoped.visible_documents()
        for row in rows:
            row["processing"] = str(row["version_id"]) in processing_versions
        return {"documents": rows[:100], "has_more": len(rows) > 100,
                "signed_in": scoped.principal.company_signed_in,
                "browser_workspace": bool(scoped.principal.subject and scoped.principal.subject.startswith("guest:"))}

    @app.get("/api/documents/{version_id}/status")
    def document_status(version_id: UUID, scoped=Depends(access)):
        inspection = scoped.inspect_version(hosted.tenant, str(version_id))
        if not inspection or not scoped.owned_document(str(inspection["version"]["document_id"])):
            raise HTTPException(404, "Document unavailable")
        with store.connect() as db:
            counts = db.execute("""SELECT count(*) AS chunks,
                count(*) FILTER(WHERE evidence_status='approved') AS accepted,
                count(*) FILTER(WHERE evidence_status='pending') AS held
                FROM evidence_chunks WHERE version_id=%s""", (version_id,)).fetchone()
            jobs = db.execute("""SELECT j.state,count(*) AS count FROM index_jobs j
                JOIN evidence_chunks c ON c.id=j.chunk_id WHERE c.version_id=%s GROUP BY j.state""",
                (version_id,)).fetchall()
            reasons = db.execute("""SELECT code,count(*) AS count FROM evidence_chunks c,
                jsonb_array_elements_text(c.warning_codes) AS code
                WHERE c.version_id=%s AND c.evidence_status='pending'
                GROUP BY code ORDER BY count(*) DESC LIMIT 8""", (version_id,)).fetchall()
            cooldown = db.execute("""SELECT COALESCE(ceil(max(extract(epoch FROM
                (j.next_attempt_at-now())))),0)::int AS seconds FROM index_jobs j
                JOIN evidence_chunks c ON c.id=j.chunk_id WHERE c.version_id=%s
                AND j.state='failed' AND j.last_error LIKE '%%HTTP 429%%'
                AND j.next_attempt_at>now()""", (version_id,)).fetchone()["seconds"]
        return {"version_id": str(version_id), "document_id": str(inspection["version"]["document_id"]),
                "state": inspection["version"]["state"], "document_class": inspection["version"]["document_class"],
                "processing": str(version_id) in processing_versions,
                **counts, "held_reasons": reasons, "retry_after_seconds": cooldown,
                "index_jobs": {row["state"]: row["count"] for row in jobs}}

    @app.delete("/api/documents/{version_id}")
    def remove_document(version_id: UUID, scoped=Depends(access)):
        if not scoped.remove_owned_document(str(version_id)):
            raise HTTPException(404, "Your private upload is unavailable; shared library files cannot be deleted here")
        emit("hosted.upload.removed", version_id=str(version_id))
        return {"status": "removed", "message": "Removed from chat and search. Original and audit evidence retained for recovery."}

    def checked_source(version_id, scoped):
        if not scoped.visible_version(str(version_id), download=True):
            raise HTTPException(404, "Source unavailable")
        item = scoped.inspect_version(hosted.tenant, str(version_id))
        if not item:
            raise HTTPException(404, "Source unavailable")
        path = Path(item["version"]["original_path"])
        if not path.is_file() or checksum(path) != item["version"]["source_sha256"]:
            raise HTTPException(409, "Source integrity check failed")
        return path, item["version"]

    @app.get("/api/source/{version_id}")
    def source(version_id: UUID, scoped=Depends(access)):
        path, version = checked_source(version_id, scoped)
        inline = path.suffix.casefold() in {".pdf", ".png", ".jpg", ".jpeg", ".bmp"}
        return FileResponse(path, filename=version["source_file"],
                            content_disposition_type="inline" if inline else "attachment")

    @app.get("/api/preview/{version_id}")
    def preview(version_id: UUID, scoped=Depends(access)):
        path, version = checked_source(version_id, scoped)
        suffix = path.suffix.casefold()
        result = {"filename": version["source_file"], "source_sha256": version["source_sha256"]}
        if suffix == ".pdf":
            return {**result, "kind": "pdf"}
        if suffix in {".png", ".jpg", ".jpeg", ".bmp"}:
            return {**result, "kind": "image"}
        if suffix == ".csv":
            return {**result, "kind": "text", "text": path.read_text(encoding="utf-8-sig"),
                    "notice": "Original CSV text; no spreadsheet formulas are executed."}
        artifact = version.get("artifact_path")
        if not artifact or not Path(artifact).is_file() or not version.get("normalized_sha256"):
            raise HTTPException(409, "Text preview is not ready; the document must finish parsing first")
        try:
            text = verified_markdown(Path(artifact), version["normalized_sha256"]).decode("utf-8")
        except (ValueError, UnicodeError):
            raise HTTPException(409, "Preview integrity check failed; the parsed artifact has changed") from None
        # Office files are not sent to a third-party viewer. This is explicitly
        # extracted text, not a claim to reproduce original formatting or verify it.
        return {**result, "kind": "text", "text": text,
                "notice": "Extracted text preview, not the original Office layout. Tables, images and formulas may differ; uncertain extraction is not verified."}

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request, scoped=Depends(access)):
        rate("chat:" + (scoped.principal.subject or "guest:" + request.client.host), hosted.chat_per_user_minute)
        rate("chat:global", hosted.chat_global_minute)
        rate("chat:global", hosted.chat_global_day, seconds=86400)
        if not chat_slots.acquire(blocking=False):
            raise HTTPException(503, "Search is busy; retry shortly", headers={"Retry-After": "3"})
        try:
            document = str(body.document_id) if body.document_id else None
            emit("hosted.chat.started", question_hash=sha256(body.question.encode()).hexdigest(),
                 query_chars=len(body.question), document_id=document)
            # Chat always searches documents. Exact schedule lookup is an
            # explicit API operation, never a guess based on words like GDS.
            greeting = " ".join(body.question.casefold().strip(" !?.").split())
            if greeting in {"hi", "hello", "hey", "how are you", "hi how are you"}:
                return {"route": "documents", "status": "greeting", "answer": {
                    "status": "greeting", "text": "Hi! I'm ready to help. Ask me about the FAQs or choose a document below.",
                    "claims": [], "citations": []}, "results": [], "schedule_search": None,
                    "rerank_input_count": 0, "timings_ms": {}}
            if body.generate_answer:
                require_answer_permission(True)  # Organization policy, not a browser reviewer flag.
            with backend() as (provider, reranker, vectors):
                report = retrieve(scoped, vectors, provider, reranker, settings, hosted.tenant,
                    body.question, RetrievalOptions(document_id=document, min_rerank_score=hosted.min_rerank_score))
            answer = generate_answer(body.question, report,
                lambda part: scoped.is_source_current_approved(hosted.tenant, part["version_id"], part["chunk_id"])) if body.generate_answer else None
            if answer is not None and report["status"] == "no_eligible_evidence":
                # Missing index/workspace setup is not a negative evidence result.
                answer.update(status="no_searchable_documents", text=(
                    "No approved indexed passages are available for the selected document. Choose All files or another document."
                    if document else "No approved indexed documents are available in this chatbot workspace. If sources were just shared, restart the chatbot server and refresh the page."))
            # A permission/source change while the model is running must also
            # remove old retrieval extracts, not merely suppress generated prose.
            visible = [item for item in report["results"] if item.get("context_parts") and all(
                scoped.is_source_current_approved(hosted.tenant, part["version_id"], part["chunk_id"])
                for part in item["context_parts"])]
            if len(visible) != len(report["results"]):
                report["results"], report["status"] = visible, "evidence_changed"
                answer = {"status": "evidence_changed", "text": "Source access changed. Please search again.",
                          "claims": [], "citations": []} if answer is not None else None
            # No before/after raw candidate dump or unapproved-source metadata is
            # exposed on the public endpoint. Offline operator evaluation keeps it.
            sources = cited_source_groups(answer, visible)
            emit("hosted.chat.completed", retrieval_status=report["status"],
                 answer_status=answer.get("status") if answer else "not_requested",
                 cited_sources=len(sources), results=len(visible),
                 backend=answer.get("provider") if answer else None,
                 model=answer.get("model") if answer else None,
                 fallback_used=answer.get("fallback_used") if answer else None)
            return {"route": "documents", "status": report["status"], "answer": answer,
                    "request_id": REQUEST_ID.get(), "answer_sources": sources,
                    "results": report["results"], "schedule_search": None,
                    "rerank_input_count": report["rerank_input_count"], "timings_ms": report["timings_ms"]}
        finally:
            chat_slots.release()

    @app.post("/api/schedules/search")
    def exact_schedules(body: HostedScheduleQuery, scoped=Depends(access)):
        result = search_schedules(scoped, hosted.tenant, body, str(body.document_id) if body.document_id else None)
        for row in result["results"]:
            if not scoped.visible_version(str(row["version_id"]), download=True):
                row["source_url"] = None
        return result

    def ingest_private(job, version):
        try:
            inspection = store.inspect_version(hosted.tenant, version, 1)
            if inspection["version"]["state"] in {"queued", "parsing"}:
                parsed = process_ingestion(store, settings, job)
                if not parsed or parsed["state"] != "review_needed":
                    return
            inspection = store.inspect_version(hosted.tenant, version, 1)
            if inspection["version"]["document_class"] == "schedule":
                return
            accepted = store.accept_clean_text(hosted.tenant, version)
            if not accepted["accepted"]:
                return
            with backend(need_reranker=False) as (provider, _, vectors):
                inspection = store.inspect_version(hosted.tenant, version, 1)
                store.publish(hosted.tenant, str(inspection["version"]["document_id"]), version,
                              provider.model_version, vectors.collection)
                for _ in range(20):
                    result = process_index(store, vectors, provider, settings, hosted.tenant, version)
                    if not result["indexed"] or result["failed"]:
                        break
                # An idempotent publish may have no new jobs. Restore the ready
                # state from existing completed jobs instead of polling forever.
                store.mark_indexed_versions([UUID(version)])
        except Exception as error:
            emit("hosted.upload.failed", version_id=version, error_type=type(error).__name__)
        finally:
            with processing_lock:
                processing_versions.discard(version)
            upload_slots.release()

    @app.post("/api/upload")
    def upload(background: BackgroundTasks, request: Request, file: UploadFile = File(...),
               schedule_kind: str | None = Form(default=None), document_id: UUID | None = Form(default=None), scoped=Depends(access)):
        if not scoped.principal.subject:
            raise HTTPException(401, "Open the chatbot to create a browser upload workspace")
        if document_id and not scoped.owned_document(str(document_id)):
            raise HTTPException(404, "Private document unavailable")
        rate("upload:" + scoped.principal.subject, 3)
        rate("upload-ip:" + (request.client.host if request.client else "unknown"), 3)
        rate("upload:global", 6)
        rate("upload:global", hosted.upload_global_day, seconds=86400)
        if not upload_slots.acquire(blocking=False):
            raise HTTPException(503, "Upload queue is busy; retry shortly")
        submitted = None
        try:
            name = Path((file.filename or "upload").replace("\\", "/")).name
            if not name or len(name) > 180 or any(ord(c) < 32 for c in name):
                raise HTTPException(400, "Choose a bounded ordinary filename")
            from gsme_rag.storage.admin import ALLOWED_SUFFIXES
            if Path(name).suffix.casefold() not in ALLOWED_SUFFIXES:
                raise HTTPException(415, "Unsupported document type")
            with tempfile.TemporaryDirectory(prefix="gsme-private-upload-") as temporary:
                path, size = Path(temporary) / name, 0
                with path.open("wb") as output:
                    while block := file.file.read(1024 * 1024):
                        size += len(block)
                        if size > 20 * 1024 * 1024:
                            raise HTTPException(413, "Upload exceeds 20 MiB")
                        output.write(block)
                submitted = store.submit(hosted.tenant, path, settings.data_dir,
                                         str(document_id) if document_id else None, schedule_kind=schedule_kind)
                if not document_id:
                    scoped.claim_new_document(submitted["document_id"])
            if submitted["no_op"]:
                upload_slots.release()
                return {"version_id": submitted["version_id"], "status": "existing", "visibility": "owner_only"}
            with processing_lock:
                processing_versions.add(submitted["version_id"])
            background.add_task(ingest_private, submitted["job_id"], submitted["version_id"])
            return {"version_id": submitted["version_id"], "status": "queued", "visibility": "owner_only"}
        except BaseException:
            upload_slots.release()
            raise

    @app.post("/api/documents/{version_id}/resume")
    def resume(version_id: UUID, background: BackgroundTasks, scoped=Depends(access)):
        inspection = scoped.inspect_version(hosted.tenant, str(version_id))
        if not inspection or not scoped.owned_document(str(inspection["version"]["document_id"])):
            raise HTTPException(404, "Private document unavailable")
        rate("resume:" + scoped.principal.subject, 6)
        with store.connect() as db:
            cooldown = db.execute("""SELECT COALESCE(ceil(max(extract(epoch FROM
                (j.next_attempt_at-now())))),0)::int AS seconds FROM index_jobs j
                JOIN evidence_chunks c ON c.id=j.chunk_id WHERE c.version_id=%s
                AND j.state='failed' AND j.last_error LIKE '%%HTTP 429%%'
                AND j.next_attempt_at>now()""", (version_id,)).fetchone()["seconds"]
        if cooldown:
            raise HTTPException(429, f"Cohere rate limit: wait {cooldown} seconds before Retry. Already indexed passages remain searchable.",
                                headers={"Retry-After": str(cooldown)})
        with processing_lock:
            if str(version_id) in processing_versions:
                return {"status": "processing", "version_id": str(version_id)}
        if not upload_slots.acquire(blocking=False):
            raise HTTPException(503, "Upload queue is busy; retry shortly")
        try:
            with store.connect() as db:
                row = db.execute("SELECT id FROM ingestion_jobs WHERE tenant_id=%s AND version_id=%s",
                                 (hosted.tenant, version_id)).fetchone()
            if not row:
                raise HTTPException(404, "Document job unavailable")
            if inspection["version"]["state"] == "failed":
                try:
                    if not store.retry_job(hosted.tenant, str(row["id"])):
                        raise HTTPException(409, "Parse cannot be retried in its current state")
                except ValueError as error:
                    raise HTTPException(409, str(error)) from None
            with processing_lock:
                if str(version_id) in processing_versions:
                    raise HTTPException(409, "Document is already processing")
                processing_versions.add(str(version_id))
            background.add_task(ingest_private, str(row["id"]), str(version_id))
            return {"status": "queued", "version_id": str(version_id)}
        except BaseException:
            upload_slots.release()
            raise

    return app


def main():
    import uvicorn
    settings = from_env()
    # The local launcher prepares additive schema before serving any requests.
    # Factory/managed deployments can continue to run migrations separately.
    from gsme_rag.observability import configure_console_logging
    configure_console_logging(settings.data_dir)
    print(f"Operation logs: {settings.data_dir / 'logs' / 'console.log'}", flush=True)
    store = EvidenceStore(settings.database_url)
    store.migrate()
    app = create_hosted_app(settings, store=store)
    # Use a TLS reverse proxy. Never change the local admin's loopback boundary.
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("GSME_HOSTED_PORT", "8020")),
                proxy_headers=True, forwarded_allow_ips=os.getenv("GSME_TRUSTED_PROXY_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
