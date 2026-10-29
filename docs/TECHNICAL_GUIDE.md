# Architecture and evidence contract

Version 0.15.5. The reusable boundary for the next retrieval stage is `storage.pipeline.search_evidence()`: it returns currently approved source evidence with citations after rechecking PostgreSQL. It does not compose an answer.

## Source to evidence

`parsing/parser.py` dispatches DOCX, PDF, CSV and raster images. It hashes the source before and after extraction, rejects source changes, and records source/normalized hashes and parser version. DOCX processing retains paragraph/table order, hyperlink text, nested-table text and superscript/subscript markup. Merged cells and nested relationships require review. Plain technical symbols use NFC normalization so `x²` does not become `x2`.

Native PDF processing retains source page markers and numbered section hierarchy, removes repeated margin furniture using source positions, and flags inferred table rows. The optional Docling backend normalizes layout items, preserves available cell-loss diagnostics, reconciles conflicting OCR as review evidence, and supplements missing raster text. `strategy_used` always identifies the backend that produced the result. A fallback does not count as a successful Docling validation.

Both simple and complex native paths scan for damaged Unicode and incomplete mathematical text. Diagnostics retain the source page, bounding box, raw text, alternate OCR reading when requested, source hash and stable ID. OCR alternatives do not replace symbols or values automatically. Chart labels can be recovered without verifying their spatial associations. Local fidelity OCR has a strict region budget.

`chunking/chunker.py` consumes normalized Markdown, independently of file parsing. It selects `faq`, `paper`, `mixed` or `schedule` based on content structure. Questions styled as headings, bold text or numbered standalone blocks retain their answer and category. Question/Answer table aliases and additional source columns are retained. Technical manuals with incidental questions keep section context. Table fields retain their column labels, and an exceptional oversized unit receives an explicit warning and review flag.

The normal chunk budget is 1,800 characters. The embedding budget is a separate provider-token limit. Split FAQ answers repeat their question and share `unit_id`, `part_index` and `part_count`. Source file/hash, pages, heading path, category and table row remain available to downstream code. Changes in parser/chunker versions or normalized text create new chunk identities.

Fenced source code retains indentation and literal page/review markers. An oversized code unit stays intact for review instead of having its syntax silently rewritten.

## Review and storage

`documents.id` is a logical document identity. `document_versions` contains each source checksum, parser setup, original path and normalized artifact hash. Submission copies and hashes source bytes before queuing a durable ingestion job. Same-source submission to an existing document is a no-op only when parser/chunker versions, classification and extraction setup also match.

Workers claim bounded jobs with leases and heartbeat while parsing or embedding. Ingestion attempts use separate artifact directories so a stale attempt cannot overwrite the files referenced by a newer attempt. Chunks retain their source ordinal; inspection is paginated in source order. Legacy rows without an ordinal are explicitly shown as having unknown order. Reparse into a new version to obtain the new provenance fields.

`review_decisions` is an append-only application history. The latest decision is chosen by sequence, not timestamps. All semantic evidence needs source-verified approval. Excluded navigation, source furniture and generic visual placeholders cannot be approved. A database trigger prevents source text/provenance from being rewritten under an earlier approval. Publication changes the document's current-version pointer in the same transaction that supersedes old index jobs. Review/publication use the same document-before-chunk lock order as embedding and cleanup.

Full/Shared schedule CSVs use `schedule_rows` with integer technology and typed dates. Physical CSV row numbers survive quoted multiline records and blank lines. Exact retrieval filters typed columns, preserving repeated business rows. Generic FAQ CSVs take the document-evidence path instead.

## Embeddings and derived index

Cohere Pro indexes approved documents and Fast embeds queries in their shared vector space. The default dimension is 1,024. `COHERE_API_KEY` and `GSME_EXTERNAL_CONTENT_APPROVED=yes` are required for external calls. Token counting happens after approval is rechecked; requests use `truncate="NONE"`. Finite, nonzero vectors and matching dimensions are required. See [Cohere model documentation](https://docs.cohere.com/docs/cohere-embed) and [Embed API](https://docs.cohere.com/reference/embed).

Fake vectors are deterministic workflow fixtures in a separate `_synthetic` collection. Their rankings do not measure semantic quality. Qdrant uses cosine distance, deterministic version-scoped point IDs and tenant/model filters. PostgreSQL owns citation text, source identity and review state. Index retries reuse an existing point only when model and text hash match. Oversized candidates fail individually without blocking healthy neighbors; automatic retries are capped at five attempts. Correct the cause and republish to retry failed jobs. A publish during active indexing preserves that running job.

`search_evidence()` checks that approved indexed evidence exists before calling an embedding provider. It overfetches Qdrant candidates and rechecks every hit against current PostgreSQL tenant, version, approval, model and collection state. It returns source text, file/hash, document/version/chunk IDs, heading path, pages, table row, split identity and review provenance. Revoked or superseded evidence is filtered immediately; reconciliation removes stale derived points afterward. Dense search is the default. Optional Qdrant lexical fusion should be evaluated on the same reviewed questions before adoption.

`repair_missing_vectors()` compares scoped approved index jobs with Qdrant and queues missing points. It makes no embedding call itself; the normal worker still checks approval and external permission. Source-reviewed evaluation cases must identify expected evidence by version ID, chunk ID and source hash.

## Application boundaries

The local console serves all document preparation and storage operations at `127.0.0.1:8010`. Its token, reviewer allowlist, origin checks and loopback bind are workstation controls. The public parsing demo has no review or storage routes. Docker copies only source/package files and excludes local secrets and data. PostgreSQL and Qdrant Compose ports bind to loopback.

Migration SQL and browser assets are package resources, so a wheel install includes them. Migrations apply in order under an advisory lock. Never edit an applied migration to change its behavior; add a forward migration.

Source review of complex formulas/tables, a representative GSME question set, production identity, backups, retention and load testing remain necessary before broader deployment. The current audit records which claims were actually checked.
