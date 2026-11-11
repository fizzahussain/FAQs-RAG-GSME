"""Separate citation integrity, answerability and human semantic judgments."""

from collections import Counter
from hashlib import sha256
import json


def answer_id(answer):
    payload = {key: answer.get(key) for key in ("status", "text", "claims", "citations", "provider", "model")}
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def summarize(report, human_reviews=()):
    """Never label quote validity or an LLM's own decision 'grounded accuracy'."""
    rows = report["cases"]
    positives = [row for row in rows if row["kind"] != "no_answer"]
    negatives = [row for row in rows if row["kind"] == "no_answer"]
    strategies = {}
    for strategy in ("dense", "hybrid"):
        ranks = [row["retrieval"].get(strategy, {}).get("matching_rank") for row in positives]
        strategies[strategy] = {"positive_cases": len(ranks), "top5_matches": sum(rank is not None and rank <= 5 for rank in ranks),
            "mrr_at_5": sum(1 / rank for rank in ranks if rank is not None and rank <= 5) / len(ranks) if ranks else None}
    judgments = {review["answer_id"]: review for review in human_reviews}
    reviewed = []
    reviewed_claims = []
    failures = Counter()
    for row in rows:
        answer = row.get("answer", {})
        judgment = judgments.get(answer_id(answer))
        if judgment:
            if judgment.get("verdict") not in {"supported", "unsupported", "correct_abstention", "incorrect_abstention"} or not judgment.get("reviewer") or not judgment.get("rationale"):
                raise ValueError("Human judgments need a verdict, reviewer and source-check rationale")
            reviewed.append(judgment)
            claims = answer.get("claims", [])
            seen = set()
            for review in judgment.get("claim_reviews", []):
                index = review.get("claim_index")
                if (type(index) is not int or not 0 <= index < len(claims) or index in seen
                        or review.get("verdict") not in {"supported", "unsupported", "contradicted"}
                        or not review.get("rationale")):
                    raise ValueError("Claim reviews need unique valid indices, verdicts and source-check rationales")
                seen.add(index)
                reviewed_claims.append(review)
        if answer.get("status") in {"answer_validation_failed", "provider_unavailable"}:
            failures[answer["status"]] += 1
        elif row["kind"] == "no_answer" and answer.get("status") == "answered":
            failures["negative_question_answered"] += 1
        elif row["kind"] != "no_answer" and answer.get("status") != "answered":
            failures["positive_question_not_answered"] += 1
        for result in row.get("retrieval", {}).values():
            if result.get("failure_category"):
                failures[result["failure_category"]] += 1
    return {"source": report["source"], "strategies": strategies,
        "positive_generated_answers": sum(row.get("answer", {}).get("status") == "answered" for row in positives),
        "positive_cases": len(positives), "negative_cases": len(negatives),
        "negative_abstentions": sum(row.get("answer", {}).get("status") in {"insufficient_evidence", "clarification_needed"} for row in negatives),
        "human_semantic_reviews": len(reviewed),
        "human_supported_answers": sum(review["verdict"] == "supported" for review in reviewed),
        "human_reviewed_claims": len(reviewed_claims),
        "unsupported_claim_rate": sum(r["verdict"] == "unsupported" for r in reviewed_claims) / len(reviewed_claims) if reviewed_claims else None,
        "contradicted_claim_rate": sum(r["verdict"] == "contradicted" for r in reviewed_claims) / len(reviewed_claims) if reviewed_claims else None,
        "semantic_groundedness_accuracy": sum(review["verdict"] in {"supported", "correct_abstention"} for review in reviewed) / len(reviewed) if reviewed else None,
        "failure_categories": dict(failures), "http_usage": report["counts"],
        "historical_attempt_failures": dict(Counter(previous.get("status") for row in rows for previous in row.get("answer_history", [])
            if previous.get("status") in {"answer_validation_failed", "provider_unavailable"})),
        "note": "Generated answers and exact citation integrity do not independently establish semantic correctness; human judgments are separate and bound to answer hashes."}
