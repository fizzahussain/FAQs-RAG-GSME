"""Hosted chatbot API. Never expose the separate local operator console.

Deploy behind HTTPS with explicit Google origins/domains. No browser-supplied
workspace, reviewer, visibility or identity is accepted. Uploads are private.
"""

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from hashlib import sha256
import os
import math
from pathlib import Path
import tempfile
from threading import BoundedSemaphore
import time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
import psycopg

from gsme_rag.generation.answers import generate_answer, require_answer_permission
from gsme_rag.observability import emit
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.planning import plan_query
from gsme_rag.retrieval.schedules import ScheduleQuery, search_schedules, ScheduleSnapshotChanged
from gsme_rag.storage.config import from_env
from gsme_rag.storage.files import checksum
from gsme_rag.storage.pipeline import process_ingestion, process_index
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import CohereEmbeddings, ProviderOperationError
from gsme_rag.retrieval.reranking import CohereReranker
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.version import __version__
from .access import AccessStore, Principal
from .identity import google_principal, IdentityUnavailable, InvalidIdentity


@dataclass(frozen=True)
class HostedSettings:
    origin: str = "http://127.0.0.1:8020"
    tenant: str = "company"
    google_client_id: str = ""
    workspace_domains: tuple[str, ...] = ()
    public_faq_guests: bool = True
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


