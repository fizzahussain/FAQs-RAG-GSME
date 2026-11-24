"""Retrieve, rerank and assemble source evidence without generating an answer."""

from dataclasses import dataclass
import math
import os
import re
import time
from uuid import UUID, uuid4
from gsme_rag.version import RETRIEVAL_VERSION
from gsme_rag.observability import REQUEST_ID, emit, operation

from .reranking import validate_ranking
from .planning import plan_query, fuse_query_hits
from gsme_rag.storage.providers import ProviderOperationError
from .local_fallback import OllamaReranker, cooldown_key, cooling_down, record_outage


@dataclass(frozen=True)
class RetrievalOptions:
    top_k: int = 5
    candidate_count: int = 30
    strategy: str = "hybrid"
    document_id: str | None = None
    category: str | None = None
    source_file: str | None = None
    context_chars: int = 16000
    min_rerank_score: float | None = None
    query_mode: str = "original"
    local_outage_fallback: bool = False

    def __post_init__(self):
        if not 1 <= self.top_k <= 20 or not self.top_k <= self.candidate_count <= 100:
            raise ValueError("Require 1–20 results and top_k <= candidate_count <= 100")
        if self.strategy not in {"dense", "hybrid"} or not 1800 <= self.context_chars <= 64000:
            raise ValueError("Invalid candidate strategy or context character budget")
        if self.query_mode not in {"original", "expanded", "multi"}:
            raise ValueError("Invalid query expansion mode")
        if self.document_id:
            UUID(self.document_id)
        if any(value is not None and (not value.strip() or len(value) > 300)
               for value in (self.category, self.source_file)):
            raise ValueError("Source/category filters must be nonblank and bounded")
        if self.min_rerank_score is not None and (not math.isfinite(self.min_rerank_score)
                                                 or not 0 <= self.min_rerank_score <= 1):
            raise ValueError("Invalid rerank score threshold")


def evidence_record(row: dict) -> dict:
    """Citation text always comes from PostgreSQL, never vector payloads."""
    result = {key: row.get(key) for key in (
        "source_file", "source_sha256", "heading_path", "category", "faq_question",
        "table_row_number", "unit_id", "part_index", "part_count", "review_required",
        "parser_backend", "parser_version", "chunker_version", "chunk_type", "chunk_index",
        "warning_codes", "source_anchor", "approval_order", "approval_basis", "source_verified",
    )}
    result.update(document_id=str(row["document_id"]), version_id=str(row["version_id"]),
                  chunk_id=row["source_chunk_id"], text=row["evidence_text"],
                  page_start=row["source_page_start"], page_end=row["source_page_end"],
                  evidence_status="approved")
    return result


