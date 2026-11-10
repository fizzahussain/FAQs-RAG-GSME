# 0.16.4 — search-first interface and source-grounded retrieval checks

## Main interface

`http://127.0.0.1:8010/` now opens the source-search product, not the operator console. Connect once, ask a question, read the strongest source extract and expand other matches if needed. Source names/pages and original downloads are visible; raw rankings/IDs remain under Search details. Documents are in a collapsible panel with searchable/held counts. No chunk review checkboxes, rationale fields or engineering tabs appear on the main page.

The previous controls remain at `/admin` for exception review and troubleshooting. The token stays in memory; workspace disconnect clears credentials/results. Real Cohere calls still require explicit document/query permission. Demo mode is labelled nonsemantic. Source HTML stays inert and only safe source hyperlinks become clickable. Identical re-uploads reuse the same logical document; they do not create duplicate hits. Large document ingestion continues bounded batches and provides a continuation action without requiring another upload.

## Reranking count

The normal search requests up to **30** candidate passages and displays up to **5** matches. Hybrid dense/BM25 rankings are fused to that candidate limit, not sent as 30+30 passages. PostgreSQL eligibility checks and point deduplication run before reranking. The service additionally enforces the requested limit even if a vector adapter returns too many hits. `rerank_input_count`, the UI and content-free logs expose the actual count. Only those matching eligible passages—not the complete manual—go to a rerank call. Approved split-unit supporting parts may be added afterward without a second rerank.

## Demonstrated manual availability defect

The native parser counted every drawing when deciding whether a page might contain an unverified diagram. Page frames, logo assets, tiny bullets and underlines caused 115 generic vector warnings in the exact 175-page manual. Page-located warnings then prevented even clean prose/glossary paragraphs from automatic acceptance.

`_substantive_drawings` now rejects margin graphics, thin text rules, tiny bullets and whole-page frames for that drawing-count heuristic. The original pages 50 and 120 were rendered and compared. Real body drawings remain eligible for detection; the separate visual-region/chart rules are unchanged. The full native rerun retained 1,440 chunks, all 674 table rows, all 902 review-marked chunks, 85 exclusions and the existing formula/chart gates. Generic vector warnings reduced from 115 to 2. The unchanged held evidence is not promoted as verified chart/formula text. Parser identity advances to 0.16.4; chunker identity remains 0.16.1.

## Source-backed cases and live-run constraints

`tools/evaluate_gsme_sources.py` checks a local case catalog against the original Word/PDF files and checksum-verified normalized audits before evaluating. The real catalog, original excerpts, renders and results live under ignored `validation/product-evaluation/`, not Git. The set is explicitly an agent-source-checked draft, not a signed human benchmark.

The current local set has **29 cases**: 18 ready answer cases, 3 merged-table cases blocked by extraction policy, 1 genuinely blank original table answer, 5 exclusion checks, and 2 out-of-scope questions. It covers FAQ paraphrases, contacts, agreements, GDS deadlines, sample quantities, shared coordination, glossary terms, uHAST and the known manual-page 30/101/163 gaps. Source references retain hashes, extraction backend, page locations and stable evidence IDs.

Real runs require `--live --confirm-external` and configured permissions. The user explicitly authorized sending the accepted text from the three named documents plus these 29 questions. The evaluator processes only the named versions, retains partial reports, reuses completed embeddings, bounds retries and paces questions conservatively. Results are not represented as a representative quality certification or a calibrated no-answer policy.

Live indexing exposed transient tokenizer transport errors and HTTP 429. Sanitized errors now include exception types; indexing honors a provider retry cooldown (60 seconds when 429 has no numeric Retry-After). Hidden SDK embedding/tokenizer retries are disabled: tokenizer transport failures have one explicit retry, while durable index jobs own embedding/rate-limit retries. [Cohere rate limits](https://docs.cohere.com/v2/docs/rate-limits) distinguish Tokenize, Embed and Rerank quotas; a faster local GPU does not remove an API quota. Rerank failures still produce no unreranked fallback.

## Verification, 6 November 2026

| Check | Observed result |
| --- | --- |
| Full suite | 230 tests passed, 0 failures/errors/skips; unittest elapsed 156.236 seconds. Isolated PostgreSQL, real Qdrant and the private manual checks were enabled; no external calls from the suite. |
| Browser | Headless Edge verified both the search home and advanced console, upload/automatic ingestion, identical re-upload, authentication, safe links/inert HTML, keyboard interaction, mobile layout and disconnect. Synthetic data only. |
| Actual indexed corpus | 29 clean FAQ passages and 246 clean manual passages in `local-test` / `gsme_evidence_v1`; merged FAQ table remains held. Automatic acceptance is not human source verification. |
| Source-scoped positive queries | 18/18 expected targets in the top 5; 17 ranked first, TSV ranked fourth. Real Cohere embeddings and `rerank-v5.0-pro`. These positives used their exact source-file filter, so this is not an all-documents ranking certification. |
| Held/blank checks | 5 held anchors did not appear in returned context. Three merged-table questions and one blank-answer question had no eligible candidates in the evaluated index. They are blocked/source gaps, not successful answers. |
| No-answer checks | Both out-of-scope questions still returned nearest candidates. No-answer calibration is unfinished; rerank scores are not confidence probabilities. |
| Rerank payload | 13 questions sent 29 passages, 12 sent 30, and 4 had no eligible passages and made no rerank call. No call sent the full manual. |
| Observed latency | Across 25 completed nonempty queries: median total 2,183.194 ms, median reranking 936.273 ms, maximum total 3,486.341 ms. Excludes between-question pacing and failed attempts/resume time; not an SLA or load test. |
| Package | Editable installation reports 0.16.4; `pip check` passed. |

Local case/report paths are `validation/product-evaluation/cases.json` and `report.json`; normalized audits, original-page renders and source hashes remain beside them. Browser screenshots are under `validation/console-ui/`. These private artifacts remain ignored by Git. The live run completed by resuming only unfinished questions after transport failures; completed queries were not resent.

Remaining source work: 30 merged-table answer records are held; the first original answer is blank. The manual retains 1,109 held chunks and 85 excluded units. Formula regions on pages 30/163, chart associations on page 101, uncertain table cells and unresolved source links need actual source review/correction, not bulk approval. This run used native extraction; it is not a new Docling validation or an evaluation of the separate manufacturing-process slide deck.

Remaining product work: representative human-reviewed, held-out cross-document tests and no-answer calibration; production authentication, backups/retention, quota/load/resilience checks before deployment. The current product retrieves cited source extracts. LLM answer composition and conversational memory are not implemented.
