# Bounded live quality pilot — 11 November 2026

User-approved scope: accepted passages from `FAQs QA Format.docx` and `MFG-GSME-0002.pdf` only. Original hashes/current approval were checked locally before sending. Nine FAQ and three manual topics each have an answerable and near-topic negative question. Original FAQ text and rendered manual pages 50/107/120 were inspected before the run; page 114 was also rendered during relevance review.

This is an **agent-source-checked diagnostic pilot**, not a human-approved acceptance benchmark. Configuration changed in response to failures; it is not a uniform before/after generation experiment. No existing document was published to guests.

## Exact accounting

| Operation | Observed | Approved ceiling |
| --- | --- | --- |
| Questions in original pilot | 24 (12 positive / 12 negative) | 24 |
| Search attempts | 48; 45 usable results, 3 failures | 48 |
| Passages per attempted rerank payload | 30 | 30 |
| Query embedding requests | 25, including recomputation after interruption | 48 endpoint guard |
| Document embedding requests | 0 | 0 |
| Command calls | 24 original + 3 separately approved answer-only checks | 27 after additional approval |
| Total Cohere HTTP attempts, including failures | 125 | 240 |
| Gemini calls / local-model fallback calls | 0 / 0 | Excluded from this pilot |

Existing document vectors were reused. Failed operations consumed budget and were not replayed. Two negative answers used their already successful, reranked dense contexts after hybrid transport failures; these are labelled in the private report, not treated as successful hybrid searches. No quota error was observed; the quota-stop boundary is unit-tested. Expected charge was $0 under the user's confirmed free key; billing entitlement/invoice was not independently inspected.

## Pinned-ID retrieval results

| Strategy / stage | Positive Hit@1 | Positive Hit@5 | MRR@5 |
| --- | --- | --- | --- |
| Dense candidates | 11/12 | 11/12 | 0.917 |
| Dense after reranking / final | 10/12 | 12/12 | 0.896 |
| Hybrid candidates | 10/12 | 11/12 | 0.875 |
| Hybrid after reranking / final | 10/12 | 12/12 | 0.896 |

TSV's pinned definition moved from candidate rank 6 dense / 7 hybrid to rank 4 after reranking. Shared Block's pinned FAQ moved from rank 1 dense / 2 hybrid to rank 2. Inspection found valid alternatives at rank 1: the TSV explanatory paragraph on manual page 114 and the FAQ about Muse handling Shared Block aggregation. The original gold IDs under-cover relevant passages; they were **not silently expanded after observing the scores**. These movements are not by themselves proof that the reranker worsened semantic relevance. Full blind relevance annotation is still needed.

Every positive had a pinned expected passage in final top 5. This does not establish recall over the whole corpus or all valid answers. Dense and hybrid tied on this small reranked set; no superiority claim is justified. Measured median retrieval time was 2.93 seconds dense / 1.26 seconds hybrid, but hybrid ran second and reused the query vector/token count, so these are **not fair cold-latency comparisons**.

Successful negative searches also returned related chunks, as expected without a score gate. Top rerank scores ranged 0.593–0.853 on positives and 0.044–0.315 on observed negatives. That sample separation is exploratory, not a verified universal cutoff. No runtime relevance threshold was changed. Failed negative searches are not included as successful rejection measurements.

## Generation results and defects

Original pilot, before separate retests: **9/12 positives returned answers**, one abstained, one was blocked as incomplete, and one encountered a connection failure. **10/12 negatives abstained**; one malformed output was blocked and one encountered a connection failure. Zero negatives returned an answered status, but errors are not counted as correct abstentions. Human claim-groundedness accuracy remains unmeasured.

| Finding | Evidence / cause | Change / verification |
| --- | --- | --- |
| Shared Block answer truncated | Provider reported MAX_TOKENS: 903 reasoning tokens within 1,024 total output tokens | Reserve 256 bounded thinking tokens plus the separate 1,024-token answer allowance for the two known reasoning-capable Command models. Older models receive no unsupported thinking setting. SDK-wire, token-limit and failure-boundary tests cover this. |
| Disabling thinking returned non-JSON prose | Live COMPLETE response still failed strict JSON parsing | Do not strip arbitrary prose or extract a convenient JSON suffix. Keep strict blocking; final setting uses bounded reasoning instead. The intermediate failure remains recorded. |
| Minimum-area answer lost technology qualification | Original FAQ says the minimum varies by technology and Shared Block can be as small as 1mm2 for popular technologies | Prompt explicitly preserves conditions and uncertainty. Separate live retest included both the technology qualification and the 1mm2 lower-bound wording. Prompting is not a mathematical entailment guarantee. |
| Broad GDS “requirements” question abstained with deadline evidence present | Source gives relative deadlines; the broad wording can imply other submission requirements | Separate approved check clarified the question to ask how many days before tapeout. It returned preliminary 14 / final 7 days with citations. This does not prove automatic clarification or the original broad wording is fixed. |
| Three search failures and two original answer connection failures | One later captured ConnectError explicitly reports Windows error 10053, an established connection aborted by host software; earlier failures lack that detailed cause | Preserve failed attempts, add safe exception-class diagnostics, retain private transport evidence and enforce no unreranked fallback. DNS/direct TLS checks succeeded separately. Those checks do not establish the underlying software/route cause. Do not disable security software; investigate with company IT. |

Cohere documents [bounded thinking and response allowance](https://docs.cohere.com/v2/docs/reasoning). The final adapter requests 1,280 total output tokens for these supported models: 256 thinking plus 1,024 answer. This is not a new complex-query routing feature.

The **three separately approved answer-only checks passed** protocol/citation validation and agent source comparison: Shared Block, qualified minimum area, and clarified GDS deadlines. They reused existing approved hybrid passages; no extra search, query embedding or rerank occurred. Originals remain in the report alongside retests. MTTF connection failures and the Shared Block negative's malformed intermediate-profile response were not live-retested after the final change; the 27-call authority is exhausted.

## Source and release work remaining

- Have an independent reviewer annotate all acceptable passages and generated claims. The small paired pilot, easy synthetic-project negatives and mixed generation profiles cannot certify company-wide accuracy.
- Clarify the additional-samples FAQ with its owner: it says both that orders use sample-quantity increments and that any quantity is possible before tapeout. The generated answer repeated the latter without the increment qualification. Do not silently choose a policy or rewrite the source.
- Resolve recurring Windows/transport failures and obtain a clean post-fix live evaluation under a new bounded approval. Do not rerun this spent ledger or reinterpret failures as abstentions.
- Real Workspace client/domain, hosted origin, durable storage/worker requirements, backups, isolation and expected-load checks remain deployment gates. Private sign-in remains disabled until configured; no Render/Vercel deployment was performed.

Private inspectable artifacts, excluded from Git: `validation/quality-pilot-0.19.0/manifest.json`, `report.json`, `RESULTS.md`, `ranking-summary.json`, rendered source pages and raw provider drafts. Drafts are not exposed as chatbot answers. HTTP counters are persisted before requests; the report retains interrupted runs and configuration profiles.
