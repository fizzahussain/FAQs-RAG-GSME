"""Explicitly bounded real-source smoke, never a human-signed quality benchmark.

Reuses the source-pinned previous local report and existing index. Sends only
the two explicitly authorized originals' approved evidence. No document embed.
"""

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

import httpx

from gsme_rag.generation.answers import generate_answer, require_answer_permission
from gsme_rag.retrieval import retrieve, RetrievalOptions
from gsme_rag.retrieval.service import evidence_record
from gsme_rag.retrieval.reranking import CohereReranker
from gsme_rag.storage.config import from_env
from gsme_rag.storage.files import checksum
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import CohereEmbeddings
from gsme_rag.storage.providers import ProviderOperationError
from gsme_rag.storage.vectors import QdrantEvidence


class EvaluationStop(BaseException):
    """Do not let provider retry/fallback wrappers swallow a hard evaluation stop."""


def inspect_provider_response(response):
    """The transport hook receives an unread response, unlike ordinary clients."""
    response.read()
    try:
        return response.json()
    except ValueError:
        return {"non_json_error": True}


class AuthorizedStore:
    def __init__(self, store, sources):
        self.store, self.sources = store, sources

    def retrieval_scope(self, tenant, model, collection, document=None, category=None, filename=None):
        if filename and filename not in self.sources:
            return []
        return list(dict.fromkeys(point for name in ([filename] if filename else self.sources)
            for point in self.store.retrieval_scope(tenant, model, collection, document, category, name)))

    def allowed_hits(self, *args):
        return {point: row for point, row in self.store.allowed_hits(*args).items()
            if row["source_file"] in self.sources
            and row["source_sha256"] == self.sources[row["source_file"]]["source_sha256"]
            and str(row["version_id"]) == self.sources[row["source_file"]]["version_id"]}

    def retrieval_parts(self, *args):
        return self.store.retrieval_parts(*args)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-external", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retest-failed", action="store_true", help="Separately authorized eight answer-only retests; no retrieval calls")
    parser.add_argument("--final-two", action="store_true", help="Separately authorized two passage-ID protocol checks")
    parser.add_argument("--output", type=Path, default=Path("validation/answer-evaluation"))
    args = parser.parse_args()
    if not (args.live and args.confirm_external):
        parser.error("Both --live and --confirm-external are required for this authorized external smoke")
    previous = json.loads(Path("validation/product-evaluation/report.json").read_text(encoding="utf-8"))
    catalog = json.loads(Path("validation/product-evaluation/cases.json").read_text(encoding="utf-8"))
    authorized = {name: dict(next(s for s in previous["sources"].values() if s["source_file"] == name))
                  for name in ("FAQs QA Format.docx", "MFG-GSME-0002.pdf")}
    paths = {Path(path).name: Path(path) for path in catalog["sources"].values()}
    for name, source in authorized.items():
        if checksum(paths[name]) != source["source_sha256"]:
            raise ValueError("Authorized original changed; obtain a new source review before external calls")
        # JSON report entries are trusted only after checking the local source.
        source["version_id"] = str(source["version_id"])
    settings = from_env()
    store = EvidenceStore(settings.database_url)
    rows = previous["cases"]
    selected = ([r for r in rows if r["source_check"] == "ready_for_retrieval" and r["source_file"] == "FAQs QA Format.docx"][:6]
        + [r for r in rows if r["id"] in {"manual-mttf", "manual-tsv", "manual-uhast", "manual-system-in-package"}]
        + [r for r in rows if r["kind"] == "no_answer"][:2])
    if len(selected) != 12:
        raise ValueError("The authorized 12-case set is unavailable; do not substitute unrelated questions")
    for row in selected:
        if row["kind"] != "no_answer" and (not row.get("expected_evidence") or any(
                not store.is_source_current_approved("local-test", item["version_id"], item["chunk_id"])
                for item in row["expected_evidence"])):
            raise ValueError("Expected evidence changed or was revoked; stop before sending any question")
    # Process-only permission follows the explicit approval in this conversation;
    # it does not alter the user's private .env or authorize other workflows.
    os.environ["GSME_ANSWER_CONTENT_APPROVED"] = "yes"
    os.environ["GSME_ANSWER_PROVIDER"] = "cohere"
    require_answer_permission(True)
    provider = CohereEmbeddings(settings.dimension)
    reranker = CohereReranker(settings.rerank_model, settings.rerank_timeout, settings.rerank_input_budget)
    vectors = QdrantEvidence(settings.qdrant_url, settings.collection, settings.dimension, hybrid=True)
    scoped = AuthorizedStore(store, authorized)
    report = {"source": "agent_source_checked_draft_not_human_benchmark", "sources": authorized,
              "limits": {"questions": 12, "retrieval_calls": 24, "generation_calls": 12, "cohere_http_attempts": 132},
              "counts": {"cohere_http_attempts": 0}, "cases": [], "status": "running"}
    args.output.mkdir(parents=True, exist_ok=True)
    if args.resume and (args.output / "report.json").exists():
        old = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        if old["sources"] != authorized:
            raise ValueError("Cannot resume changed source versions")
        report["counts"] = old["counts"]
        report["cases"] = old["cases"]
        report["provider_diagnostics"] = old.get("provider_diagnostics", [])
        report["generation_metadata"] = old.get("generation_metadata", [])
    elif (args.output / "report.json").exists():
        raise ValueError("Use --resume to retain the aggregate authorized request budget")

    def save():
        (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    original_send = httpx.Client._send_single_request

    def bounded_send(client, request):
        host = request.url.host
        if host not in {"127.0.0.1", "localhost", "api.cohere.com", "api.cohere.ai"}:
            raise EvaluationStop("Unexpected HTTP destination; no content sent")
        if host in {"api.cohere.com", "api.cohere.ai"}:
            if report["counts"]["cohere_http_attempts"] >= 132:
                raise EvaluationStop("Authorized HTTP attempt limit reached")
            report["counts"]["cohere_http_attempts"] += 1
            endpoint = request.url.path.rsplit("/", 1)[-1]
            maximum = ({"chat": 22 if args.final_two else 20} if args.retest_failed else {"chat": 12, "rerank": 24, "embed": 24, "tokenize": 72}).get(endpoint)
            if maximum is None:
                raise EvaluationStop("Unapproved Cohere endpoint")
            if report["counts"].get(endpoint, 0) >= maximum:
                raise EvaluationStop("Authorized endpoint request limit reached")
            report["counts"][endpoint] = report["counts"].get(endpoint, 0) + 1
            save()
        response = original_send(client, request)
        # This hook runs below Client.send's normal response consumption. Read
        # before inspecting JSON; otherwise ResponseNotRead masquerades as a
        # provider failure and prevents the SDK seeing a successful response.
        if host in {"api.cohere.com", "api.cohere.ai"} and (response.status_code >= 400 or request.url.path.endswith("/chat")):
            inspect_provider_response(response)
        if host in {"api.cohere.com", "api.cohere.ai"} and request.url.path.endswith("/chat") and response.status_code == 200:
            raw = response.json()
            report.setdefault("generation_metadata", []).append({"finish_reason": raw.get("finish_reason"), "usage": raw.get("usage")})
            save()
        if host in {"api.cohere.com", "api.cohere.ai"} and response.status_code >= 400:
            # Keep raw provider diagnostics private, never in console logs/Git.
            try:
                body = response.json()
            except ValueError:
                body = {"non_json_error": True}
            errors = report.setdefault("provider_diagnostics", [])
            errors.append({"endpoint": request.url.path.rsplit("/", 1)[-1], "status": response.status_code, "body": body})
            save()
        if host in {"api.cohere.com", "api.cohere.ai"} and response.status_code == 429:
            raise EvaluationStop("Cohere quota/rate limit; run stopped without automatic repetition")
        return response

    try:
        with patch.object(httpx.Client, "_send_single_request", bounded_send):
            if args.retest_failed:
                marker = "final_two_attempted" if args.final_two else "answer_retest_attempted"
                failed = [row for row in report["cases"] if row.get("answer", {}).get("status") in
                    {"answer_validation_failed", "provider_unavailable"} and not row.get(marker)]
                if len(failed) > (2 if args.final_two else 8):
                    raise EvaluationStop("More than eight answer retests would exceed the additional approval")
                report["additional_authorization"] = {"answer_only_retests": 10 if args.final_two else 8, "command_total_ceiling": 22 if args.final_two else 20,
                                                       "overall_http_ceiling": 132, "new_retrieval_calls": 0}
                for row in failed:
                    case_started = time.monotonic()
                    groups = {}
                    with store.connect() as db:
                        for version, chunk in row["retrieval"]["hybrid"]["evidence_ids"]:
                            source = db.execute("""SELECT * FROM approved_current_evidence WHERE tenant_id=%s
                                AND version_id=%s AND source_chunk_id=%s""", ("local-test", version, chunk)).fetchone()
                            if (not source or source["source_file"] not in authorized
                                    or source["source_sha256"] != authorized[source["source_file"]]["source_sha256"]):
                                raise EvaluationStop("Previously returned source changed; no rehydrated text sent")
                            record = evidence_record(source)
                            groups.setdefault((version, record["unit_id"] or chunk), []).append(record)
                    context = {"synthetic": False, "reranking_status": "completed", "results": [{
                        "context_parts": sorted(parts, key=lambda p: p["part_index"]),
                        "context_complete": len(parts) == max(p["part_count"] for p in parts)} for parts in groups.values()]}
                    row[marker] = True
                    row.setdefault("answer_history", []).append(row["answer"])
                    save()
                    def audit_draft(value):
                        (args.output / (row["id"] + "-draft.json")).write_text(json.dumps(value, indent=2), encoding="utf-8")
                    row["answer"] = generate_answer(row["question"], context,
                        lambda p: store.is_source_current_approved("local-test", p["version_id"], p["chunk_id"]), audit=audit_draft)
                    save()
                    print(json.dumps({"case": row["id"], "answer_retest": row["answer"]["status"],
                        "http_attempts": report["counts"]["cohere_http_attempts"]}), flush=True)
                    time.sleep(max(0, 7 - (time.monotonic() - case_started)))
                report["status"] = "completed"
                save()
                return
            for case in selected:
                row = next((item for item in report["cases"] if item["id"] == case["id"]), None)
                if row is None:
                    row = {"id": case["id"], "kind": case["kind"], "question": case["question"], "retrieval": {}}
                    report["cases"].append(row)
                elif row["question"] != case["question"]:
                    raise ValueError("Cannot resume a changed question")
                for strategy in ("dense", "hybrid"):
                    if strategy in row["retrieval"] and (strategy == "dense" or "answer" in row):
                        continue
                    started = time.monotonic()
                    result = retrieve(scoped, vectors, provider, reranker, settings, "local-test", case["question"],
                                      RetrievalOptions(strategy=strategy))
                    expected = {(item["version_id"], item["chunk_id"]) for item in case.get("expected_evidence", [])}
                    before = [(item["version_id"], item["chunk_id"]) for item in result["initial_candidates"]]
                    final = [(part["version_id"], part["chunk_id"]) for item in result["results"] for part in item["context_parts"]]
                    rank = next((i for i, item in enumerate(result["results"], 1) if any(
                        (p["version_id"], p["chunk_id"]) in expected for p in item["context_parts"])), None)
                    failure = (None if rank or not expected else "candidate_miss" if not expected.intersection(before)
                               else "context_or_final_ranking_miss")
                    row["retrieval"][strategy] = {"matching_rank": rank, "rerank_input_count": result["rerank_input_count"],
                        "evidence_ids": final, "timings_ms": result["timings_ms"], "failure_category": failure}
                    if strategy == "hybrid":
                        row["answer"] = generate_answer(case["question"], result,
                            lambda p: store.is_source_current_approved("local-test", p["version_id"], p["chunk_id"]))
                        row["answer"]["expected_anchor_in_support"] = bool(case.get("anchor")) and any(
                            case["anchor"].casefold() in support["quote"].casefold()
                            for claim in row["answer"]["claims"] for support in claim["supports"])
                    save()
                    print(json.dumps({"case": case["id"], "strategy": strategy, "rank": rank,
                        "answer_status": row.get("answer", {}).get("status"), "http_attempts": report["counts"]["cohere_http_attempts"]}), flush=True)
                    if row.get("answer", {}).get("status") == "answer_validation_failed":
                        raise EvaluationStop("Answer validation failed; diagnose before consuming more quota")
                    time.sleep(max(0, 7 - (time.monotonic() - started)))
        report["status"] = "completed"
    except EvaluationStop as error:
        report.update(status="stopped_at_boundary", stop_reason=str(error))
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, ProviderOperationError):
            report["provider_error"] = str(error)
    finally:
        vectors.client.close()
        save()
    print(json.dumps({"status": report["status"], "counts": report["counts"], "report": str(args.output / "report.json")}))
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
