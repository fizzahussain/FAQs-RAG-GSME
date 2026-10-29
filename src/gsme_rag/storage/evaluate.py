"""Score retrieval against reviewed evidence IDs"""

from pathlib import Path
import json
import statistics
import time

from gsme_rag.storage.pipeline import search_evidence


def evaluate_cases(
    store, vectors, provider, settings, cases_path: Path, hybrid: bool = False
) -> dict:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    label = fixture.get("source")
    if label not in ("synthetic", "source_reviewed"):
        raise ValueError("Evaluation source must be synthetic or source_reviewed")
    cases = fixture.get("cases", [])
    if not cases:
        raise ValueError("No reviewed question-to-evidence cases; cannot claim retrieval quality")
    # Validate the whole case set before a query can leave the machine.
    for case in cases:
        if not case.get("tenant_id", "").strip() or not case.get("question", "").strip():
            raise ValueError("Each evaluation case needs tenant_id and question")
        if label == "source_reviewed":
            expected = case.get("expected_evidence", [])
            if not expected:
                raise ValueError("Source-reviewed cases must pin expected_evidence by version, chunk and source hash")
            for item in expected:
                if not all(item.get(key) for key in ("version_id", "chunk_id", "source_sha256")):
                    raise ValueError("Expected evidence needs version_id, chunk_id and source_sha256")
                inspected = store.inspect_version(case["tenant_id"], item["version_id"], 1)
                if (not inspected or inspected["version"]["source_sha256"] != item["source_sha256"]
                        or not store.is_source_current_approved(case["tenant_id"], item["version_id"], item["chunk_id"])):
                    raise ValueError("Expected source evidence is unavailable, unapproved or superseded")
        elif not case.get("expected_chunk_ids"):
            raise ValueError("Every question must have reviewed expected evidence IDs")
    # Synthetic scores check the pipeline and never represent source quality
    times = []
    results = []
    invalid_returned = 0
    for case in cases:
        expected = ({(item["version_id"], item["chunk_id"]) for item in case["expected_evidence"]}
                    if label == "source_reviewed" else set(case["expected_chunk_ids"]))
        if not expected:
            raise ValueError("Every question must have reviewed expected evidence IDs")
        start = time.perf_counter()
        found = search_evidence(
            store,
            vectors,
            provider,
            settings,
            case["tenant_id"],
            case["question"],
            limit=10,
            hybrid=hybrid,
        )
        times.append((time.perf_counter() - start) * 1000)
        ranked = ([(hit["version_id"], hit["chunk_id"]) for hit in found]
                  if label == "source_reviewed" else [hit["chunk_id"] for hit in found])
        invalid_returned += sum(
            not store.is_source_current_approved(
                case["tenant_id"], hit["version_id"], hit["chunk_id"]
            )
            for hit in found
        )
        recall5 = len(expected.intersection(ranked[:5])) / len(expected)
        recall10 = len(expected.intersection(ranked[:10])) / len(expected)
        results.append(
            {
                "question": case["question"],
                "recall_at_5": recall5,
                "recall_at_10": recall10,
                "expected_chunk_ids": sorted(expected),
                "returned_chunk_ids": ranked,
                "missing_chunk_ids": sorted(expected - set(ranked)),
            }
        )
    counts = store.metrics()
    n = len(times)
    estimated_bytes = store.estimated_vector_bytes(settings.dimension)
    return {
        "source": label,
        "strategy": "hybrid" if hybrid else "dense",
        "questions": n,
        "recall_at_5": sum(r["recall_at_5"] for r in results) / n,
        "recall_at_10": sum(r["recall_at_10"] for r in results) / n,
        "wrong_version_or_revoked_returned": invalid_returned,
        "search_latency_ms_p50": statistics.median(times),
        "search_latency_ms_p95": sorted(times)[min(n - 1, int((n - 1) * 0.95 + 0.999))],
        "cumulative_ingestion_seconds": counts["ingestion_seconds"],
        "cumulative_embedding_calls": counts["embedding_calls"],
        "postgres_bytes": counts["postgres_bytes"],
        "estimated_vector_and_text_bytes_lower_bound": estimated_bytes,
        "failures": [r for r in results if r["missing_chunk_ids"]],
        "cases": results,
        "note": "Synthetic scores only exercise plumbing, not retrieval quality"
        if label == "synthetic"
        else "Source-reviewed case set; report sample size and coverage before extrapolating",
    }
