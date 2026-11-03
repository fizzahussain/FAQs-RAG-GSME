"""Synthetic-only retrieval demo. Never reads or approves private document input.

Writes a replayable question set and before/after report under ignored validation/.
The Cohere option requires explicit consent and the configured permissions.
"""

from argparse import ArgumentParser
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from docx import Document

from gsme_rag.storage.config import from_env
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.pipeline import process_ingestion, process_index
from gsme_rag.storage.providers import CohereEmbeddings, FakeEmbeddings
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.reranking import CohereReranker, FakeReranker
from gsme_rag.retrieval.evaluation import evaluate_retrieval


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("fake", "cohere"), default="fake")
    parser.add_argument("--confirm-external", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("validation/retrieval-demo"))
    args = parser.parse_args()
    if args.provider == "cohere" and not args.confirm_external:
        parser.error("--confirm-external is required for the generated synthetic texts")
    settings = from_env()
    args.output.mkdir(parents=True, exist_ok=True)
    # A separate collection/workspace prevents the smoke from indexing private
    # pending jobs or reconciling unrelated production evidence.
    collection_base = "gsme_retrieval_demo_" + uuid4().hex[:12]
    collection = collection_base + ("_synthetic" if args.provider == "fake" else "")
    settings = replace(settings, collection=collection_base, data_dir=(args.output / "store").resolve())
    store = EvidenceStore(settings.database_url)
    store.migrate()
    provider = FakeEmbeddings(settings.dimension) if args.provider == "fake" else CohereEmbeddings(settings.dimension)
    reranker = FakeReranker() if args.provider == "fake" else CohereReranker(
        settings.rerank_model, settings.rerank_timeout, settings.rerank_input_budget)
    vectors = QdrantEvidence(settings.qdrant_url, collection, settings.dimension, hybrid=True)
    tenant = "synthetic-retrieval-" + uuid4().hex[:12]
    pairs = [
        ("How do I submit a synthetic shared-run design?", "Submit the synthetic shared-run design using the Example Submission Portal."),
        ("When is the synthetic approval deadline?", "The synthetic approval deadline is 15 October 2026."),
        ("What is the synthetic packaging contact?", "The synthetic packaging contact is packaging@example.invalid."),
    ]
    with TemporaryDirectory(prefix="gsme-retrieval-fixture-") as temporary:
        path = Path(temporary) / "synthetic_retrieval_faq.docx"
        document = Document()
        document.add_heading("Synthetic workflow fixture — not GSME facts", 1)
        for question, answer in pairs:
            document.add_heading(question, 2)
            document.add_paragraph(answer)
        document.save(path)
        submitted = store.submit(tenant, path, settings.data_dir)
    result = process_ingestion(store, settings, submitted["job_id"])
    if result["state"] != "review_needed":
        raise RuntimeError("Synthetic fixture ingestion failed")
    inspection = store.inspect_version(tenant, submitted["version_id"])
    if len(inspection["chunks"]) != len(pairs):
        raise RuntimeError("Synthetic source did not produce the expected FAQ units")
    cases = []
    for chunk, (question, answer) in zip(inspection["chunks"], pairs, strict=True):
        if chunk["faq_question"] != question or answer not in chunk["evidence_text"]:
            raise RuntimeError("Synthetic extraction differs from its generated source; not approving")
        store.record_review(tenant, submitted["version_id"], chunk["source_chunk_id"], "approved",
                            "synthetic-fixture", "Verified against the generated synthetic question/answer", True)
        cases.append({"tenant_id": tenant, "question": chunk["faq_question"], "expected_evidence": [{
            "version_id": str(submitted["version_id"]), "chunk_id": chunk["source_chunk_id"],
            "source_sha256": inspection["version"]["source_sha256"]}]})
    cases.append({"tenant_id": tenant, "question": "What is the cafeteria opening time?", "expect_no_answer": True})
    case_path = args.output / "synthetic-cases.json"
    case_path.write_text(json.dumps({"source": "synthetic", "cases": cases}, indent=2), encoding="utf-8")
    store.publish(tenant, submitted["document_id"], submitted["version_id"], provider.model_version, collection)
    indexed = process_index(store, vectors, provider, settings, tenant, submitted["version_id"])
    if indexed["indexed"] != len(pairs):
        raise RuntimeError("Synthetic indexing did not complete")
    reports = {}
    for strategy in ("dense", "hybrid"):
        reports[strategy] = evaluate_retrieval(store, vectors, provider, reranker, settings, case_path, strategy)
    sample = retrieve(store, vectors, provider, reranker, settings, tenant, cases[0]["question"], RetrievalOptions())
    report = {"source": "synthetic", "provider": args.provider, "collection": collection,
        "collection_base": collection_base,
        "tenant_id": tenant, "version_id": str(submitted["version_id"]), "index": indexed,
        "evaluations": reports, "sample": sample,
        "note": "Generated fixture only; not a representative GSME quality benchmark. Data is retained for replay."}
    (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"source": "synthetic", "provider": args.provider, "indexed": indexed["indexed"],
        "reranking_status": sample["reranking_status"], "reranker_model": reranker.model_version,
        "sample_result_count": len(sample["results"]),
        "evaluations": {key: {"questions": value["questions"], "metrics": value["metrics"],
            "negative_cases_with_results": value["negative_cases_with_results"],
            "wrong_version_or_revoked_returned": value["wrong_version_or_revoked_returned"],
            "latency_ms_p50": value["latency_ms_p50"]} for key, value in reports.items()},
        "tenant_id": tenant, "version_id": str(submitted["version_id"]), "collection_base": collection_base,
        "report_path": str(args.output / "report.json")}, indent=2))


if __name__ == "__main__":
    main()
