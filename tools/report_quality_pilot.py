"""Local-only pilot ranking/latency report. Never invent human review labels."""

from argparse import ArgumentParser
import json
from pathlib import Path
import statistics


def summarize_rankings(report):
    positives = [c for c in report["cases"] if c["kind"] == "answer"]
    negatives = [c for c in report["cases"] if c["kind"] == "no_answer"]
    output = {}
    for strategy in ("dense", "hybrid"):
        completed = [c["retrieval"][strategy] for c in positives if strategy in c["retrieval"]]
        stages = {}
        for stage in ("candidate", "reranked", "final"):
            ranks = [r["ranking_trace"][stage]["first_expected_rank"] for r in completed]
            stages[stage] = {"measured_positive_cases": len(ranks),
                **{f"hit_at_{k}": sum(r is not None and r <= k for r in ranks) / len(ranks) if ranks else None
                   for k in (1, 3, 5)},
                "mrr_at_5": sum(1 / r for r in ranks if r is not None and r <= 5) / len(ranks) if ranks else None}
        all_rows = [c["retrieval"][strategy] for c in report["cases"] if strategy in c["retrieval"]]
        timings = sorted(r["timings_ms"]["total"] for r in all_rows)
        output[strategy] = {"stages": stages,
            "successful_searches": len(all_rows),
            "failed_searches": sum(strategy in c.get("retrieval_failures", {}) for c in report["cases"]),
            "positive_rank_improvements": sum((r["ranking_trace"]["rank_gain"] or 0) > 0 for r in completed),
            "positive_rank_demotions": sum(r["ranking_trace"]["reranker_made_expected_rank_worse"] for r in completed),
            "rerank_input_counts": sorted({r["rerank_input_count"] for r in all_rows}),
            "retrieval_median_ms": statistics.median(timings) if timings else None,
            "retrieval_p95_ms": timings[max(0, (95 * len(timings) + 99) // 100 - 1)] if timings else None,
            "negative_with_related_context": sum(bool(c["retrieval"].get(strategy, {}).get("full_report", {}).get("results")) for c in negatives)}
    return output


def main():
    cli = ArgumentParser(description=__doc__)
    cli.add_argument("--directory", type=Path, default=Path("validation/quality-pilot-0.19.0"))
    args = cli.parse_args()
    if not args.directory.resolve().is_relative_to((Path(__file__).resolve().parents[1] / "validation").resolve()):
        raise ValueError("Private reports must stay under ignored validation/")
    report = json.loads((args.directory / "report.json").read_text(encoding="utf-8"))
    metrics = summarize_rankings(report)
    (args.directory / "ranking-summary.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    lines = ["# Source-checked live pilot", "", f"Run status: {report['status']}. Agent source-checked, not a human-approved benchmark.", "",
             "24 planned questions: 12 answerable / 12 near-topic negatives. Generation normally uses hybrid context; any answer evaluated with an already reranked dense context after a hybrid failure is labelled separately. No document re-embedding or Gemini calls.", "",
             "## Positive passage ranks", "", "| Strategy | Stage | Measured positives | Hit@1 | Hit@3 | Hit@5 | MRR@5 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for strategy, data in metrics.items():
        for stage, values in data["stages"].items():
            lines.append(f"| {strategy} | {stage} | {values['measured_positive_cases']} | "
                + " | ".join("unmeasured" if values[k] is None else f"{values[k]:.3f}" for k in ("hit_at_1", "hit_at_3", "hit_at_5", "mrr_at_5")) + " |")
    lines.extend(["", "## Question-level results", "",
        "Expected IDs include known valid alternative passages. A near-topic negative may retrieve related content without supplying an answer.", "",
        "| Question | Expected | Dense candidate → rerank → final | Hybrid candidate → rerank → final | Answer status / context |",
        "| --- | --- | --- | --- | --- |"])
    for case in report["cases"]:
        cells = []
        for strategy in ("dense", "hybrid"):
            trace = case["retrieval"].get(strategy, {}).get("ranking_trace")
            cells.append(" → ".join(str(trace[s]["first_expected_rank"] or "—") for s in ("candidate", "reranked", "final"))
                         if trace else "FAILED; no ranking" if strategy in case.get("retrieval_failures", {}) else "not run")
        lines.append(f"| {case['question'].replace('|', '/')} | {case['kind']} | {cells[0]} | {cells[1]} | {case.get('answer', {}).get('status', 'not run')} / {case.get('answer_context_strategy', 'hybrid' if 'answer' in case else 'none')} |")
    if report.get("answer_only_retests"):
        lines.extend(["", "## Separately approved answer-only checks", "",
            "Original results above are retained. These checks reused saved hybrid passages and did not repeat retrieval.", "",
            "| Question | Status | Answer |", "| --- | --- | --- |"])
        for check in report["answer_only_retests"]:
            answer = check.get("answer", {})
            text = answer.get("text", "No completed response").replace("|", "/").replace("\n", " ")
            lines.append(f"| {check['question']} | {answer.get('status', 'not completed')} | {text} |")
    lines.extend(["", "## Budget and limitations", "", "```json", json.dumps(report["counts"], indent=2), "```", "",
        "Exact evidence quotations and citation checks do not prove every generated claim is entailed. Human groundedness/qualification review is separate. "
        "No relevance cutoff was changed; tuning diagnostics are exploratory and held-out questions must remain independent. "
        "Small, selected pilot results do not establish corpus-wide accuracy or hosted multi-user performance. "
        "Generation configuration changed during diagnosis; do not label all answers as a single final-profile benchmark. "
        "Hybrid ran second with cached query vectors/token counts: observed dense/hybrid latency is not a fair cold-start comparison.", ""])
    (args.directory / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"run_status": report["status"], "metrics": metrics, "output": str(args.directory / "RESULTS.md")}))


if __name__ == "__main__":
    main()
