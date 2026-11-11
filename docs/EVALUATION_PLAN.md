# Retrieval and answer acceptance

## Current controls

| Control | Current default | Meaning |
| --- | --- | --- |
| Retrieval | Hybrid dense + BM25, RRF fusion | Combines semantic and lexical candidates |
| Candidate / rerank budget | Up to 30 passages | Actual eligible count may be lower |
| Final context | Top 5; 16,000 characters | No silent evidence truncation |
| Minimum rerank score | Unset | No experimentally justified cutoff yet |
| Rerank / answer timeout | 30 / 60 seconds | Failure must not become an invented answer |
| Generation | Command first, Ollama operational fallback | Not Gemini by default; not a fallback for missing evidence |
| Schedule route | Typed SQL filters | Exact dates/milestones, not vector similarity |

Rerank scores measure relevance, not factual correctness. Do not choose 0.7 or another arbitrary cutoff and call it verified. `GSME_MIN_RERANK_SCORE` is configurable for the hosted app after calibration.

## What to measure

For each source-checked positive question, record the expected evidence IDs and their candidate, reranked and final ranks; hit@1/3/5/10, reciprocal rank and rank movement. Report candidates missed entirely separately from reranker demotions and final-context exclusions. Existing expected-ID metrics and new ranking traces are complementary: a correct document alone is weaker than the correct passage.

For near-topic negatives, related chunks can legitimately be retrieved. The important distinction is whether they answer the question. Measure false evidence acceptance and unsupported final answers separately. Require explicit abstention when sources do not support the requested claim. Numeric, formula, chart-category and missing-link questions need particularly strict source review.

Generation checks validate exact quoted evidence and citation IDs. Human claim review separately labels supported, unsupported or contradicted claims, with rationales. Automatic citation validity is not semantic groundedness certification. Reports expose claim-review coverage and unsupported/contradicted rates only when judgments exist.

## Draft cases and threshold selection

`tools/draft_challenge_cases.py` builds ignored local `validation/challenge-benchmark/` artifacts from approved FAQ/manual evidence after checking stored source hashes. Original FAQ questions, paraphrases and near-topic negatives are drafts until reviewed against originals. Per-manual-chunk lexical probes cover the indexed corpus but are not independent semantic tests.

Review negative absence claims and expected positive evidence. Split by topic/group so paraphrases cannot leak between tuning and held-out sets. The threshold helper requires verified labels, at least five distinct tuning groups in each class, and no cross-split groups before recommending a cutoff. Its targets are at most 5% negative acceptance and at least 90% positive query retention; report held-out results independently. Positive query retention does not prove that the correct passage was retained. If targets cannot be met, report that rather than force a threshold.

No new live evaluation is implied by local tests. The proposed bounded pilot requires separate approval: accepted FAQ QA DOCX/manual passages only, 24 questions, dense/hybrid 48 searches, at most 30 passages/rerank, at most 24 Command answers and 240 total Cohere HTTP attempts including retries. Reuse document embeddings, stop on quota errors, and do not call Gemini. The reported free evaluation key implies expected $0 only while its free entitlement applies; quota is still consumed. This pilot is smaller than the full draft question bank and is not a release-wide quality guarantee.

The user subsequently approved that pilot and three additional answer-only checks, raising only the Command ceiling to 27. The run used **125/240 HTTP attempts**, with **45/48 successful searches** and retained failures. See the [exact live results and limitations](QUALITY_PILOT_0.19.0.md). It is not a fully green acceptance benchmark or human groundedness certification; do not replay the spent ledger.
