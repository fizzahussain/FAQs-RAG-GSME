# Retrieval and mandatory reranking — 0.18.0

The retrieval service returns approved evidence. Optional generation adds cited answers using Command first and local Ollama on operational failure. [0.17.0 strategies, real-source results, citation-selection fix and remaining acceptance work](CHANGES_0.17.0.md). Generation is opt-in with separate content permission. The local console remains at `http://127.0.0.1:8010/`; neither retrieval nor its operator token belongs on the public parsing application.

The main `/` page is a search-first interface with cited extracts and a collapsible document panel. `/admin` retains advanced review, exact schedules and diagnostics. Normal searches request at most 30 candidates, rerank the eligible subset in one call and show at most 5 matches. `rerank_input_count` reports the actual count. Parser/retrieval identities are 0.18.0; document chunker remains 0.17.0. [Schedule search](SCHEDULE_SEARCH.md) is a separate exact SQL path with local calendar/filter planning, no mandatory external rerank, and source-linked pagination. [Submission and demo](SUBMISSION_GUIDE.md).

## Implemented flow

1. Validate the question, workspace, bounded candidate/result counts and exact optional document/category/filename filters.
2. PostgreSQL supplies the currently source-approved, published, indexed point IDs matching those filters. Apply this scope as a Qdrant prefilter; do not search everything then hope a narrow document appears in the top results. Scopes above 100,000 points require narrower filters rather than silent truncation.
3. Embed the question. Retrieve up to 30 candidates by default. `hybrid` combines native Qdrant BM25 text search and dense rankings using reciprocal rank fusion; `dense` remains available for comparison. Both strategies require reranking. The hybrid text index is provisioned on demand; existing document vectors need no re-embedding.
4. Rehydrate candidate text from PostgreSQL and recheck eligibility before reranking. Qdrant payload text is not authoritative evidence.
5. Rerank every eligible candidate using the pinned Cohere model. Validate result indices, uniqueness, scores and complete candidate coverage. A provider failure raises an explicit error; there is no unreranked fallback.
6. Fetch only approved, indexed parts of the same source/version/unit for split context. Recheck candidates and supporting parts after the network operation. Revoked text is removed from both final evidence and diagnostic rankings.
7. Deduplicate repeated units and identical text within a version. Preserve independent versions/sources. Return up to five results within a shared 16,000-character context budget. Whole passages/parts may be omitted with warnings; evidence is never silently shortened.

`context_parts` contains the matched and supporting parts in source order. `text` is the actual matched/reranked passage. The score does not apply independently to added supporting parts. `context_complete=false` means required parts were missing, unapproved, unindexed, beyond the 20-part expansion bound or over budget. Do not present such a group as a complete answer.

## Setup

Keep existing credentials and volumes. Start Docker Desktop and the existing Compose services, install the updated editable package, then restart the console:

```powershell
docker compose -f compose.local.yaml up -d
.\.venv\Scripts\python.exe -m pip install -e ".[storage,test]"
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m gsme_rag.storage.admin
```

Neither 0.16.0 nor 0.16.1 adds a new SQL migration. Ranking-only changes in 0.16.0 did not invalidate extraction identities. The 0.16.1 hyperlink/review changes do advance parser/chunker identities; ingest a new source version to apply those fixes.

Real retrieval needs indexed Cohere embeddings, model access and these private settings:

```dotenv
COHERE_API_KEY=your-local-key
GSME_EXTERNAL_CONTENT_APPROVED=yes
GSME_RERANK_CONTENT_APPROVED=yes
GSME_RERANK_MODEL=rerank-v5.0-pro
GSME_RERANK_TIMEOUT=30
GSME_RERANK_INPUT_BUDGET=8192
```

Set permissions only when the organization permits sending **questions and approved passages** to Cohere. The interface also asks for per-request confirmation and clears it when the question/workspace/source filters change. Do not paste real keys into tickets or commit `.env`. The smoke test's process-only permission for generated synthetic text is not approval to send private GSME content.

The input budget uses a conservative combined query/passage UTF-8 byte guard with framing allowance. It rejects an oversized request before calling rerank rather than relying on the API's automatic long-document truncation. Timeouts and sanitized provider errors are explicit; automatic rerank retries are disabled to bound request usage. Billed usage is exposed when the provider returns it, not converted into an invented monetary estimate.