def retrieve(store, vectors, provider, reranker, settings, tenant_id: str, question: str,
             options: RetrievalOptions | None = None, *, on_event=None) -> dict:
    options = options or RetrievalOptions()
    if not tenant_id.strip() or len(tenant_id) > 120 or not question.strip() or len(question) > 20000:
        raise ValueError("A bounded question and workspace are required")
    if provider.external != reranker.external:
        raise ValueError("Synthetic and real embedding/rerank providers cannot be mixed")
    started = time.perf_counter()
    plan = plan_query(question, options.query_mode)
    timings = {}
    report = {
        "request_id": REQUEST_ID.get() or str(uuid4()), "retrieval_version": RETRIEVAL_VERSION,
        "route": "documents", "status": "no_eligible_evidence",
        "strategy": options.strategy, "embedding_model": provider.model_version,
        "reranker_model": reranker.model_version, "reranking_status": "not_run_no_candidates",
        "synthetic": not provider.external, "answerability": "not_assessed",
        "filters": {"document_id": options.document_id, "category": options.category,
                    "source_file": options.source_file},
        "requested_candidates": options.candidate_count, "candidate_count": 0, "rerank_input_count": 0,
        "context_characters": 0,
        "initial_candidates": [], "reranked_candidates": [], "results": [], "warnings": [], "timings_ms": timings,
        "min_rerank_score": options.min_rerank_score,
        "query_mode": options.query_mode, "query_variant_count": len(plan.variants),
        "query_plan_reasons": list(plan.reasons),
        "candidate_fusion": "rrf_across_queries" if len(plan.variants) > 1 else "single_query",
        "note": "Relevance scores are not factual confidence. No answer has been generated.",
    }

    def finish(status):
        report["status"] = status
        timings["total"] = round((time.perf_counter() - started) * 1000, 3)
        emit("retrieval.completed", request_id=report["request_id"], status=status,
             results=len(report["results"]), candidates=report["candidate_count"], elapsed_ms=timings["total"])
        return report

    # The complete eligible point set is also a Qdrant prefilter. This avoids
    # starving a narrow document/category search behind unrelated top hits and
    # prevents stale/unapproved text in vector payloads from reaching reranking.
    scope = store.retrieval_scope(tenant_id, provider.model_version, vectors.collection,
                                  options.document_id, options.category, options.source_file)
    timings["eligibility"] = round((time.perf_counter() - started) * 1000, 3)
    emit("retrieval.eligibility", request_id=report["request_id"], eligible=len(scope),
         document_id=options.document_id, model=provider.model_version,
         collection=vectors.collection, min_rerank_score=options.min_rerank_score)
    if not scope:
        return finish("no_eligible_evidence")
    outage_key = cooldown_key(provider, reranker)
    local_mode = options.local_outage_fallback and provider.external and cooling_down(outage_key)
    report["fallback_used"] = False

    def use_local(stage, error=None):
        report["fallback_used"] = True
        report["warnings"].append("local_outage_recovery_quality_not_cohere_equivalent")
        emit("retrieval.fallback", stage=stage, backend="ollama", fallback_used=True,
             failure_reason=error.error_code if error else "provider_cooldown")
        if on_event:
            on_event({"type":"status", "stage":"local_retrieval", "backend":"ollama", "fallback_used":True})

    phase = time.perf_counter()
    query_vectors = []
    try:
        if not local_mode:
            for variant in plan.variants:
                if len(provider.encode_tokens(variant)) > settings.max_tokens:
                    raise ValueError("Query exceeds embedding token budget")
                with operation("retrieval.query_embedding", request_id=report["request_id"], model=provider.model_version):
                    query_vectors.append(provider.embed_query(variant))
    except ProviderOperationError as error:
        if not (options.local_outage_fallback and provider.external and record_outage(outage_key, error)):
            raise
        local_mode = True
        use_local("query_embedding", error)
    timings["query_embedding"] = round((time.perf_counter() - phase) * 1000, 3)
    phase = time.perf_counter()
    scope_set = set(scope)
    with operation("retrieval.candidate_search", request_id=report["request_id"]):
        if local_mode:
            use_local("lexical_candidates")
            report["strategy"] = "bm25_local_outage"
            rankings = [vectors.lexical_query(tenant_id, variant, options.candidate_count,
                         model_version=provider.model_version, point_ids=scope) for variant in plan.variants]
        else:
            rankings = [vectors.query(tenant_id, query_vector, options.candidate_count,
                             text=variant if options.strategy == "hybrid" else None,
                             model_version=provider.model_version, point_ids=scope)
                    for query_vector, variant in zip(query_vectors, plan.variants, strict=True)]
        rankings = [sorted((hit for hit in ranking if str(hit.id) in scope_set),
                           key=lambda hit: (-float(hit.score), str(hit.id))) for ranking in rankings]
        hits = rankings[0] if len(rankings) == 1 else fuse_query_hits(rankings, options.candidate_count)
    timings["candidate_search"] = round((time.perf_counter() - phase) * 1000, 3)
    hit_ids = [str(hit.id) for hit in hits if str(hit.id) in scope_set]
    allowed = store.allowed_hits(tenant_id, hit_ids, provider.model_version, vectors.collection)
    candidates = []
    seen = set()
    for hit in hits:
        point = str(hit.id)
        if point in allowed and point in scope_set and point not in seen:
            seen.add(point)
            candidates.append((point, hit, allowed[point]))
            if len(candidates) == options.candidate_count:
                break
    if not candidates:
        emit("retrieval.candidates.filtered", request_id=report["request_id"], hits=len(hits), candidates=0)
        return finish("no_candidates")
    phase = time.perf_counter()
    def bound_local_candidates():
        if options.min_rerank_score is not None:
            # Avoid any local inference when the configured gate is known to
            # be incomparable. Never silently loosen a deployment's threshold.
            raise ProviderOperationError("Configured Cohere relevance threshold is not calibrated for local reranking",
                                         error_code="local_threshold_uncalibrated", retryable=False)
        configured = int(os.getenv("GSME_LOCAL_RERANK_CANDIDATES", "10"))
        if not 1 <= configured <= 30:
            raise ValueError("Local candidate limit must be between 1 and 30")
        limit = max(options.top_k, configured)
        if len(candidates) > limit:
            report["warnings"].append("local_candidate_limit_reduces_recall")
            del candidates[limit:]
        report["local_candidate_limit"] = limit

    if local_mode:
        bound_local_candidates()
    documents = [row["evidence_text"] for _, _, row in candidates]
    active_reranker = OllamaReranker() if local_mode else reranker
    try:
        with operation("retrieval.rerank", request_id=report["request_id"], candidates=len(candidates), model=active_reranker.model_version):
            ranked = validate_ranking(active_reranker.rank(plan.variants[0], documents), len(candidates))
    except ProviderOperationError as error:
        if local_mode or not (options.local_outage_fallback and provider.external and record_outage(outage_key, error)):
            raise
        local_mode = True
        use_local("reranking", error)
        bound_local_candidates()
        documents = [row["evidence_text"] for _, _, row in candidates]
        active_reranker = OllamaReranker()
        ranked = validate_ranking(active_reranker.rank(plan.variants[0], documents), len(candidates))
    report["reranker_model"] = active_reranker.model_version
    report["rerank_input_count"] = len(candidates)
    timings["reranking"] = round((time.perf_counter() - phase) * 1000, 3)
    report["reranking_status"] = "completed"
    report["candidate_count"] = len(candidates)
    report["rerank_usage"] = getattr(active_reranker, "last_usage", {})
    phase = time.perf_counter()

    # Only same-unit parts can expand a result. Arbitrary neighboring paragraphs
    # and entire pages are not automatically evidence for the user's question.
    related = {}
    related_units = {}
    for item in ranked:
        point, _, row = candidates[item.index]
        if row.get("unit_id") and row.get("part_count", 1) > 1:
            key = (str(row["version_id"]), row["unit_id"])
            if key not in related_units:
                related_units[key] = store.retrieval_parts(tenant_id, key[0], key[1],
                                                           provider.model_version, vectors.collection)
            related[point] = related_units[key]
    all_ids = list(seen | {str(p) for points in related.values() for p in points})
    current = store.allowed_hits(tenant_id, all_ids, provider.model_version, vectors.collection)
    # Filter diagnostic candidates too: revocation during a network call must
    # not leak the old text via the before/after display.
    for index, (point, hit, _) in enumerate(candidates):
        if point in current:
            report["initial_candidates"].append({**evidence_record(current[point]),
                "retrieval_rank": index + 1, "retrieval_score": float(hit.score)})
    for rank, item in enumerate(ranked, start=1):
        point, hit, _ = candidates[item.index]
        if point in current:
            row = current[point]
            report["reranked_candidates"].append({"version_id": str(row["version_id"]),
                "chunk_id": row["source_chunk_id"], "source_file": row["source_file"],
                "page_start": row["source_page_start"], "retrieval_rank": item.index + 1,
                "retrieval_score": float(hit.score), "rerank_rank": rank, "rerank_score": item.score})
    emit("retrieval.ranking", request_id=report["request_id"], hits=len(hits), candidates=len(candidates),
         ranked_chunks=[{key: r[key] for key in ("version_id", "chunk_id", "retrieval_rank", "rerank_rank", "rerank_score")}
                        for r in report["reranked_candidates"]])
    used_units = set()
    used_texts = set()
    used_chars = 0
    for rank, item in enumerate(ranked, start=1):
        point, hit, _ = candidates[item.index]
        row = current.get(point)
        if row is None or (options.min_rerank_score is not None and item.score < options.min_rerank_score):
            continue
        unit = (str(row["version_id"]), row.get("unit_id") or row["source_chunk_id"])
        text_identity = (str(row["version_id"]), row["evidence_text"])
        if unit in used_units or text_identity in used_texts:
            continue
        record = evidence_record(row)
        if used_chars + len(record["text"]) > options.context_chars:
            report["warnings"].append("context_budget_skipped_whole_passage")
            continue
        used_chars += len(record["text"])
        used_units.add(unit)
        used_texts.add(text_identity)
        support = []
        for part in sorted((current[p] for p in related.get(point, []) if p in current and p != point),
                           key=lambda part: part["part_index"]):
            if used_chars + len(part["evidence_text"]) <= options.context_chars:
                support.append(evidence_record(part))
                used_chars += len(part["evidence_text"])
        complete = len(support) + 1 == row.get("part_count", 1)
        if not complete:
            report["warnings"].append("split_unit_incomplete_or_over_context_budget")
        record.update(retrieval_rank=item.index + 1, retrieval_score=float(hit.score),
                      rerank_rank=rank, rerank_score=item.score, supporting_evidence=support,
                      context_parts=sorted([evidence_record(row), *support], key=lambda part: part["part_index"]),
                      context_complete=complete)
        report["results"].append(record)
        if len(report["results"]) == options.top_k:
            break
    report["context_characters"] = used_chars
    timings["context_and_recheck"] = round((time.perf_counter() - phase) * 1000, 3)
    report["warnings"] = sorted(set(report["warnings"]))
    if report["results"]:
        phase = time.perf_counter()
        expand_comparison_context(store, tenant_id, question, report, options.context_chars)
        timings["comparison_context"] = round((time.perf_counter() - phase) * 1000, 3)
        report["warnings"] = sorted(set(report["warnings"]))
        return finish("results")
    if not report["initial_candidates"]:
        return finish("evidence_changed_during_retrieval")
    return finish("no_results_after_threshold_or_budget")


