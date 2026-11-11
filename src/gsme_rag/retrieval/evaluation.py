"""Pinned question-to-evidence evaluation, including before/after rankings."""

import json
import math
from pathlib import Path
import statistics

from .service import RetrievalOptions, retrieve
from .benchmarking import ranking_trace


def draft_cases(store, tenant_id: str, version_id: str) -> dict:
    """Suggest original FAQ questions from approved sources; never approve a benchmark."""
    inspection = store.inspect_version(tenant_id, version_id, 1)
    if not inspection or not inspection["version"]["is_current"]:
        raise ValueError("Choose a currently published source version in this workspace")
    with store.connect() as db:
        rows = db.execute("""SELECT source_chunk_id,faq_question,source_sha256
            FROM approved_current_evidence WHERE tenant_id=%s AND version_id=%s
            AND faq_question IS NOT NULL ORDER BY chunk_index NULLS LAST,part_index,source_chunk_id""",
            (tenant_id, version_id)).fetchall()
    by_question = {}
    for row in rows:
        by_question.setdefault(row["faq_question"], []).append({"version_id": str(version_id),
            "chunk_id": row["source_chunk_id"], "source_sha256": row["source_sha256"]})
    return {"source": "draft", "cases": [{"tenant_id": tenant_id, "question": question,
        "expected_evidence": expected} for question, expected in by_question.items()],
        "instructions": "Not an approved benchmark. Check each question and supporting source; add paraphrases, technical and negative cases, split tuning/held-out sets. Change source to source_reviewed only after human verification. Original FAQ questions alone are not representative quality evidence."}


def evaluate_retrieval(store, vectors, provider, reranker, settings, path: Path,
                       strategy: str = "hybrid", min_rerank_score: float | None = None) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("source") not in {"synthetic", "source_reviewed"} or not fixture.get("cases"):
        raise ValueError("A nonempty synthetic or source-reviewed question set is required")
    def case_options(case):
        filters = case.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - {"document_id", "category", "source_file", "candidate_count", "context_chars"}:
            raise ValueError("Evaluation filters allow document/category/filename, candidate_count and context_chars only")
        return RetrievalOptions(strategy=strategy, top_k=10, **filters, min_rerank_score=min_rerank_score)

    # Validate every pinned expected source before any query leaves the machine.
    for case in fixture["cases"]:
        if not case.get("tenant_id", "").strip() or not case.get("question", "").strip():
            raise ValueError("Every case needs a workspace and question")
        expected = case.get("expected_evidence", [])
        if bool(expected) == bool(case.get("expect_no_answer", False)):
            raise ValueError("Each case needs expected evidence OR expect_no_answer=true")
        for item in expected:
            if not all(item.get(k) for k in ("version_id", "chunk_id", "source_sha256")):
                raise ValueError("Expected evidence must pin version, chunk and source hash")
            source = store.inspect_version(case["tenant_id"], item["version_id"], 1)
            if (not source or source["version"]["source_sha256"] != item["source_sha256"]
                    or not store.is_source_current_approved(case["tenant_id"], item["version_id"], item["chunk_id"])):
                raise ValueError("Expected evidence is unavailable, unapproved or superseded")
        case_options(case)

    rows = []
    invalid = 0
    for case in fixture["cases"]:
        report = retrieve(store, vectors, provider, reranker, settings, case["tenant_id"], case["question"],
            case_options(case))
        expected = {(item["version_id"], item["chunk_id"]) for item in case.get("expected_evidence", [])}
        before = [(item["version_id"], item["chunk_id"]) for item in report["initial_candidates"]]
        after = [(item["version_id"], item["chunk_id"]) for item in report["results"]]

        def recall(ranking):
            return len(expected & set(ranking)) / len(expected) if expected else None

        def reciprocal(ranking):
            return next((1 / rank for rank, item in enumerate(ranking[:5], 1) if item in expected), 0.0)

        def ndcg(ranking):
            dcg = sum(1 / math.log2(rank + 1) for rank, item in enumerate(ranking[:5], 1) if item in expected)
            ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(5, len(expected)) + 1))
            return dcg / ideal if ideal else None

        context = [part for item in report["results"] for part in [item, *item["supporting_evidence"]]]
        context_ids = [(item["version_id"], item["chunk_id"]) for item in context]
        for item in [*report["initial_candidates"], *context]:
            invalid += not store.is_source_current_approved(case["tenant_id"], item["version_id"], item["chunk_id"])
        rows.append({"question": case["question"], "expect_no_answer": not bool(expected),
            "ranking_trace": ranking_trace(report, expected),
            "candidate_recall": recall(before), "before_recall_at_5": recall(before[:5]),
            "after_recall_at_5": recall(after[:5]), "after_recall_at_10": recall(after[:10]),
            "context_recall": recall(context_ids), "before_mrr_at_5": reciprocal(before) if expected else None,
            "after_mrr_at_5": reciprocal(after) if expected else None, "after_ndcg_at_5": ndcg(after),
            "returned_results": len(after), "negative_case_with_results": not expected and bool(after),
            "status": report["status"], "timings_ms": report["timings_ms"],
            "returned_evidence": [{"version_id": v, "chunk_id": c} for v, c in after],
            "missing_expected_evidence": [{"version_id": v, "chunk_id": c} for v, c in sorted(expected - set(context_ids))]})
    metrics = {}
    for key in ("candidate_recall", "before_recall_at_5", "after_recall_at_5", "after_recall_at_10",
                "context_recall", "before_mrr_at_5", "after_mrr_at_5", "after_ndcg_at_5"):
        values = [row[key] for row in rows if row[key] is not None]
        metrics[key] = statistics.mean(values) if values else None
    for stage in ("candidate", "reranked", "final"):
        for cutoff in (1, 3, 5, 10):
            values = [row["ranking_trace"][stage][f"hit_at_{cutoff}"] for row in rows
                      if row["ranking_trace"][stage][f"hit_at_{cutoff}"] is not None]
            metrics[f"{stage}_hit_at_{cutoff}"] = statistics.mean(values) if values else None
    times = sorted(row["timings_ms"]["total"] for row in rows)
    return {"source": fixture["source"], "synthetic_models": not provider.external,
        "strategy": strategy, "reranker_model": reranker.model_version, "questions": len(rows),
        "positive_cases": sum(not row["expect_no_answer"] for row in rows),
        "negative_cases": sum(row["expect_no_answer"] for row in rows),
        "negative_cases_with_results": sum(row["negative_case_with_results"] for row in rows),
        "wrong_version_or_revoked_returned": invalid, "metrics": metrics,
        "latency_ms_p50": statistics.median(times), "latency_ms_p95": times[math.ceil(len(times) * .95) - 1],
        "min_rerank_score": min_rerank_score, "cases": rows,
        "note": "Synthetic models/cases test plumbing, not GSME relevance. Negative cases measure returned candidates, not generated answers. Scores require held-out calibration."}
