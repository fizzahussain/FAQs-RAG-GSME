"""Inspect rank movement and tune relevance gates without new provider calls.

Retrieving a related passage for an unanswerable question is not equivalent to
answering it incorrectly. Keep candidate relevance, context and final-answer
evaluation separate; a rerank score is not a factual correctness certificate.
"""

import math


def ranking_trace(report, expected):
    expected = {(str(version), chunk) for version, chunk in expected}
    stages = {"candidate": report.get("initial_candidates", []),
              "reranked": report.get("reranked_candidates", []),
              "final": report.get("results", [])}
    traces = {}
    for stage, records in stages.items():
        matches = []
        seen = set()
        for position, item in enumerate(records, 1):
            key = (str(item["version_id"]), item["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            if key in expected:
                matches.append({"version_id": key[0], "chunk_id": key[1],
                    "rank": position, "retrieval_rank": item.get("retrieval_rank"),
                    "rerank_rank": item.get("rerank_rank"), "rerank_score": item.get("rerank_score")})
        first = matches[0]["rank"] if matches else None
        traces[stage] = {"first_expected_rank": first, "expected_matches": matches,
                         **{f"hit_at_{k}": (first is not None and first <= k) if expected else None
                            for k in (1, 3, 5, 10)},
                         "reciprocal_rank": (1 / first if first else 0) if expected else None}
    before, after = traces["candidate"]["first_expected_rank"], traces["reranked"]["first_expected_rank"]
    traces["rank_gain"] = before - after if before is not None and after is not None else None
    traces["reranker_made_expected_rank_worse"] = before is not None and (after is None or after > before)
    traces["top_rerank_score"] = next((r.get("rerank_score") for r in stages["reranked"]), None)
    traces["unanswerable_with_context"] = not expected and bool(stages["final"])
    return traces


def threshold_report(cases, *, labels_verified=False, max_false_accept_rate=.05, min_positive_retention=.90):
    """Tune on labelled tuning cases only; report held-out results separately.

    Each row needs split, group_id, answerable and top_score. Source variants of
    the same question must share a group so they cannot leak across splits.
    This tunes a query relevance gate, not a guarantee of grounded answers.
    """
    if not 0 <= max_false_accept_rate <= 1 or not 0 <= min_positive_retention <= 1:
        raise ValueError("Calibration targets must be rates between zero and one")
    groups = {}
    for row in cases:
        if row.get("split") not in {"tuning", "heldout"} or not row.get("group_id") or type(row.get("answerable")) is not bool:
            raise ValueError("Calibration requires split, group_id and verified boolean answerability")
        score = row.get("top_score")
        if score is not None and (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("Calibration scores must be finite relevance scores")
        if row["group_id"] in groups and groups[row["group_id"]] != row["split"]:
            raise ValueError("Question variants cannot appear in both tuning and held-out sets")
        groups[row["group_id"]] = row["split"]
    tuning = [r for r in cases if r["split"] == "tuning"]
    heldout = [r for r in cases if r["split"] == "heldout"]

    def measure(rows, threshold):
        positives = [r for r in rows if r["answerable"]]
        negatives = [r for r in rows if not r["answerable"]]
        accepted = lambda r: r["top_score"] is not None and r["top_score"] >= threshold
        tp, fp = sum(accepted(r) for r in positives), sum(accepted(r) for r in negatives)
        return {"positive_cases": len(positives), "negative_cases": len(negatives),
                "positive_retention": tp / len(positives) if positives else None,
                "false_accept_rate": fp / len(negatives) if negatives else None,
                "accepted_positive_queries": tp, "accepted_negative_queries": fp}

    thresholds = sorted({0., 1., *[r["top_score"] for r in tuning if r["top_score"] is not None]})
    sweep = [{"threshold": value, **measure(tuning, value)} for value in thresholds]
    feasible = [r for r in sweep if r["false_accept_rate"] is not None and r["positive_retention"] is not None
                and r["false_accept_rate"] <= max_false_accept_rate and r["positive_retention"] >= min_positive_retention]
    enough = all(len({r["group_id"] for r in tuning if r["answerable"] == label}) >= 5 for label in (True, False))
    selected = max(feasible, key=lambda r: r["threshold"]) if feasible and enough and labels_verified else None
    return {"status": "candidate_for_heldout_validation" if selected else "not_calibrated",
            "recommended_threshold": selected["threshold"] if selected else None,
            "tuning": selected, "heldout": measure(heldout, selected["threshold"]) if selected else None,
            "sweep": sweep, "labels_verified": labels_verified,
            "note": "No runtime setting was changed. Relevance acceptance is not answer correctness. "
                    "Require separate held-out human answer review and bind settings to model/corpus/strategy."}
