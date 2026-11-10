"""Source-grounded local cases; live calls require --live --confirm-external.

Real sources, questions, rendered pages and results belong in ignored validation/.
This is an agent-checked regression set, never a claimed human-approved benchmark.
"""

from argparse import ArgumentParser
from collections import Counter
import json
import os
from pathlib import Path
import re
import unicodedata
import time

from docx import Document
import pymupdf as fitz

from gsme_rag.chunking import chunk_markdown
from gsme_rag.models import DocumentMetadata, ParsedDocument, ParseWarning
from gsme_rag.storage.acceptance import clean_text_eligible
from gsme_rag.storage.config import from_env
from gsme_rag.storage.files import checksum, verified_markdown
from gsme_rag.storage.pipeline import process_ingestion, process_index
from gsme_rag.storage.postgres import EvidenceStore, _page_warning_codes
from gsme_rag.storage.providers import CohereEmbeddings
from gsme_rag.storage.vectors import QdrantEvidence
from gsme_rag.parsing.profile import source_profile
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.reranking import CohereReranker
from gsme_rag.version import PARSER_VERSION


def comparable(text):
    """Normalize only a comparison copy, never the stored evidence."""
    text = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[*_#`]", "", text)).strip()


def source_text(path, page=None):
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as pdf:
            return pdf[page - 1].get_text() if page else "\n".join(p.get_text() for p in pdf)
    doc = Document(path)
    return "\n".join([p.text for p in doc.paragraphs] + [cell.text for table in doc.tables for row in table.rows for cell in row.cells])


def load_audit(path, audit_root):
    directory = audit_root / (path.stem + "_" + path.suffix[1:])
    raw = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    metadata = DocumentMetadata(**raw["metadata"])
    if metadata.content_sha256 != checksum(path) or metadata.parser_version != PARSER_VERSION:
        raise ValueError("Audit is stale or belongs to a different source; rerun tools/audit_sources.py")
    if source_profile(path, "native") != raw["environment"]:
        raise ValueError("Audit dependency profile changed; rerun the native source audit")
    md = verified_markdown(directory / "normalized.md", metadata.normalized_sha256).decode("utf-8")
    parsed = ParsedDocument(path.name, metadata.document_type, raw["strategy_used"], md,
                            [ParseWarning(**w) for w in raw["warnings"]], metadata)
    chunks = chunk_markdown(md, path.name, metadata=metadata).chunks
    return parsed, chunks, directory


def expected_chunks(case, chunks):
    return [chunk for chunk in chunks if (not case.get("page") or
        chunk.source_page_start is not None and chunk.source_page_start <= case["page"] <= (chunk.source_page_end or chunk.source_page_start))
        and comparable(case["anchor"]) in comparable(chunk.text)]


def inspect_cases(catalog, audits):
    rows = []
    for case in catalog["cases"]:
        if case["kind"] == "no_answer":
            rows.append({**case, "source_check": "out_of_scope_question", "expected_chunks": []})
            continue
        path, parsed, chunks, directory = audits[case["source"]]
        if comparable(case["anchor"]) not in comparable(source_text(path, case.get("page"))):
            raise ValueError("Case anchor is absent from original source: " + case["id"])
        matched = expected_chunks(case, chunks)
        eligible = [c for c in matched if clean_text_eligible(c.text, c.chunk_type, c.review_required,
                                                              c.evidence_status, _page_warning_codes(parsed, c))]
        if case["kind"] == "source_gap":
            blank = any(comparable(case["anchor"]) in comparable(row.cells[0].text) and not row.cells[1].text.strip()
                        for table in Document(path).tables for row in table.rows if len(row.cells) >= 2)
            if not blank:
                raise ValueError("The expected blank original answer is no longer blank")
            status = "confirmed_blank_source_answer" if not matched else "unexpected_export_for_blank_answer"
        elif case["kind"] == "hold":
            status = "held_as_required" if not eligible else "unsafe_automatic_acceptance"
        else:
            status = "ready_for_retrieval" if eligible else "blocked_by_extraction_policy" if matched else "missing_from_normalized_evidence"
        rows.append({**case, "source_check": status, "source_file": path.name,
                     "source_sha256": parsed.metadata.content_sha256,
                     "strategy_used": parsed.strategy_used,
                     "normalized_sha256": parsed.metadata.normalized_sha256,
                     "expected_chunks": [c.chunk_id for c in matched],
                     "eligible_chunks": [c.chunk_id for c in eligible],
                     "rendered_source": str(directory / f"page-{case['page']}.png") if case.get("page") else None})
    return rows


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("validation/product-evaluation/cases.json"))
    parser.add_argument("--audit-root", type=Path, default=Path("validation/product-evaluation/source-audit"))
    parser.add_argument("--output", type=Path, default=Path("validation/product-evaluation"))
    parser.add_argument("--tenant", default="local-test")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-external", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse already completed questions from the same source-pinned report")
    parser.add_argument("--question-interval", type=float, default=7,
                        help="Minimum seconds between questions; default accommodates 10/min trial rerank quota")
    args = parser.parse_args()
    if args.live and not args.confirm_external:
        parser.error("--confirm-external is required for real source text/query calls")
    os.environ["GSME_PDF_BACKEND"] = "native"
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    audits = {key: (Path(path), *load_audit(Path(path), args.audit_root)) for key, path in catalog["sources"].items()}
    rows = inspect_cases(catalog, audits)
    args.output.mkdir(parents=True, exist_ok=True)
    sources = {key: {"source_file": path.name, "source_sha256": parsed.metadata.content_sha256,
                    "strategy_used": parsed.strategy_used, "chunks": len(chunks)}
               for key, (path, parsed, chunks, directory) in audits.items()}
    report = {"status": catalog["status"], "sources": sources, "cases": rows,
              "live": args.live, "note": "Source-checked draft, not human benchmark signoff or calibrated answerability."}
    if args.resume and (args.output / "report.json").is_file():
        previous = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        if not previous.get("live") or any(previous["sources"][key]["source_sha256"] != value["source_sha256"] for key, value in sources.items()):
            raise ValueError("Cannot resume results for different source files")
        old_cases = {row["id"]: row for row in previous["cases"]}
        for row in rows:
            old = old_cases.get(row["id"], {})
            if old.get("live"):
                if any(old.get(key) != row.get(key) for key in ("question", "anchor", "page", "kind", "source_sha256")):
                    raise ValueError("Cannot reuse a case whose question or source expectation changed")
                row["live"] = old["live"]
                row["expected_evidence"] = old.get("expected_evidence", [])
    (args.output / "source-checks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.live:
        settings = from_env()
        store = EvidenceStore(settings.database_url)
        store.migrate()
        provider = CohereEmbeddings(settings.dimension)
        reranker = CohereReranker(settings.rerank_model, settings.rerank_timeout, settings.rerank_input_budget)
        vectors = QdrantEvidence(settings.qdrant_url, settings.collection, settings.dimension, hybrid=True)
        try:
            existing = store.list_versions(args.tenant, 100)
            for key, (path, parsed, chunks, directory) in audits.items():
                old = next((v for v in existing if v["source_file"] == path.name), None)
                submitted = store.submit(args.tenant, path, settings.data_dir, str(old["document_id"]) if old else None)
                if not submitted["no_op"]:
                    ingested = process_ingestion(store, settings, submitted["job_id"])
                    if ingested["state"] != "review_needed":
                        raise ValueError("Source ingestion did not complete: " + key)
                accepted = store.accept_clean_text(args.tenant, submitted["version_id"])
                sources[key].update(version_id=submitted["version_id"], **accepted)
                print(json.dumps({"source": path.name, "accepted": accepted["accepted"], "held": accepted["held"]}), flush=True)
                if accepted["accepted"]:
                    store.publish(args.tenant, submitted["document_id"], submitted["version_id"], provider.model_version, vectors.collection)
                    failure_batches = 0
                    for _ in range(200):
                        result = process_index(store, vectors, provider, settings, args.tenant, submitted["version_id"])
                        print(json.dumps({"stage": "index", "source": path.name, "indexed_in_batch": result["indexed"],
                                          "failed": result["failed"]}), flush=True)
                        failure_batches += bool(result["failed"])
                        report["indexing_sources"] = sources
                        (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
                        if failure_batches > 3:
                            raise ValueError("Indexing failed repeatedly; partial report retained. Inspect index jobs before evaluating")
                        states = store.index_status(args.tenant, submitted["version_id"])
                        remaining = sum(int(row["chunks"]) for row in states if row["model_version"] == provider.model_version
                                        and row["state"] in {"queued", "failed", "indexing"})
                        if not remaining:
                            break
                        if not result["indexed"]:
                            time.sleep(min(60, max(2, result.get("retry_after_seconds", 0))))
                        else:
                            time.sleep(1)
                    else:
                        raise ValueError("Indexing did not finish within the bounded run; partial report retained")
            for row in rows:
                if "live" in row:
                    if any(not store.is_source_current_approved(args.tenant, item["version_id"], item["chunk_id"])
                           for item in row["live"]["evidence"]):
                        raise ValueError("Prior evidence was revoked or superseded; cannot reuse this evaluation")
                    print(json.dumps({"case": row["id"], "status": "reused_completed_result"}), flush=True)
                    continue
                query_started = time.monotonic()
                if row.get("source_file"):
                    with store.connect() as db:
                        expected = db.execute("""SELECT version_id,source_chunk_id,source_sha256,evidence_text,
                            source_page_start,source_page_end FROM approved_current_evidence
                            WHERE tenant_id=%s AND source_file=%s AND source_sha256=%s""",
                            (args.tenant, row["source_file"], row["source_sha256"])).fetchall()
                    row["expected_evidence"] = [{"version_id": str(c["version_id"]), "chunk_id": c["source_chunk_id"],
                        "source_sha256": c["source_sha256"]} for c in expected
                        if comparable(row["anchor"]) in comparable(c["evidence_text"])
                        and (not row.get("page") or c["source_page_start"] is not None
                             and c["source_page_start"] <= row["page"] <= (c["source_page_end"] or c["source_page_start"]))]
                options = RetrievalOptions(candidate_count=30, top_k=5,
                    source_file=row.get("source_file") if row["kind"] != "no_answer" else None)
                try:
                    result = retrieve(store, vectors, provider, reranker, settings, args.tenant, row["question"], options)
                except Exception as error:
                    row["live_error"] = {"error_type": type(error).__name__}
                    (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
                    raise
                ranked = result["results"]
                matches = [i + 1 for i, item in enumerate(ranked)
                    if row.get("source_sha256") == item["source_sha256"] and row.get("anchor")
                    and any(comparable(row["anchor"]) in comparable(part["text"]) for part in item["context_parts"])
                    and (not row.get("page") or item["page_start"] <= row["page"] <= (item["page_end"] or item["page_start"]))]
                row["live"] = {"status": result["status"], "rerank_input_count": result["rerank_input_count"],
                               "model": result["reranker_model"], "matching_rank": min(matches) if matches else None,
                               "returned_count": len(ranked), "timings_ms": result["timings_ms"],
                               "evidence": [{k: item.get(k) for k in ("version_id", "chunk_id", "source_file", "source_sha256", "page_start", "page_end")} for item in ranked]}
                # Persist after each question so interrupted runs retain exact results.
                (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
                print(json.dumps({"case": row["id"], "matched_at": row["live"]["matching_rank"],
                                  "reranked": result["rerank_input_count"]}), flush=True)
                time.sleep(min(60, max(0, args.question_interval - (time.monotonic() - query_started))))
        finally:
            vectors.client.close()
    ready = [r for r in rows if r["source_check"] == "ready_for_retrieval"]
    report["summary"] = {"case_count": len(rows), "source_checks": dict(Counter(r["source_check"] for r in rows)),
        "ready_cases_with_top5_match": sum(bool(r.get("live", {}).get("matching_rank")) for r in ready) if args.live else None,
        "ready_positive_cases": len(ready),
        "negative_cases_returning_candidates": sum(bool(r.get("live", {}).get("returned_count")) for r in rows if r["kind"] == "no_answer") if args.live else None}
    (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "report": str(args.output / "report.json")}, indent=2))


if __name__ == "__main__":
    main()
