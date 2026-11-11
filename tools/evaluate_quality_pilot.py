"""Source-pinned 24-question pilot; no external calls without explicit flags.

Company originals, rendered pages, questions and model responses stay in ignored
validation/. This is an agent-source-checked pilot, not human quality approval.
The request ledger is written BEFORE sending, including unsuccessful attempts.
"""

from argparse import ArgumentParser
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

import httpx
import pymupdf

from gsme_rag.generation.answers import (
    AnswerProviderUnavailable, AnswerSettings, CommandAnswers, generate_answer,
    require_answer_permission,
)
from gsme_rag.generation.evaluation import summarize
from gsme_rag.retrieval import RetrievalOptions, retrieve
from gsme_rag.retrieval.benchmarking import ranking_trace, threshold_report
from gsme_rag.retrieval.reranking import CohereReranker
from gsme_rag.storage.config import from_env
from gsme_rag.storage.files import checksum
from gsme_rag.storage.postgres import EvidenceStore
from gsme_rag.storage.providers import CohereEmbeddings, ProviderOperationError
from gsme_rag.storage.vectors import QdrantEvidence
from evaluate_answer_increment import AuthorizedStore, EvaluationStop
from evaluate_gsme_sources import comparable, source_text


FAQ_TOPICS = (
    "legal agreements", "get a quote", "reschedule or cancel", "commercial companies",
    "minimum die area", "gds upload deadlines", "additional samples", "access a pdk",
    "what is a shared block",
)
SOURCE_NAMES = {"FAQs QA Format.docx", "MFG-GSME-0002.pdf"}
ANCHORS = {
    "legal agreements": "A Mutual Non-Disclosure Agreement (MNDA)",
    "get a quote": "Email the following details about your project to sales@musesemi.com to request a quote",
    "reschedule or cancel": "You may reschedule or cancel your project via email without charge prior to the Final GDS deadline",
    "commercial companies": "Yes! We have several commercial customers",
    "minimum die area": "reduces the minimum area to as small as 1mm2",
    "gds upload deadlines": "Our deadline for preliminary GDS is 14 days prior to tapeout",
    "additional samples": "up to six months after initial sample shipment",
    "access a pdk": "Yes, we support access to TSMC PDKs",
    "what is a shared block": "aggregates multiple customer projects to meet TSMC's minimum area requirement",
    "manual-MTTF": "Mean time to failure (MTTF): The average time to failure",
    "manual-TSV": "through-silicon via (TSV)",
    "manual-uHAST": "Evaluate the reliability of non-hermetic packages that are exposed to moist environments",
}
CLARIFIED_GDS = "How many days before tapeout are preliminary and final GDS uploads due?"


def private_output(path):
    if not path.resolve().is_relative_to((Path(__file__).resolve().parents[1] / "validation").resolve()):
        raise ValueError("Private evaluation artifacts must stay under ignored validation/")
    return path


