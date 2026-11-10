# 0.16.3 — ingest ordinary documents without review forms

Normal console flow is **Ingest document → parse → accept clean text → publish/embed → Ask sources**. No reviewer, rationale, per-chunk checkbox or separate publish button is required for ordinary clean text. Real Cohere embedding still requires external-content permission and the document-level consent checkbox. Fake mode remains local and nonsemantic. Full/Shared schedule CSVs continue through exact typed storage without embeddings.

## Automatic acceptance, not fabricated human review

Migration 004 adds `approval_basis` to the separate decision history. Existing decisions remain `human_source_review`. New clean-text decisions use `automatic_clean_text_v1`, `reviewer=ingestion-policy` and `source_verified=false`. Their automatically generated rationale describes policy acceptance and never claims a person checked the PDF. The index adapter and retrieval citations preserve that distinction.

The policy accepts only nonempty section, FAQ-pair or table-row text with no review flag or warning. OCR, uncertain/inferred or merged tables, chart evidence, broken Unicode, suspicious mathematical text, code blocks, missing links and generic visual gaps are not accepted automatically. The policy is conservative; it is not a guarantee of factual correctness. Native PDF FAQ tables marked inferred remain held, not silently promoted to trusted records.

The original source and normalized artifact hashes are checked before policy acceptance. Decisions are version/tenant scoped, transactional and idempotent. No earlier human approval/rejection is overwritten. Human review can revoke automatic acceptance, which immediately filters search results. The database eligibility view, publication and candidate preparation all enforce the distinction; automatic decisions cannot carry `source_verified=true`.

## Interface and API

`POST /api/ingest-ready` takes the authenticated tenant/document/version/provider scope, accepts eligible text and publishes/indexes up to twenty bounded worker batches. It returns accepted/held/excluded counts, index failures and remaining work. Zero eligible text returns a held result, not a false searchable success. Failed or deferred jobs remain retryable/inspectable; repeat ingestion does not duplicate decisions or vectors. The initial source submission/parsing endpoints remain separate for durable job tracking.

The console connects these steps behind **Ingest document**, opens search when eligible text is ready, and shows partial-ingestion counts. Existing documents have **Ingest clean text (no review notes)**. Advanced **Parse only** and manual exception review remain available. The low-level CLI worker does not automatically change existing review decisions.

Parser/chunker identities remain 0.16.1 because source extraction did not change. Apply migration 004 and restart the console; no source reparse is required merely to use the new ingestion policy. No existing private document has been automatically accepted or sent to Cohere as part of development/testing.

## Operation logs and actionable rerank configuration

The console now reports the exact missing setting names rather than a generic approval failure. Check `COHERE_API_KEY`, `GSME_EXTERNAL_CONTENT_APPROVED=yes` and the separate `GSME_RERANK_CONTENT_APPROVED=yes`. Edit the private `.env` yourself and restart; existing PowerShell environment values take precedence over `.env`. The per-request send-content checkbox remains required. A blocked preflight sends no rerank request and never returns an unreranked fallback.

Starting `python -m gsme_rag.storage.admin` writes content-free stage events to PowerShell and `GSME_DATA_DIR/logs/console.log` (default `data/logs/console.log`). Rotation bounds the files to four approximately 2 MB files. `GSME_LOG_LEVEL=INFO` is the default. Events cover parser backend/warnings, chunking, acceptance counts, indexing, query embedding, candidate search, reranking and database/vector-service failures. They include counts, durations, stage names and exception types, not raw exception payloads, questions, document text, review rationales, API keys or database URLs.

Each API response carries a server-generated `X-GSME-Request-ID`; the UI includes it in errors and retrieval reports use the same ID. Match that ID in the terminal/file to find the failing stage. **Connection details & logs → Recent operation logs** reads the last 80 entries through authenticated `/api/logs` (maximum 200); no arbitrary file path is accepted. Logs are local/ignored, not Git artifacts.

## Verification

Final full suite: **222 passed, 0 failures, 0 errors, 0 skipped**, 111.729 seconds. Tests use isolated real PostgreSQL/Qdrant services and native PDF extraction. The no-notes ingestion regression accepts the clean answer, holds the missing link and incomplete formula, preserves honest automatic provenance, rejects foreign scope and respects later revocation. Logging tests cover privacy, request correlation, exact permission errors, authenticated bounded log viewing and file rotation.

The headless Edge workflow passed upload/parse, optional manual batch review, automatic ingestion without notes, publish/index, mandatory synthetic reranking, explicit failure display, safe links/HTML, keyboard tabs, workspace reset and mobile layout. Zero browser errors or external requests. Editable installation is 0.16.3; dependency and whitespace checks pass. Migration 004 was applied locally without changing existing review decisions. No live Cohere rerank test was performed because the local rerank-specific permission remains unset.

## Test hygiene

Disposable synthetic test collections had accumulated in local Qdrant. Only collections linked to the isolated test database and matching known random test namespaces were removed (77); user collections and originals were untouched. The deleted synthetic vectors can be regenerated by their fixtures. New integration/browser cleanup removes only each run's own randomly named derived collection.
