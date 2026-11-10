# 0.17.0 — retrieval strategies, Office sources and citation by selection

Local-first increment, 10 November 2026. Credentials and private permissions were preserved. Existing approved versions stay searchable; extraction/ranking identities advance for newly processed evidence. No wholesale re-embedding or new SQL migration is required.

## Capabilities

| Area | Implementation and boundary |
| --- | --- |
| Retrieval/reranking | Dense or native Qdrant BM25 hybrid/RRF, mandatory learned Cohere reranking; default up to 30 passages and 5 final results. No unreranked fallback. |
| Query handling | Optional local acronym expansion and up to 3 query variants; original identifiers, values and negations survive. One global candidate cap; rerank against the original question. |
| Routing | Optional `auto` document/schedule/mixed routing. Ambiguous kinds or untyped natural-language numeric/date filters cause clarification, not guessed SQL. |
| Contextual chunks | Source-derived headings, FAQ anchors, rows/slides and hashes; generated context is not passed off as original evidence. |
| Semantic boundaries | Optional local Nomic paragraph embeddings. Exact source text, pages, units and review state survive. Model digest enters ingestion/chunk identity. Nomic does not replace Cohere search vectors. |
| Compression | Optional exact paragraph excerpts with offsets; no generated summaries or dismantling of QA/table/code/formula/flagged evidence. |
| PPTX | Slide text/links, grouped shapes and FAQ tables; notes, chart XML, image/formula evidence stored separately for review. No trusted visual mappings invented. |
| XLSX | Visible cells, FAQ headers, physical rows and links; formulas/merged/error/formatted-number/generic cells reviewed. Hidden data is excluded from ordinary evidence. No macro/formula/external-link execution. Typed schedules still use known CSV columns. |
| Incremental indexing | Identical text vectors may be reused only within the same tenant/model/collection after independent new-source acceptance/token validation. No cross-tenant reuse or copied approval. Cache loss simply embeds normally. |
| Versioning | Immutable original/normalized hashes, separate decisions, publication pointers and revocation checks remain authoritative. |
| Generation | Command primary, local Qwen operational fallback; optional separately permitted Gemini adapter. Models select passage IDs and the application copies exact support text. |
| Visual inspection | Local PDF region render/OCR with hashes, coordinates and disagreement records; optional Gemini descriptions remain unapproved drafts, never auto-indexed. |
| Evaluation | Comparative ranks, abstention counts, historical/current failure categories and separate hash-bound human groundedness judgments. |

Expansion, multi-query, semantic boundaries and compression remain **opt-in**: no live result here establishes they improve GSME ranking. The live comparison used original questions and existing structural chunks.

## Demonstrated fixes

Command's initial wire/schema included unsupported Pydantic length constraints. A documented `response_format.schema` with flattened refs now carries portable constraints; strict bounds are enforced locally. Early provider bodies were not retained, so each initial request difference cannot be individually attributed. Experimental SDK warnings remain visible.

An evaluation-only hook inspected unread HTTPX responses below `Client.send`, making `ResponseNotRead` appear as provider failure. It now reads first, with a regression test and persistent aggregate/endpoint request ceilings.

Two free-form citation drafts failed source matching. Production generation now selects server-issued `quote_id` values; the application dereferences exact source text. Unknown IDs, unsupported numeric/URL claims, incomplete and invalid answers fail closed. This proves citation integrity, **not semantic entailment**.

The exact Word FAQ has four merged category bands but independent Q/A cells under explicit headers. Bands no longer taint every row. Physical rows/category context survive; genuinely merged Q/A, OCR and damaged text stay reviewed. Native rerun: **30 answer-bearing rows, 28 clean, 2 reviewed**, no oversized chunks. The original blank first answer is not invented. This file was audited locally, not republished/embedded in the live evaluation.

## Verification

Authorized external content was restricted to accepted passages from exact `FAQs QA Format.docx` and `MFG-GSME-0002.pdf` versions/hashes. Candidate scope included both authorized files, not unrelated workspace evidence. No document embedding call was made; approved answer-only retests reused previously retrieved, still-current passages.