def prepare(store, output):
    draft = json.loads(Path("validation/challenge-benchmark/cases.json").read_text(encoding="utf-8"))
    if set(draft["sources"]) != SOURCE_NAMES:
        raise ValueError("The approved source set must match exactly")
    with store.connect() as db:
        rows = db.execute("""SELECT c.*,v.original_path FROM approved_current_evidence c
            JOIN document_versions v ON v.id=c.version_id WHERE c.tenant_id=%s
            AND c.source_file=ANY(%s) ORDER BY c.chunk_index""", ("local-test", list(SOURCE_NAMES))).fetchall()
    sources, originals, by_id = {}, {}, {}
    for row in rows:
        name = row["source_file"]
        identity = dict(version_id=str(row["version_id"]), source_sha256=row["source_sha256"])
        if identity != draft["sources"][name]:
            raise ValueError("Approved source versions changed; regenerate and source-check the pilot")
        if name not in sources:
            path = Path(row["original_path"])
            if checksum(path) != row["source_sha256"]:
                raise ValueError("Original hash mismatch")
            sources[name] = {**identity, "original_path": str(path)}
            originals[name] = source_text(path)
        by_id[(str(row["version_id"]), row["source_chunk_id"])] = row
    if set(sources) != SOURCE_NAMES:
        raise ValueError("Both approved originals are required")
    if any("zx999" in text.casefold() for text in originals.values()):
        raise ValueError("Negative synthetic project identifier is present in the source")
    selected = []
    for index, topic in enumerate((*FAQ_TOPICS, "manual-MTTF", "manual-TSV", "manual-uHAST")):
        if topic.startswith("manual-"):
            group = topic
        else:
            faq = next(r for r in rows if r["source_file"] == "FAQs QA Format.docx"
                       and topic in (r["faq_question"] or "").casefold())
            group = faq["source_chunk_id"]
        pair = [c for c in draft["cases"] if c["pair_id"] == group and c["kind"] != "original_faq"]
        if len(pair) != 2 or sum(c["expect_no_answer"] for c in pair) != 1:
            raise ValueError("Each pilot topic must have one positive and one negative")
        for raw in pair:
            case = dict(raw, split="tuning" if index < 8 else "heldout",
                        kind="no_answer" if raw["expect_no_answer"] else "answer")
            checks = []
            for expected in case["expected_evidence"]:
                row = by_id[(expected["version_id"], expected["chunk_id"])]
                name, page = row["source_file"], row["source_page_start"]
                original = source_text(Path(sources[name]["original_path"]), page) if page else originals[name]
                anchor = ANCHORS[topic]
                if comparable(anchor) not in comparable(original) or comparable(anchor) not in comparable(row["evidence_text"]):
                    raise ValueError("Positive evidence anchor is not present in both original and approved passage")
                checks.append({"source_file": name, "page": page, "chunk_id": expected["chunk_id"], "anchor": anchor})
            # TSV is also explicitly defined in the glossary. Count either
            # verified passage as relevant; do not penalize a correct alternative.
            if topic == "manual-TSV" and case["kind"] == "answer":
                glossary = next(r for r in rows if r["source_file"] == "MFG-GSME-0002.pdf"
                    and r["evidence_text"].split("\n\n")[-1].startswith("Through-silicon via (TSV):"))
                case["expected_evidence"].append(dict(version_id=str(glossary["version_id"]),
                    chunk_id=glossary["source_chunk_id"], source_sha256=glossary["source_sha256"]))
                checks.append({"source_file": glossary["source_file"], "page": glossary["source_page_start"],
                    "chunk_id": glossary["source_chunk_id"], "anchor": "Through-silicon via (TSV):"})
            case.update(source_check="agent_text_checked_pending_visual_review", source_checks=checks,
                negative_scope_check=("Both original texts checked: no synthetic ZX999 project, private credentials, "
                    "named Muse customer payment history, NDA penalty, fixed tapeout price or late-cancellation refund is established. "
                    "Generic nearby process information is not an answer." if case["kind"] == "no_answer" else None))
            selected.append(case)
    output.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(sources["MFG-GSME-0002.pdf"]["original_path"]) as pdf:
        for page in (50, 107, 120):
            pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).save(output / f"source-page-{page}.png")
    manifest = {"source": "agent_source_checked_pilot_not_human_benchmark", "sources": sources,
        "cases": selected, "eligible_chunks": len(rows), "negative_project_marker_absent": True,
        "manual_rendered_pages": [50, 107, 120], "external_calls": 0}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest, rows