Fake embeddings and `synthetic-word-overlap-v1` reranking are paired fixtures in the synthetic collection. They never measure semantic quality. Real embeddings cannot be paired with a fake reranker to bypass the mandatory step.

## Interface and API

The main page shows one question, optional document scope, the strongest source extract, expandable alternatives and an actual rerank count. Advanced candidate controls, before/after rankings and diagnostics remain at `/admin`. Source opening uses the authenticated checksum-checked endpoint. `/api/search` and CLI `search` remain **diagnostic baselines**, not the reranked product path; consumers should use `/api/retrieve`.

`POST /api/retrieve` uses the existing local `X-GSME-Token` authentication and origin/loopback restrictions. Example document body:

```json
{
  "tenant": "local-test",
  "question": "How do I submit a design?",
  "provider": "cohere",
  "confirm_external": true,
  "route": "documents",
  "strategy": "hybrid",
  "candidate_count": 30,
  "top_k": 5
}
```

Optional fields: `document_id`, exact `category`, exact `source_file`, `context_chars`, and `min_rerank_score`. A threshold is optional and uncalibrated by default, not a confidence percentage. Responses identify the retrieval/embedding/reranker versions, synthetic status, request ID, candidate and reranked rankings, source hashes/IDs, pages, headings, table rows, warning codes, approval order and stage timings. No full questions or passages are added to server-side query logs.

Routes are explicit, not inferred by an LLM:

- `documents`: mandatory-reranked evidence. Schedule filters on this route are rejected rather than ignored.
- `schedules`: exact current-version SQL rows, no embeddings/reranking or external permission needed. Supply `schedule_kind` (`full`/`shared`), optional `technology_nm`, `flavor`, `tapeout_on_or_after`, and optional schedule `document_id`. A missing kind yields `clarification_needed`. Natural-language dates/technology are not silently inferred from the question.
- `mixed`: the two paths run separately. Document filters apply to document evidence, while typed schedule filters apply to schedule rows. `schedule_status` and `schedule_rows` are separate from document ranking/status. No common relevance score is invented.

Schedule responses report `has_more` (or `schedule_has_more` for mixed requests). `schedule_limit` defaults to 100, with a maximum of 999; refine filters or increase the limit when more rows exist. A bounded result list is not silently represented as the complete schedule.

Document statuses include `results`, `no_eligible_evidence`, `no_candidates`, `evidence_changed_during_retrieval` and `no_results_after_threshold_or_budget`. Empty eligibility avoids query embedding and reranking. Nonempty results are **candidates**, not proof the question is answerable: `answerability` remains `not_assessed`.

## CLI and repeatable demonstration

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli retrieve "Your question" --tenant local-test --provider cohere --strategy hybrid --confirm-external
.\.venv\Scripts\python.exe tools\demo_retrieval.py --provider fake
# Only generated synthetic FAQ text leaves the machine in this smoke:
.\.venv\Scripts\python.exe tools\demo_retrieval.py --provider cohere --confirm-external --output validation\retrieval-live-synthetic
```

The demo generates three known FAQ pairs, verifies their extracted text, approves **only** those generated units, and indexes them in an isolated workspace/collection. It runs dense and hybrid evaluation on three positive questions plus one unanswerable question and writes replayable reports under ignored `validation/`. It retains its synthetic index/data for replay; it does not approve private sources or reconcile unrelated collections. No GPU is required for the API-backed models.

To inspect a demo in the console, run it against your configured local database, then start the console in a separate terminal with `GSME_COLLECTION` set to the report's `collection_base`. Enter the report's `tenant_id` in the interface and select the same provider. For CLI evaluation/replay, use that collection base too. Automated service tests use an isolated test database instead of the normal development database; their data is not automatically visible in a console connected to the development database.

## Prepare the GSME benchmark

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli draft-evaluation --tenant local-test --version-id YOUR_CURRENT_VERSION
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli evaluate-retrieval validation\reviewed-cases.json --provider cohere --strategy dense --confirm-external
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli evaluate-retrieval validation\reviewed-cases.json --provider cohere --strategy hybrid --confirm-external
```

The draft command only prints suggested original FAQ questions and pinned approved source references; it makes no model call and does not write a benchmark. Save the output locally, inspect every case, add paraphrases, manual/table/identifier/multi-part questions and negative cases, then change `source` from `draft` to `source_reviewed` only after verification. Drafts and empty sets are rejected. Reserve held-out cases before tuning; original FAQ wording alone is not representative.

