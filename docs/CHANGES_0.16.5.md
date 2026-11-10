# 0.16.5 — Command-first answer generation with local Ollama fallback

The optional answer-generation path is now separate from embeddings and mandatory reranking. `POST /api/retrieve` accepts `generate_answer=true` and `confirm_answer_external=true` for real document retrieval. The home page has an explicit answer-generation consent checkbox; ordinary retrieval remains available unchanged. Exact schedule rows and synthetic search cannot be passed through real generation.

## Provider configuration

Preserve the private `.env`. Enable the separate generation permission only after company authorization:

```dotenv
GSME_ANSWER_CONTENT_APPROVED=yes
GSME_ANSWER_MODEL=command-a-plus-05-2026
GSME_OLLAMA_MODEL=qwen3:4b
GSME_ANSWER_TIMEOUT=60
```

The existing Cohere API key and general external-content permission remain required. No private permission or key was changed by this release. Restart the local server after configuration changes. Ollama must already be running with the configured local model; the application does not download one. The local endpoint is fixed to `http://127.0.0.1:11434`; proxy environment variables and redirects are disabled. Operators must keep Ollama itself local-only; this is not a network-egress sandbox. The installed `nomic-embed-text` model does not replace existing Cohere embeddings.

Primary: Cohere Command. Fallback: `qwen3:4b`. Network failures, unavailable/access-denied primary models, rate limits and transient service errors can trigger fallback. A configuration/permission denial, malformed generation, invalid citation, truncated answer or model-declared insufficiency cannot trigger model hopping. At most one call per provider is attempted. Missing evidence produces a deterministic insufficiency message without a model call. If both providers are unavailable, the response preserves source retrieval and reports answer unavailability.

**This is an answer-generation fallback, not an offline retrieval implementation.** Cohere query embedding/reranking failure still fails explicitly; Ollama cannot bypass mandatory reranking. The worst-case sequential generation timeout is approximately twice the configured provider timeout, plus retrieval/network overhead.

## Evidence boundaries

Only complete, server-selected approved retrieval context is supplied. The shared prompt budget is conservatively bounded to 6,000 UTF-8 bytes for local 8K context, including instructions/schema/question. Oversized passages are omitted whole with warnings, never shortened. This may limit source coverage and causes some questions to abstain; no claim of optimal compression is made.

Both providers return structured decisions and claims with exact supporting quotes and source IDs. The application rejects unknown IDs, changed quotes, missing claim support, invented numeric values/explicit URLs absent from supporting quotes, inconsistent status and incomplete output. Source approval/current-version state is checked before provider sends and after generation. Citations retain source hash, version/chunk identity and available pages. The UI renders generated text inertly and labels the provider/fallback.

These checks establish **citation integrity, not semantic entailment or factual correctness**. A valid quote can still be irrelevant to a claim; a model may misjudge answerability or follow malicious source instructions despite prompt separation. Human-reviewed groundedness, adversarial and cross-document evaluations remain required. The application never promises a factual answer to every question.

Provider references: [Cohere Chat structured outputs](https://docs.cohere.com/v2/reference/chat) (JSON `response_format` cannot be combined with the `documents` parameter), [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs), [Ollama thinking controls](https://docs.ollama.com/capabilities/thinking). Qwen thinking output is disabled and never used as answer text.

## Verification scope

Final full suite on 10 November 2026: **244 tests passed, zero failures/errors/skips**, unittest elapsed **158.998 seconds**. Isolated PostgreSQL, real Qdrant and the configured private-manual checks were enabled; the suite made no external model calls. Editable installation reports 0.16.5, `pip check` and `git diff --check` passed.

Focused tests cover primary success, operational fallback, no model hopping on abstention/validation failures, both providers unavailable, quote/source/numeric/URL integrity, revocation before/after calls, bounded prompts, local transport settings, API approval preflight and the opt-in API wiring. Browser checks passed for the source-search/admin workflows plus generated-answer display, source citation anchors, inert HTML, mobile layout and disconnect, using only synthetic fixtures and no external requests.

The first real local Qwen synthetic smoke exceeded the 60-second timeout. A repeated warmed-up smoke answered with a locally validated supporting quote/citation in **15.8 seconds**; a separate unrelated personal-bank-balance question correctly returned `insufficient_evidence` with zero claims in **7.01 seconds**. Ollama `/api/ps` reported `size_vram=0` and an 8,192-token context at observation time. These are two-question synthetic runtime observations, not a quality certification or an SLA. Cold-start latency remains a limitation. Live Command/private-source testing remains pending the explicitly requested document/call/cost approval; no company content was sent to a new answer provider during this increment.