def expand_comparison_context(store, tenant, question, report, budget):
    """Bring both sides of one explicit source comparison into bounded context.

    These are source-section siblings, not reranked hits. Keep that distinction
    in diagnostics and recheck approval/ACL before exposing each sibling.
    """
    if not re.search(r"\b(?:compar\w*|difference|versus|vs\.?)\b", question, re.I):
        return
    anchor = next((r for r in report["results"][:2] if r.get("context_complete") and
                   any(re.search(r"\b(?:vs\.?|versus|comparison)\b", h, re.I)
                       for h in r.get("heading_path") or [])), None)
    if anchor is None or not hasattr(store, "comparison_context"):
        return
    existing = {(p["version_id"], p["chunk_id"]) for r in report["results"] for p in r["context_parts"]}
    added = 0
    for row in store.comparison_context(tenant, anchor["version_id"], anchor["chunk_id"]):
        part = evidence_record(row)
        if ((part["version_id"], part["chunk_id"]) in existing or part.get("review_required")
                or part.get("chunk_type") != "section" or row.get("part_count") != 1
                or part["version_id"] != anchor["version_id"]
                or part.get("heading_path") != anchor.get("heading_path")
                or part.get("category") != anchor.get("category")
                or not store.is_source_current_approved(tenant, part["version_id"], part["chunk_id"])):
            continue
        if report["context_characters"] + len(part["text"]) > budget:
            report["warnings"].append("comparison_context_budget_omitted")
            continue
        part["context_origin"] = "approved_comparison_section_not_vector_hit"
        anchor["context_parts"].append(part)
        existing.add((part["version_id"], part["chunk_id"]))
        report["context_characters"] += len(part["text"])
        added += 1
        if added == 3:
            break
    if added:
        report["warnings"].append("approved_comparison_section_expanded")
        emit("retrieval.comparison.expanded", request_id=report.get("request_id"),
             version_id=anchor["version_id"], chunks=added)