| Observation | Result |
| --- | --- |
| Retrieval | Dense and hybrid each: **10/10 top-5**, MRR@5 **0.925**; 9 first-place targets, TSV fourth. No hybrid superiority established. |
| Final answers | **10/10 positive answers**, **2/2 negative abstentions**, local support checks passed. |
| Protocol | Legal/TSV passed real passage-selection checks. Other successful live cases used earlier exact-quote validation; not all cases were rerun under the final protocol. |
| History | **9 validation failures + 1 provider-unavailable attempt** retained; final success does not erase them. |
| Usage | **97 HTTP attempts**, including 24 query embedding calls, 24 reranks and 22 Command attempts. Approved ceilings: 132 total / 22 Command. |
| Cost basis | User confirmed free evaluation key; expected $0 under that plan, monthly quota consumed. Billing not independently queried. |
| Groundedness | Human-signed semantic reviews: **0**. Source-checked regression results are not representative held-out accuracy certification. |
| Full regression suite | **271 tests passed**, zero failures, errors or skips, in **107.230 seconds**. Includes isolated PostgreSQL and local Qdrant integration checks. |
| Browser and packaging | Desktop/mobile interface checks passed, including citation links and safe answer rendering. Version 0.17.0 wheel verified; dependency and whitespace checks passed. |

Private cases, drafts, source refs, attempts, usage and summary: ignored `validation/answer-evaluation/`. Human judgments require exact answer hash, reviewer and rationale; changed answers cannot inherit old judgments.

Manufacturing-process PDF physical pages 1/30/101/163 were audited and original renders inspected: native `pymupdf_table_fallback`, 12 chunks, 9 reviewed, 4 excluded visual-gap units, zero oversized, latest slice 5.913 seconds. Parent hash/map/parent-page fields are explicit. This is not full 188-page revalidation; physical page 101 displays printed slide 71.

Manual pages 30/101/163 have new local region/OCR records under `validation/visual-review/`: selectable text, alternate OCR, bounding box and hashes remain separate; every record is review-required, unapproved and ineligible for embedding. No formula/chart association was approved.

Actual local Nomic smoke preserved all characters across two synthetic pieces (5.38 seconds). Actual Qwen passage-selection smoke answered a synthetic positive in 49.4 seconds using a 120-second test timeout and abstained on an unrelated question in 6.5 seconds. Earlier cold startup exceeded the ordinary 60-second timeout. These are tiny runtime checks, not an SLA or quality certification.

## Configuration

Defaults remain original-question hybrid retrieval and structural chunking. UI Settings/API/CLI expose query handling: API `query_mode=original|expanded|multi`, `route=auto` optionally; CLI `retrieve --query-mode ...`.

```dotenv
GSME_CHUNK_STRATEGY=auto
# semantic uses local nomic-embed-text:latest and records its exact digest.
GSME_CONTEXT_COMPRESSION=none
# paragraph extracts exact prose; it is not a factual summarizer.
GSME_ANSWER_PROVIDER=cohere
```

Optional Gemini needs `GEMINI_API_KEY`, an available `GSME_GEMINI_MODEL`, `GSME_GEMINI_CONTENT_APPROVED=yes` and existing general/answer permissions. Vision additionally needs `GSME_VISION_CONTENT_APPROVED=yes` and explicit CLI confirmation. No Google calls were made; adapter tests are mocked. Google is not an automatic hidden fallback.

```powershell
# Local-only rendering/OCR; cannot auto-index the result:
.\.venv\Scripts\python.exe tools\review_visual_region.py "D:\Downloads\MFG-GSME-0002.pdf" --page 30
```

## Remaining acceptance and deployment work

- Source-owner verification of equations, uncertain numeric/merged tables, OCR and chart mappings. Missing URLs and blank answers cannot be reconstructed by code.
- Representative human-reviewed tuning/held-out cases, conflicts and adversarial-source tests. No-answer thresholds and semantic/compression strategies remain uncalibrated.
- Real company PPTX/XLSX examples: current format tests are synthetic. Optional Gemini/vision live checks need separate access/approval.
- Company identity/roles, document access policy, hosting, backups/restore/retention and workload/latency targets. A local token and loopback bind are not enterprise multi-user authorization.

Local tests passing does not declare production complete; a second model cannot authorize invented answers.

References: [Cohere schema constraints](https://docs.cohere.com/docs/structured-outputs), [pricing basis](https://docs.cohere.com/v2/docs/how-does-cohere-pricing-work), [python-pptx](https://python-pptx.readthedocs.io/en/latest/user/quickstart.html), [openpyxl](https://openpyxl.readthedocs.io/en/stable/api/openpyxl.reader.excel.html), [Gemini API](https://ai.google.dev/api/generate-content).
