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
from pathlib import Path
import tempfile
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from gsme_rag.storage.config import Settings, from_env
from gsme_rag.storage.pipeline import process_ingestion, process_index, reconcile, search_evidence
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import CohereEmbeddings, FakeEmbeddings, ProviderOperationError
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.storage.admin_ui import PAGE


MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".pdf", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


class JobRequest(BaseModel):
    tenant: str = Field(min_length=1)
    job_id: str


class ReviewRequest(BaseModel):
    tenant: str = Field(min_length=1)
    version_id: str
    chunk_id: str
    decision: Literal["approved", "rejected"]
    reviewer: str
    rationale: str
    source_verified: bool = False


class VersionRequest(BaseModel):
    tenant: str = Field(min_length=1)
    document_id: str | None = None
    version_id: str
    provider: Literal["fake", "cohere"] = "cohere"
    confirm_external: bool = False


class SearchRequest(BaseModel):
    tenant: str = Field(min_length=1)
    question: str = Field(min_length=1)
    provider: Literal["fake", "cohere"] = "cohere"
    limit: int = Field(default=5, ge=1, le=20)
    hybrid: bool = False


def _provider(name: str, settings: Settings):
    return FakeEmbeddings(settings.dimension) if name == "fake" else CohereEmbeddings(settings.dimension)


def _vectors(name: str, settings: Settings):
    return QdrantEvidence(
        settings.qdrant_url,
        settings.collection + ("_synthetic" if name == "fake" else ""),
        settings.dimension,
        settings.hybrid_enabled,
    )


def _safe(action):
    try:
        return action()
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

    app = FastAPI(title="GSME Local Evidence Console", version="0.15.4")

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
        if not x_gsme_token or not hmac.compare_digest(x_gsme_token, secret):
            raise HTTPException(401, "Local operator token required")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return PAGE

    @app.get("/api/health", dependencies=[Depends(authorize)])
    def health():
        return {"status": "ok", "dimension": settings.dimension,
                "collection": settings.collection, "hybrid_enabled": settings.hybrid_enabled,
                "cohere_configured": bool(os.getenv("COHERE_API_KEY")) and
                os.getenv("GSME_EXTERNAL_CONTENT_APPROVED") == "yes"}

    @app.get("/api/versions", dependencies=[Depends(authorize)])
    def versions(tenant: str, limit: int = 50):
        return _safe(lambda: store.list_versions(tenant, limit))

    @app.get("/api/status/{job_id}", dependencies=[Depends(authorize)])
    def status(job_id: str, tenant: str):
        value = store.job_status(tenant, job_id)
        if not value:
            raise HTTPException(404, "Job not found for tenant")
        return value

    @app.get("/api/inspect/{version_id}", dependencies=[Depends(authorize)])
    def inspect(version_id: str, tenant: str, limit: int = 100):
        value = _safe(lambda: store.inspect_version(tenant, version_id, limit))
        if not value:
            raise HTTPException(404, "Version not found for tenant")
        value["index_jobs"] = store.index_status(tenant, version_id)
        return value

    @app.get("/api/markdown/{version_id}", dependencies=[Depends(authorize)])
    def markdown(version_id: str, tenant: str):
        value = store.inspect_version(tenant, version_id, 1)
        if not value or not value["version"]["artifact_path"]:
            raise HTTPException(404, "Normalized Markdown unavailable")
        path = Path(value["version"]["artifact_path"])
        if not path.is_file():
            raise HTTPException(404, "Normalized Markdown unavailable")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/manifest/{version_id}", dependencies=[Depends(authorize)])
    def manifest(version_id: str, tenant: str):
        value = store.inspect_version(tenant, version_id, 1)
        if not value or not value["review_manifest_path"]:
            raise HTTPException(404, "Review manifest unavailable")
        path = Path(value["review_manifest_path"])
        if not path.is_file():
            raise HTTPException(404, "Review manifest unavailable")
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/source/{version_id}", dependencies=[Depends(authorize)])
    def source(version_id: str, tenant: str):
        value = store.inspect_version(tenant, version_id, 1)
        if not value:
            raise HTTPException(404, "Version not found for tenant")
        path = Path(value["version"]["original_path"])
        if not path.is_file():
            raise HTTPException(404, "Original source unavailable")
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

    @app.post("/api/review", dependencies=[Depends(authorize)])
    def review(body: ReviewRequest):
        reviewers = {item.strip() for item in os.getenv("GSME_AUTHORIZED_REVIEWERS", "").split(",")}
        if body.reviewer not in reviewers or not body.reviewer:
            raise HTTPException(403, "Reviewer is not on the local authorized list")
        _safe(lambda: store.record_review(body.tenant, body.version_id, body.chunk_id,
                                          body.decision, body.reviewer, body.rationale,
                                          body.source_verified))
        return {"recorded": True}

    @app.post("/api/publish", dependencies=[Depends(authorize)])
    def publish(body: VersionRequest):
        if not body.document_id:
            raise HTTPException(400, "Document ID required")
        provider = _safe(lambda: _provider(body.provider, settings))
        vectors = _vectors(body.provider, settings)
        count = _safe(lambda: store.publish(body.tenant, body.document_id, body.version_id,
                                            provider.model_version, vectors.collection))
        return {"approved_candidates": count, "model_version": provider.model_version,
                "collection": vectors.collection}

    @app.post("/api/index", dependencies=[Depends(authorize)])
    def index(body: VersionRequest):
        if body.provider == "cohere" and not body.confirm_external:
            raise HTTPException(400, "Confirm sending only approved evidence to Cohere")
        version = store.inspect_version(body.tenant, body.version_id, 1)
        if not version:
            raise HTTPException(404, "Version not found for tenant")
        provider = _safe(lambda: _provider(body.provider, settings))
        vectors = _vectors(body.provider, settings)
        result = process_index(store, vectors, provider, settings, body.tenant, body.version_id)
        return {"index": result, "reconciled": reconcile(store, vectors, provider.model_version),
                "model_version": provider.model_version, "collection": vectors.collection}

    @app.post("/api/search", dependencies=[Depends(authorize)])
    def search(body: SearchRequest):
        provider = _safe(lambda: _provider(body.provider, settings))
        vectors = _vectors(body.provider, settings)
        return _safe(lambda: search_evidence(store, vectors, provider, settings, body.tenant,
                                             body.question, body.limit, body.hybrid))

    @app.get("/api/embedding/{source_chunk_id}", dependencies=[Depends(authorize)])
    def embedding(source_chunk_id: str, tenant: str, version_id: str,
                  provider: Literal["fake", "cohere"] = "cohere"):
        provider_object = _safe(lambda: _provider(provider, settings))
        vectors = _vectors(provider, settings)
        point = store.approved_point(tenant, version_id, source_chunk_id,
                                     provider_object.model_version, vectors.collection)
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
                "model_version": provider_object.model_version, "collection": vectors.collection,
                "point_id": str(point["point_id"]), "dimension": len(values),
                "l2_norm": math.sqrt(sum(value * value for value in values)),
                "first_8_values": values[:8],
                "note": "Preview only; vector coordinates do not explain semantic meaning."}

    @app.post("/api/reconcile", dependencies=[Depends(authorize)])
    def cleanup(body: VersionRequest):
        provider = _safe(lambda: _provider(body.provider, settings))
        vectors = _vectors(body.provider, settings)
        return {"deleted": reconcile(store, vectors, provider.model_version)}

    return app


def main():
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(override=False)
    app = create_admin_app()
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("GSME_ADMIN_PORT", "8010")))


if __name__ == "__main__":
    main()
