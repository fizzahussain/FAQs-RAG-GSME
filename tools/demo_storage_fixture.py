"""Opt-in local-only synthetic smoke test; never approves real source files."""

from pathlib import Path
import json
import sys
from uuid import uuid4

from gsme_rag.storage.config import from_env
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import FakeEmbeddings
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.storage.pipeline import process_ingestion, process_index, search_evidence, reconcile


def main():
    root = Path(__file__).resolve().parents[1]
    settings = from_env()
    store = EvidenceStore(settings.database_url)
    store.migrate(root)
    tenant = f"synthetic-{uuid4().hex[:10]}"
    source = root / "examples" / "synthetic_reviewed_faq.docx"
    submitted = store.submit(tenant, source, settings.data_dir)
    ingested = process_ingestion(store, settings, submitted["job_id"])
    if ingested["state"] != "review_needed":
        raise RuntimeError(ingested)
    with store.connect() as db:
        chunks = db.execute(
            """SELECT source_chunk_id FROM evidence_chunks WHERE version_id=%s
                              AND evidence_status='pending'""",
            (submitted["version_id"],),
        ).fetchall()
    if len(chunks) != 1:
        raise RuntimeError("Fixture should contain exactly one FAQ chunk")
    source_chunk_id = chunks[0]["source_chunk_id"]
    store.record_review(
        tenant,
        submitted["version_id"],
        source_chunk_id,
        "approved",
        "synthetic-fixture",
        "Programmatically generated fixture, reviewed against its generated text",
        True,
    )
    provider = FakeEmbeddings(settings.dimension)
    store.publish(
        tenant,
        submitted["document_id"],
        submitted["version_id"],
        provider.model_version,
        settings.collection + "_synthetic",
    )
    vectors = QdrantEvidence(
        settings.qdrant_url, settings.collection + "_synthetic", settings.dimension
    )
    indexed = process_index(store, vectors, provider, settings, tenant)
    question = "What is the synthetic approval deadline?"
    hits = search_evidence(store, vectors, provider, settings, tenant, question)
    again = store.submit(tenant, source, settings.data_dir, submitted["document_id"])
    if (
        not hits
        or hits[0]["chunk_id"] != source_chunk_id
        or not again["no_op"]
        or provider.calls != 1
    ):
        raise RuntimeError("Synthetic milestone failed")
    store.record_review(
        tenant,
        submitted["version_id"],
        source_chunk_id,
        "rejected",
        "synthetic-fixture",
        "Synthetic revocation check",
        True,
    )
    revoked_hits = search_evidence(store, vectors, provider, settings, tenant, question)
    if revoked_hits:
        raise RuntimeError("Revoked synthetic evidence remained searchable")
    removed = reconcile(store, vectors, provider.model_version)
    print(
        json.dumps(
            {
                "source": "synthetic",
                "tenant_id": tenant,
                "ingestion": ingested,
                "index": indexed,
                "rerun_no_op": again["no_op"],
                "hits": hits,
                "embedding_calls": provider.calls,
                "revoked_search_hits": len(revoked_hits),
                "stale_points_removed": removed,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