Each positive case needs `tenant_id`, `question` and `expected_evidence: [{version_id, chunk_id, source_sha256}]`. A negative case instead has `expect_no_answer: true`. Optional `filters` accept `document_id`, `category`, `source_file`, `candidate_count` (at least 10 for Recall@10) and `context_chars`. Strategy and score threshold are controlled by the evaluation command for consistent comparisons. Every pinned expected source is validated before any query leaves the machine. The evaluator reports candidate recall, before/after Recall@5, final Recall@10, MRR@5, nDCG@5, context recall, p50/p95 latency, invalid returned evidence and negative cases that still return results. Legacy CLI `evaluate` remains positive-case baseline evaluation.

## Verified results and remaining work

The records below are historical. Current answer-generation results are in [0.17.0](CHANGES_0.17.0.md); current schedule/source regressions and full-suite results are in [0.18.0](CHANGES_0.18.0.md).

Latest verification, 6 November 2026: **230 tests passed with zero skips** and the search-first browser workflow passed. An explicitly authorized real-source run indexed 29 FAQ and 246 manual passages. All 18 ready, **source-filtered** positive cases found the target in the top 5 (17 at rank 1); both out-of-scope cases still returned candidates. Table/blank/held cases are recorded separately, not counted as positive answers. See [0.16.4 results, actual rerank counts and limitations](CHANGES_0.16.4.md). Private source-backed cases/results stay under ignored `validation/product-evaluation/`.

Historical verification record, 3 November 2026 (synthetic fixture, before the real-source run):

| Check | Result |
| --- | --- |
| Final full suite | **202 tests passed, zero skips**, 142.299 seconds; isolated PostgreSQL, real Qdrant and configured private-manual parsing checks. No external model calls from the suite. |
| Real storage regressions | Native BM25/RRF queries, exact source scope, tenant isolation, revocation during reranking, and split FAQ context before/after middle-part approval passed. |
| Live Cohere synthetic smoke | Three generated FAQ targets and one negative question, dense and hybrid paths, plus a final 0.16.0 response check. Model: `rerank-v5.0-pro`; all positive targets found. No private GSME evidence transmitted. |
| Local console demo | Authenticated API returns three cited synthetic results and three before/after ranking rows. Replay data: ignored `validation/retrieval-console-demo/report.json`. |
| Packaging and hygiene | Editable install updated; wheel integrity and source-byte/resource checks passed. JavaScript syntax, HTML/script IDs, Python syntax/imports and dependency checks passed. No private credential values found in Git candidates; private `.env` unchanged. |

The four-question live fixture observed median total latency of 2,580.877 ms for dense and 1,149.475 ms for hybrid; a separate cold/concurrent final query took 6,576.118 ms, including 727.203 ms in reranking. These are tiny local smoke observations with concurrent test activity, not a controlled comparison or SLA. The final provider response reported 120 input tokens for that rerank call; no price estimate is inferred.

The full suite includes real PostgreSQL/Qdrant hybrid search, source filters, API flow and concurrent revocation checks, plus local fake-provider tests for input budgets, no-fallback errors, malformed scores/indices, source-order split expansion, deduplication and draft/negative evaluation handling. Run service tests against an isolated `gsme_rag_test*` database as documented in the storage runbook.

A live Cohere smoke on three generated FAQs and four questions confirmed model access, real query embeddings, mandatory `rerank-v5.0-pro`, native hybrid search and source-linked results. Both strategies retrieved all three positive targets in this tiny fixture; it does not demonstrate improvement on GSME documents. The negative question still returned candidates without a calibrated threshold. No private GSME content was sent in that smoke.

Still required before quality claims: a representative source-reviewed GSME benchmark, held-out no-answer calibration, latency/cost/load measurements at the intended corpus size, and source review of the manual gaps recorded in `PROJECT_AUDIT.md`. Scope-ID prefiltering is deliberately simple for this local corpus and needs load testing before large-scale deployment. Conversational answer generation, chat history, automatic intent/entity inference and production multi-user authorization remain separate work.

Provider contracts: [Cohere Rerank API](https://docs.cohere.com/reference/rerank), [Qdrant hybrid search](https://qdrant.tech/documentation/search/text-search/hybrid-search/).
