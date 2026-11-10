"""Retrieve, rerank and assemble source evidence without generating an answer."""

from dataclasses import dataclass
import math
import time
from uuid import UUID, uuid4
from gsme_rag.version import RETRIEVAL_VERSION
from gsme_rag.observability import REQUEST_ID, emit, operation

from .reranking import validate_ranking
from .planning import plan_query, fuse_query_hits


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
             options: RetrievalOptions | None = None) -> dict:
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
    if not scope:
        return finish("no_eligible_evidence")
    phase = time.perf_counter()
    query_vectors = []
    for variant in plan.variants:
        if len(provider.encode_tokens(variant)) > settings.max_tokens:
            raise ValueError("Query exceeds embedding token budget")
        with operation("retrieval.query_embedding", request_id=report["request_id"], model=provider.model_version):
            query_vectors.append(provider.embed_query(variant))
    timings["query_embedding"] = round((time.perf_counter() - phase) * 1000, 3)
    phase = time.perf_counter()
    scope_set = set(scope)
    with operation("retrieval.candidate_search", request_id=report["request_id"]):
        rankings = [vectors.query(tenant_id, query_vector, options.candidate_count,
                             text=variant if options.strategy == "hybrid" else None,
                             model_version=provider.model_version, point_ids=scope)
                    for query_vector, variant in zip(query_vectors, plan.variants, strict=True)]
        rankings = [[hit for hit in ranking if str(hit.id) in scope_set] for ranking in rankings]
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
        return finish("no_candidates")
    phase = time.perf_counter()
    report["rerank_input_count"] = len(candidates)
    with operation("retrieval.rerank", request_id=report["request_id"], candidates=len(candidates), model=reranker.model_version):
        ranked = validate_ranking(reranker.rank(question, [row["evidence_text"] for _, _, row in candidates]), len(candidates))
    timings["reranking"] = round((time.perf_counter() - phase) * 1000, 3)
    report["reranking_status"] = "completed"
    report["candidate_count"] = len(candidates)
    report["rerank_usage"] = getattr(reranker, "last_usage", {})
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
        return finish("results")
    if not report["initial_candidates"]:
        return finish("evidence_changed_during_retrieval")
    return finish("no_results_after_threshold_or_budget")