class RequestBudget:
    """Count below the SDK retry layer; persist before every external attempt."""

    def __init__(self, report, save):
        self.report, self.save = report, save

    def before(self, request):
        if request.url.host in {"127.0.0.1", "localhost"}:
            return False
        if request.url.scheme != "https" or request.url.host not in {"api.cohere.com", "api.cohere.ai"}:
            raise EvaluationStop("Unapproved HTTP destination; nothing sent")
        endpoint = request.url.path.rsplit("/", 1)[-1]
        counts = self.report["counts"]
        chat_limit = 27 if self.report.get("additional_authorization", {}).get("answer_only_retests") == 3 else 24
        limits = {"tokenize": 96, "embed": 48, "rerank": 48, "chat": chat_limit}
        if endpoint not in limits or counts["cohere_http_attempts"] >= 240 or counts.get(endpoint, 0) >= limits[endpoint]:
            raise EvaluationStop("Authorized request ceiling reached; nothing sent")
        body = json.loads(request.content)
        if endpoint == "embed" and (body.get("input_type") != "search_query" or len(body.get("texts", [])) != 1):
            raise EvaluationStop("Document re-embedding is not authorized")
        if endpoint == "rerank" and not 1 <= len(body.get("documents", [])) <= 30:
            raise EvaluationStop("Rerank passage ceiling exceeded")
        counts["cohere_http_attempts"] += 1
        counts[endpoint] = counts.get(endpoint, 0) + 1
        self.save()
        return True

    def after(self, request, response, external):
        if not external:
            return
        response.read()
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if response.status_code >= 400:
            self.report.setdefault("provider_diagnostics", []).append({"endpoint": endpoint, "status": response.status_code})
        if endpoint == "chat" and response.status_code == 200:
            raw = response.json()
            self.report.setdefault("generation_usage", []).append({"usage": raw.get("usage"), "finish_reason": raw.get("finish_reason")})
        self.save()
        if response.status_code == 429:
            raise EvaluationStop("Cohere rate/quota limit; stopped without retry")


class CachedQueries:
    """Dense and hybrid comparisons reuse the same vector, not new document vectors."""

    external = True

    def __init__(self, provider, questions):
        self.provider, self.questions = provider, set(questions)
        self.model_version = provider.model_version
        self.tokens, self.vectors = {}, {}

    def _check(self, text):
        if text not in self.questions:
            raise EvaluationStop("Unapproved query variant")

    def encode_tokens(self, text):
        self._check(text)
        if text not in self.tokens:
            self.tokens[text] = self.provider.encode_tokens(text)
        return self.tokens[text]

    def embed_query(self, text):
        self._check(text)
        if text not in self.vectors:
            self.vectors[text] = self.provider.embed_query(text)
        return self.vectors[text]

    def embed_documents(self, texts):
        raise EvaluationStop("Document re-embedding is not authorized")


class NoFallback:
    name, model = "disabled_for_pilot", "none"

    def generate(self, *args):
        raise AnswerProviderUnavailable("Local fallback excluded from Cohere-only pilot metrics")


class ScopedCommand(CommandAnswers):
    def __init__(self, settings, questions, texts):
        super().__init__(settings)
        self.questions, self.texts = set(questions), set(texts)

    def generate(self, question, evidence):
        if question not in self.questions or any(p["text"] not in self.texts for p in evidence):
            raise EvaluationStop("Unapproved generation question or evidence")
        return super().generate(question, evidence)


