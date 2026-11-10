# 0.18.1 — provider and resource-lifecycle audit

10 November 2026, branch `dev`. Package version advances; parser 0.18.0, chunker 0.17.0 and retrieval 0.18.0 identities are unchanged. No reparse, database migration or re-embedding is required for these fixes.

## Demonstrated defects fixed

| Finding | Change / regression |
| --- | --- |
| Legacy index/search/preview/reconciliation/repair API clients could remain open, including early error returns | Managed per-request Qdrant clients close on success and failure. Endpoint tests check each path and preview 404. Publish no longer constructs a client just to calculate a collection name. |
| Gemini answer preflight unnecessarily required a Cohere answer key | Check the selected answer provider's own key/permissions. Tests confirm Gemini-only preflight makes no request and cannot skip general/answer/Gemini permission. Cohere retrieval/reranking retains its independent requirements. |
| Malformed Gemini candidate parts could escape adapter error classification | Convert malformed shapes to sanitized InvalidAnswer; regression matrix covers missing/invalid candidates, parts, blocked/incomplete output and transport failure. |
| Vision transport/JSON failures could escape the sanitized review-draft boundary | Handle transport, JSON and malformed parts without returning private payloads. A successful vision draft remains review-required, unapproved and ineligible for embeddings. |
| An empty abstention was labelled as legacy quotation protocol | Keep passage-selection protocol metadata for empty claims; nonempty legacy drafts still require exact-source quotation validation. |

## Repository / dependency inspection

Source/tool AST inspection found no unused imported names to remove (excluding intentional package exports). Synthetic fixtures, tests, migrations and referenced release notes serve current regressions/history and were retained. The two old root storage ZIP archives, `.env`, original data, build output and validation artifacts are ignored by Git; private/build directories and archives are excluded from Docker context. None are tracked by Git. Local archives were not deleted without an explicit cleanup request. No secrets were displayed, `.env` edited, documents removed, or Git commit/push performed.

Optional adapters are not enabled merely because their code exists. [Model-routing proposal](MODEL_ROUTING.md) separates current capability from a possible standard/complex experiment. Default remains Command with Ollama operational fallback; no new model-routing feature or cloud evaluation was added.

## Verification record

- Focused provider/vision/resource tests passed; schedule audit passed **44/44**, all **47 CSV/Excel records** agree, **zero external model calls**.
- An initial concurrent full-suite/browser run failed: one index batch encountered a Qdrant collection-check transport failure, and the browser timed out during ingestion. The full run was 289 tests, one failure and no skips; no assertion was weakened. The isolated admin integration rerun passed all seven tests. Subsequent full and browser runs are executed serially; their final results follow below.
- Docker daemon diagnostics were denied by the execution environment. Local Qdrant health returned HTTP 200; this does not establish production load reliability. Intermittent service failures are retained here rather than erased by a successful rerun.
- Final serial full suite: **289 tests passed**, zero failures/errors/skips, **127.001 seconds**. Real isolated PostgreSQL/Qdrant and original-manual checks were enabled, using the native PDF backend. No new external model calls.
- Updated editable installation and source-matching wheel verified (72 packaged files, migration/browser assets included, no private artifacts). Dependency and diff whitespace checks passed.
- Final serial headless Edge browser run passed desktop/mobile upload, ingestion, source links, workspace reset, safe generated-answer rendering and schedule result rendering; zero browser errors or external requests. Answer/schedule rendering checks include synthetic response fixtures, not new live provider evaluation. Artifacts: ignored `validation/audit-0.18.1/browser/`.

This is a local regression/resource audit, not a penetration test, broad human answer-quality certification or new live Gemini/Cohere model evaluation. Source-review and company deployment limitations in the [submission guide](SUBMISSION_GUIDE.md) still apply.
