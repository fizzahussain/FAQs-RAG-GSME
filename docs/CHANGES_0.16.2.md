# 0.16.2 — Windows artifact integrity and selected-chunk review

## Markdown integrity

Windows text-mode artifact writes inserted CRLF even though the parser hashed UTF-8 LF bytes. New ingestion writes exact UTF-8 bytes. Viewing a legacy artifact accepts CRLF-to-LF conversion only if the result exactly matches the previously recorded SHA-256. Raw artifacts, recorded hashes, approvals and embeddings are unchanged. Other content changes still return HTTP 409.

The reported FAQ version was checked locally: 9,042 stored bytes contained 39 CRLF sequences; LF recovery exactly reproduced `7bd7ebe1a0a513af63235a773193ee15d2de06260bb188438a19ceb51fa3eb3b`. No resubmission is needed for that newline-only case.

## Less repetitive source review

Under Review & index, open **Review several checked chunks together**. Select individual chunks or use **Select displayed pending chunks** (current screen only; missing-link answers are skipped). Compare every selected record with the original, write one shared note describing the checked pages/records, confirm source verification and approve the selection. Rejection needs a note but not verification. Selection and verification reset when the page/version/workspace changes.

`POST /api/review/batch` accepts 1–100 distinct explicit chunk IDs from one tenant/version. It validates authorization and source confirmation, locks document then chunks in stable order, and commits all decisions or none. Every selected chunk gets its own append-only review record. Excluded evidence and generic visual-gap placeholders remain blocked. This does not approve the document automatically or bypass formula/table/OCR review.

Unselected chunks can remain pending: they do not need rejection and cannot enter the approved index. Publishing/indexing remains a separate explicit action.

## Diagnostics

Missing-link warnings no longer spread to unrelated rows on the same source page. Existing review views reconcile only that text-local warning and expose `raw_warning_codes` unchanged for inspection; stored evidence/review history and raw manifests are not rewritten. Inferred-table review flags remain in place. The original FAQ source has no recoverable destination for its Shared Block “here” answer; obtain the original URL and ingest a corrected source rather than guessing.

Health responses include the application version; versioned static assets help distinguish an old running server/browser cache. Restart the console and reload after updating. Parser/chunker identities remain 0.16.1 because extracted evidence text is unchanged.

The attached console transcript also shows failed reranking despite a successful embedding-only baseline. Local configuration currently has general external-content permission but no rerank-specific permission. Health now reports `cohere_rerank_configured`, and the search screen shows the actual sanitized failure reason rather than only a generic failure sentence. To authorize real reranking, set `GSME_RERANK_CONTENT_APPROVED=yes` yourself and restart; the per-request checkbox is still required. This release does not enable that permission or send private source content externally.

## Verification

- Full suite: **215 passed, 0 failures, 0 errors, 0 skipped**, 97.743 seconds in the final run. Real isolated PostgreSQL/Qdrant services were used; PDF tests selected native extraction. No new full-manual Docling conversion was needed for these storage/UI fixes.
- The reported existing FAQ version returned HTTP 200 through the authenticated Markdown endpoint; response bytes matched the recorded hash. Its 31 chunks, stored evidence and reviews were retained. Review display reduced four page-propagated link labels to the two actual missing-link answers while preserving all four raw labels.
- Regressions cover legacy newlines versus genuine text tampering, byte-exact ingestion artifacts, batch authorization, explicit verification, duplicate/foreign/unavailable IDs, atomic failure, per-chunk audit records, selected-only approval/rejection, and text-local link warnings.
- Headless Edge workflow passed with generated synthetic evidence: batch selection skips missing links, confirmation is mandatory, selection resets, unselected chunks remain pending, publish/index/reranked retrieval works, failed retrieval shows its reason without fallback, hyperlinks are safe, source HTML is inert, keyboard navigation and mobile layout work. Zero browser errors or external requests.
- Editable installation reports 0.16.2; dependency checks and whitespace checks pass. Real Cohere reranking remains untested in this change because the separate local permission is not enabled.