def reconcile_attempts(report):
    """An interrupted attempt consumes budget even if its result was not saved."""
    answer_limit = 27 if report.get("additional_authorization", {}).get("answer_only_retests") == 3 else 24
    for key, limit in (("searches", 48), ("answer_attempts", answer_limit), ("cohere_http_attempts", 240)):
        count = report["counts"].get(key)
        if type(count) is not int or not 0 <= count <= limit:
            raise ValueError("Invalid aggregate attempt ledger")
    completed = sum(len(c["retrieval"]) for c in report["cases"])
    failures = sum(len(c.get("retrieval_failures", {})) for c in report["cases"])
    if report["counts"]["searches"] - completed - failures == 1 and report["cases"]:
        last = report["cases"][-1]
        strategy = "hybrid" if "dense" in last["retrieval"] else "dense"
        if strategy in last.get("retrieval_failures", {}) or strategy in last["retrieval"]:
            raise ValueError("Cannot attribute the interrupted attempt safely")
        last.setdefault("retrieval_failures", {})[strategy] = {"error_type": report.get("error_type"),
            "reason": "Attempt failed before a usable ranking; original diagnostics did not identify the underlying cause"}
    elif report["counts"]["searches"] != completed + failures:
        raise ValueError("Attempt ledger cannot be reconciled; do not guess or repeat calls")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-external", action="store_true")
    parser.add_argument("--source-checked", action="store_true", help="Agent checked original FAQ and rendered manual pages before sending")
    parser.add_argument("--resume-unattempted", action="store_true", help="Keep the same aggregate budget; never repeat an attempted search or answer")
    parser.add_argument("--answer-only-retests", action="store_true", help="Requires the separately approved three answer-only checks; no new searches")
    parser.add_argument("--record-failures-and-continue", action="store_true", help="Record known blocked results; run only other unattempted cases, never retry a failure")
    parser.add_argument("--output", type=Path, default=Path("validation/quality-pilot-0.19.0"))
    args = parser.parse_args()
    output = private_output(args.output)
    prior = None
    if (output / "report.json").exists() and not args.resume_unattempted:
        raise ValueError("A run ledger already exists; do not reset the approved budget or silently rerun")
    if args.resume_unattempted:
        prior = json.loads((output / "report.json").read_text(encoding="utf-8"))
        if ((prior["status"].startswith("completed") and not args.answer_only_retests)
                or any(d["status"] == 429 for d in prior.get("provider_diagnostics", []))):
            raise ValueError("Completed or quota-stopped evaluation cannot be resumed under this approval")
    settings = from_env()
    store = EvidenceStore(settings.database_url)
    manifest, rows = prepare(store, output)
    if prior and (prior["sources"] != manifest["sources"] or prior["manifest_sha256"] != checksum(output / "manifest.json")):
        raise ValueError("Do not resume changed sources or questions")
    if args.answer_only_retests and (not prior or prior["counts"]["searches"] != 48
            or len(prior["cases"]) != 24 or prior["counts"]["answer_attempts"] < 24):
        raise ValueError("Finish the original pilot before the separately approved answer-only checks")
    if not args.live:
        print(json.dumps({"cases": len(manifest["cases"]), "eligible_chunks": len(rows), "external_calls": 0,
                          "manual_pages_to_inspect": manifest["manual_rendered_pages"]}))
        return
    if not (args.confirm_external and args.source_checked):
        parser.error("Live evaluation requires --confirm-external --source-checked after original-source inspection")
    os.environ.update(GSME_EXTERNAL_CONTENT_APPROVED="yes", GSME_RERANK_CONTENT_APPROVED="yes",
                      GSME_ANSWER_CONTENT_APPROVED="yes", GSME_ANSWER_PROVIDER="cohere")
    require_answer_permission(True)
    report = prior or {"source": manifest["source"], "sources": manifest["sources"], "cases": [], "status": "running",
        "manifest_sha256": checksum(output / "manifest.json"), "counts": {"cohere_http_attempts": 0, "searches": 0, "answer_attempts": 0},
        "limits": {"questions": 24, "searches": 48, "passages_per_rerank": 30, "command_answers": 24,
                   "cohere_http_attempts": 240, "document_embeddings": 0},
        "source_review": "Agent inspected original FAQ and rendered manual pages; not human approval",
        "threshold_applied": None, "local_fallback_enabled": False}
    if prior:
        report.setdefault("run_history", []).append({"status": prior["status"], "error_type": prior.get("error_type"),
            "elapsed_seconds": prior.get("elapsed_seconds"), "counts": dict(prior["counts"])})
        # The first ledger predates per-stage attempt markers. It stopped in
        # dense reranking after 26 completed searches; retain that failed attempt.
        reconcile_attempts(report)
    report.update(status="running", tool_sha256=checksum(Path(__file__)))
    if args.answer_only_retests:
        report["additional_authorization"] = {"answer_only_retests": 3, "command_total_ceiling": 27,
            "overall_http_ceiling": 240, "new_embedding_or_rerank_calls": 0}

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    save()
    budget = RequestBudget(report, save)
    send = httpx.Client._send_single_request

    def bounded_send(client, request):
        external = budget.before(request)
        try:
            response = send(client, request)
        except Exception as error:
            if external:
                report.setdefault("transport_failures", []).append({"endpoint": request.url.path.rsplit("/", 1)[-1],
                    "host": request.url.host, "error_type": type(error).__name__,
                    "cause_type": type(error.__cause__).__name__ if error.__cause__ else None,
                    "local_diagnostic": str(error)})
                save()
            raise
        budget.after(request, response, external)
        return response

    started = time.monotonic()
    try:
        with ExitStack() as resources:
            real = CohereEmbeddings(settings.dimension)
            resources.enter_context(real.client)
            reranker = CohereReranker(settings.rerank_model, settings.rerank_timeout, settings.rerank_input_budget)
            resources.enter_context(reranker.client)
            vectors = QdrantEvidence(settings.qdrant_url, settings.collection, settings.dimension, hybrid=True)
            resources.callback(vectors.client.close)
            provider = CachedQueries(real, [c["question"] for c in manifest["cases"]])
            command = ScopedCommand(AnswerSettings.from_env(), provider.questions | ({CLARIFIED_GDS} if args.answer_only_retests else set()),
                                    [r["evidence_text"] for r in rows])
            scoped = AuthorizedStore(store, manifest["sources"])
            resources.enter_context(patch.object(httpx.Client, "_send_single_request", bounded_send))
            for case in ([] if args.answer_only_retests else manifest["cases"]):
                row = next((r for r in report["cases"] if r["id"] == case["id"]), None)
                if row is None:
                    row = {**case, "source_check": "agent_original_source_checked", "retrieval": {}}
                    report["cases"].append(row)
                expected = {(p["version_id"], p["chunk_id"]) for p in case["expected_evidence"]}
                for strategy in ("dense", "hybrid"):
                    if strategy in row["retrieval"] or strategy in row.get("retrieval_failures", {}):
                        continue
                    phase = time.monotonic()
                    if report["counts"]["searches"] >= 48:
                        raise EvaluationStop("Search ceiling reached")
                    report["counts"]["searches"] += 1
                    save()
                    try:
                        result = retrieve(scoped, vectors, provider, reranker, settings, "local-test", case["question"],
                                          RetrievalOptions(strategy=strategy))
                    except BaseException as error:
                        row.setdefault("retrieval_failures", {})[strategy] = {"error_type": type(error).__name__,
                            "reason": str(error) if isinstance(error, (EvaluationStop, ProviderOperationError)) else "Search failed; no unreranked fallback"}
                        save()
                        if args.record_failures_and_continue and isinstance(error, ProviderOperationError):
                            if any(d["status"] in {401, 403, 429} for d in report.get("provider_diagnostics", [])):
                                raise
                            continue
                        raise
                    trace = ranking_trace(result, expected)
                    row["retrieval"][strategy] = {"matching_rank": trace["final"]["first_expected_rank"],
                        "ranking_trace": trace, "rerank_input_count": result["rerank_input_count"],
                        "timings_ms": result["timings_ms"], "full_report": result}
                    save()
                    if strategy == "hybrid":
                        if report["counts"]["answer_attempts"] >= 24:
                            raise EvaluationStop("Answer ceiling reached")
                        report["counts"]["answer_attempts"] += 1
                        row["generation_profile"] = "thinking_256_plus_answer_1024_preserve_source_qualifiers"
                        save()
                        def audit(draft, case_id=case["id"]):
                            (output / (case_id + "-draft.json")).write_text(json.dumps(draft, indent=2), encoding="utf-8")
                        row["answer"] = generate_answer(case["question"], result,
                            lambda p: p["source_file"] in manifest["sources"] and
                                store.is_source_current_approved("local-test", p["version_id"], p["chunk_id"]),
                            primary=command, fallback=NoFallback(), audit=audit)
                        row["answer_context_strategy"] = "hybrid"
                        save()
                    print(json.dumps({"case": case["id"], "kind": case["kind"], "strategy": strategy,
                        "before_rank": trace["candidate"]["first_expected_rank"],
                        "after_rank": trace["reranked"]["first_expected_rank"],
                        "final_rank": trace["final"]["first_expected_rank"],
                        "answer": row.get("answer", {}).get("status"), "http_attempts": report["counts"]["cohere_http_attempts"]}), flush=True)
                    if not args.record_failures_and_continue and row.get("answer", {}).get("status") in {"answer_validation_failed", "provider_unavailable"}:
                        raise EvaluationStop("Answer validation/provider failure; diagnose before further calls")
                    # Free-key evaluation must not burst requests. This pause
                    # occurs only inside the bounded run and never after a 429.
                    time.sleep(max(0, 8 - (time.monotonic() - phase)))
            # A failed hybrid request is NOT promoted to successful retrieval.
            # For answer evaluation only, an already successful dense/reranked
            # context can cover that same approved question without another
            # search. Label the different context explicitly and retain failure.
            for row in ([] if args.answer_only_retests else report["cases"]):
                if "answer" in row or "dense" not in row["retrieval"]:
                    continue
                if report["counts"]["answer_attempts"] >= 24:
                    raise EvaluationStop("Answer ceiling reached")
                report["counts"]["answer_attempts"] += 1
                row["generation_profile"] = "thinking_256_plus_answer_1024_preserve_source_qualifiers"
                save()
                def audit_dense(draft, case_id=row["id"]):
                    (output / (case_id + "-draft.json")).write_text(json.dumps(draft, indent=2), encoding="utf-8")
                row["answer"] = generate_answer(row["question"], row["retrieval"]["dense"]["full_report"],
                    lambda p: p["source_file"] in manifest["sources"] and
                        store.is_source_current_approved("local-test", p["version_id"], p["chunk_id"]),
                    primary=command, fallback=NoFallback(), audit=audit_dense)
                row["answer_context_strategy"] = "dense_after_failed_hybrid_for_evaluation_only"
                save()
                if not args.record_failures_and_continue and row["answer"]["status"] in {"answer_validation_failed", "provider_unavailable"}:
                    raise EvaluationStop("Answer validation/provider failure; diagnose before further calls")
            if args.answer_only_retests:
                for topic in ("what is a shared block", "minimum die area", "gds upload deadlines"):
                    row = next(c for c in report["cases"] if any(s["anchor"] == ANCHORS[topic] for s in c["source_checks"]))
                    if any(r["original_case_id"] == row["id"] for r in report.get("answer_only_retests", [])):
                        continue
                    if report["counts"]["answer_attempts"] >= 27:
                        raise EvaluationStop("Additional answer ceiling reached")
                    question = CLARIFIED_GDS if topic == "gds upload deadlines" else row["question"]
                    entry = {"original_case_id": row["id"], "question": question, "context_strategy": "hybrid",
                        "generation_profile": "thinking_256_plus_answer_1024_preserve_source_qualifiers"}
                    report.setdefault("answer_only_retests", []).append(entry)
                    report["counts"]["answer_attempts"] += 1
                    save()
                    def audit_retest(draft, case_id=row["id"]):
                        (output / (case_id + "-retest-draft.json")).write_text(json.dumps(draft, indent=2), encoding="utf-8")
                    entry["answer"] = generate_answer(question, row["retrieval"]["hybrid"]["full_report"],
                        lambda p: p["source_file"] in manifest["sources"] and
                            store.is_source_current_approved("local-test", p["version_id"], p["chunk_id"]),
                        primary=command, fallback=NoFallback(), audit=audit_retest)
                    save()
                    print(json.dumps({"answer_only_retest": topic, "status": entry["answer"]["status"],
                                      "http_attempts": report["counts"]["cohere_http_attempts"]}), flush=True)
                    if not args.record_failures_and_continue and entry["answer"]["status"] in {"answer_validation_failed", "provider_unavailable"}:
                        raise EvaluationStop("Answer-only check failed; diagnose without automatic repetition")
                    time.sleep(8)
        failed = any(c.get("retrieval_failures") or c.get("answer", {}).get("status") in
                     {"answer_validation_failed", "provider_unavailable"} for c in report["cases"])
        report["status"] = "completed_with_failures" if failed else "completed"
    except EvaluationStop as error:
        report.update(status="stopped_at_boundary", stop_reason=str(error))
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["completed_searches"] = sum(len(c["retrieval"]) for c in report["cases"])
        report["failed_searches"] = sum(len(c.get("retrieval_failures", {})) for c in report["cases"])
        report["summary"] = summarize(report)
        report["threshold_diagnostics"] = {strategy: threshold_report([
            {"split": c["split"], "group_id": c["group_id"], "answerable": c["kind"] == "answer",
             "top_score": c["retrieval"][strategy]["ranking_trace"]["top_rerank_score"]}
            for c in report["cases"] if strategy in c["retrieval"]], labels_verified=False)
            for strategy in ("dense", "hybrid")}
        save()
    print(json.dumps({"status": report["status"], "counts": report["counts"], "report": str(output / "report.json")}))
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