def create_hosted_app(settings=None, hosted=None, store=None, verifier=None, providers=None):
    settings, hosted = settings or from_env(), hosted or HostedSettings.from_env()
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

    @app.middleware("http")
    async def boundary(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD"} and origin and origin != hosted.origin:
            return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' https://accounts.google.com/gsi/client; "
                "connect-src 'self' https://accounts.google.com; frame-src https://accounts.google.com; "
                "style-src 'self' https://accounts.google.com/gsi/style; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        if hosted.origin.startswith("https:"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(ValueError)
    async def invalid_request(request, error):
        emit("hosted.request.invalid", error_type=type(error).__name__)
        return JSONResponse({"detail": "Request or provider configuration is invalid; contact the operator"}, status_code=400)

    @app.exception_handler(ProviderOperationError)
    async def provider_unavailable(request, error):
        return JSONResponse({"detail": "Search provider unavailable; no unreranked answer returned"}, status_code=502)

    @app.exception_handler(ScheduleSnapshotChanged)
    async def schedule_changed(request, error):
        return JSONResponse({"detail": "Schedule sources or filters changed; restart the search"}, status_code=409)

    @app.exception_handler(psycopg.OperationalError)
    async def storage_unavailable(request, error):
        return JSONResponse({"detail": "Storage is temporarily unavailable"}, status_code=503)

    def identity(request: Request):
        # Limit unverified requests before Google certificate retrieval too.
        rate("ip:" + (request.client.host if request.client else "unknown"), 120)
        value = request.headers.get("authorization")
        if not value:
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
                "public_faqs": hosted.public_faq_guests}

    @app.get("/api/documents")
    def documents(scoped=Depends(access)):
        rows = scoped.visible_documents()
        return {"documents": rows[:100], "has_more": len(rows) > 100,
                "signed_in": scoped.principal.subject is not None}

    @app.get("/api/source/{version_id}")
    def source(version_id: UUID, scoped=Depends(access)):
        if not scoped.visible_version(str(version_id), download=True):
            raise HTTPException(404, "Source unavailable")
        item = scoped.inspect_version(hosted.tenant, str(version_id))
        if not item:
            raise HTTPException(404, "Source unavailable")
        path = Path(item["version"]["original_path"])
        if not path.is_file() or checksum(path) != item["version"]["source_sha256"]:
            raise HTTPException(409, "Source integrity check failed")
        return FileResponse(path, filename=item["version"]["source_file"])

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request, scoped=Depends(access)):
        rate("chat:" + (scoped.principal.subject or "guest:" + request.client.host), hosted.chat_per_user_minute)
        rate("chat:global", hosted.chat_global_minute)
        rate("chat:global", hosted.chat_global_day, seconds=86400)
        if not chat_slots.acquire(blocking=False):
            raise HTTPException(503, "Search is busy; retry shortly", headers={"Retry-After": "3"})
        try:
            plan = plan_query(body.question)
            document = str(body.document_id) if body.document_id else None
            schedules = None
            if plan.route in {"schedules", "mixed"}:
                schedules = search_schedules(scoped, hosted.tenant, ScheduleQuery(question=body.question, limit=100), document)
                for row in schedules["results"]:
                    if not scoped.visible_version(str(row["version_id"]), download=True):
                        row["source_url"] = None
                if plan.route == "schedules" or schedules["status"] == "clarification_needed":
                    # Schedule records never enter a generated answer as prose.
                    return schedules
            if body.generate_answer:
                require_answer_permission(True)  # Organization policy, not a browser reviewer flag.
            with backend() as (provider, reranker, vectors):
                report = retrieve(scoped, vectors, provider, reranker, settings, hosted.tenant,
                    body.question, RetrievalOptions(document_id=document, min_rerank_score=hosted.min_rerank_score))
            answer = generate_answer(body.question, report,
                lambda part: scoped.is_source_current_approved(hosted.tenant, part["version_id"], part["chunk_id"])) if body.generate_answer else None
            # A permission/source change while the model is running must also
            # remove old retrieval extracts, not merely suppress generated prose.
            visible = [item for item in report["results"] if item.get("context_parts") and all(
                scoped.is_source_current_approved(hosted.tenant, part["version_id"], part["chunk_id"])
                for part in item["context_parts"])]
            if len(visible) != len(report["results"]):
                report["results"], report["status"] = visible, "evidence_changed"
                answer = {"status": "evidence_changed", "text": "Source access changed. Please search again.",
                          "claims": [], "citations": []} if answer is not None else None
            if schedules and any(not scoped.visible_version(str(s["version_id"])) for s in schedules.get("sources", [])):
                schedules = {"status": "evidence_changed", "results": [], "sources": [], "total": 0,
                             "message": "Schedule access changed. Please search again."}
            # No before/after raw candidate dump or unapproved-source metadata is
            # exposed on the public endpoint. Offline operator evaluation keeps it.
            return {"route": plan.route, "status": report["status"], "answer": answer,
                    "results": report["results"], "schedule_search": schedules,
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
        except Exception as error:
            emit("hosted.upload.failed", version_id=version, error_type=type(error).__name__)
        finally:
            upload_slots.release()

    @app.post("/api/upload")
    def upload(background: BackgroundTasks, file: UploadFile = File(...),
               schedule_kind: str | None = Form(default=None), document_id: UUID | None = Form(default=None), scoped=Depends(access)):
        if not scoped.principal.subject:
            raise HTTPException(401, "Sign in to upload an owner-only document")
        if document_id and not scoped.owned_document(str(document_id)):
            raise HTTPException(404, "Private document unavailable")
        rate("upload:" + scoped.principal.subject, 3)
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
        if not upload_slots.acquire(blocking=False):
            raise HTTPException(503, "Upload queue is busy; retry shortly")
        try:
            with store.connect() as db:
                row = db.execute("SELECT id FROM ingestion_jobs WHERE tenant_id=%s AND version_id=%s",
                                 (hosted.tenant, version_id)).fetchone()
            if not row:
                raise HTTPException(404, "Document job unavailable")
            background.add_task(ingest_private, str(row["id"]), str(version_id))
            return {"status": "queued", "version_id": str(version_id)}
        except BaseException:
            upload_slots.release()
            raise

    return app


def main():
    import uvicorn
    settings = from_env()
    app = create_hosted_app(settings)
    # Use a TLS reverse proxy. Never change the local admin's loopback boundary.
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("GSME_HOSTED_PORT", "8020")),
                proxy_headers=True, forwarded_allow_ips=os.getenv("GSME_TRUSTED_PROXY_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
