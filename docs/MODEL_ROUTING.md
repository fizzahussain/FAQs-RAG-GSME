# Model selection: current setup and optional complexity routing

## What exists

The answer provider is configured by `GSME_ANSWER_PROVIDER=cohere|gemini`. Default remains Cohere Command; an operational provider outage can use local Ollama `qwen3:4b`. Gemini is separately permission-gated and has mocked adapter tests, not live GSME answer evaluation. **There is no automatic simple/complex model router yet.**

Changing the answer model does not require re-embedding documents. Answer generation, query/document embeddings and reranking are distinct components. Keep Cohere embedding models in their shared vector space; an unrelated Gemini or Ollama embedding model is not a drop-in replacement for an existing vector collection.

## Recommended next experiment, not an enabled feature

| Request | Proposed handling |
| --- | --- |
| Exact tapeout schedules | Local typed SQL, no generation model required |
| Ordinary source-backed FAQ | Existing standard answer provider; exact source extracts also available |
| Comparison/synthesis across verified sources | Optionally select an organization-approved stronger Gemini model |
| Missing, contradictory or uncertain source evidence | Clarify/abstain; never escalate merely to obtain an answer |
| Provider outage | Existing local operational fallback, with the same evidence checks |

For a future experiment, support explicit `standard` / `complex` selection before an automatic policy. Automatic selection should use the requested operation and verified evidence requirements—not question length or an uncalibrated rerank score alone. Bound provider attempts, input/output size, latency and cost; log the selected provider/model/reason without logging private text. Do not ask a second model to overrule failed citation checks or source approval.

Evaluate standard versus stronger generation on identical approved contexts and held-out GSME questions. Human reviewers should judge correct/complete answers, unsupported claims, citations and useful abstentions; measure latency and actual provider-reported usage separately. Only introduce complexity routing if this shows a worthwhile quality/latency/cost trade-off. A larger context window must not become permission to upload the whole manual.

## Before any Google content transfer

Confirm company permission, account/service data-handling terms, available model IDs and a bounded evaluation budget. Google warns against submitting confidential, sensitive or personal information to unpaid Gemini services; a free key must not be assumed suitable for private manufacturing documents. See [official terms](https://ai.google.dev/gemini-api/terms) and [current model catalog](https://ai.google.dev/gemini-api/docs/models). Account-specific terms and available models must be checked at enablement.

The existing adapter requires general external-content permission, answer-content permission at the application boundary, separate Gemini permission, `GEMINI_API_KEY` and an explicitly configured model. Vision drafts require additional visual-content permission and remain unapproved/non-indexable. No credentials should be pasted into chat or committed. This audit did not enable routing, change `.env` permissions or send company content to Google.
