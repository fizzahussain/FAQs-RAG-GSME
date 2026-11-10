# Architecture and evidence contract

Version 0.18.0. The reusable retrieval boundary is `retrieval.service.retrieve()`: it searches approved evidence, mandates reranking, assembles bounded source-linked context and rechecks PostgreSQL. `retrieval.schedules.search_schedules()` supplies the separate local typed schedule path. `storage.pipeline.search_evidence()` remains an explicitly unreranked diagnostic baseline. Optional `generation.answers` rechecks source state and uses Command with local Ollama operational fallback. Old approved versions stay searchable. [Schedule contracts](SCHEDULE_SEARCH.md) and [document strategies/verification](CHANGES_0.17.0.md).

## Source to evidence

`parsing/parser.py` dispatches DOCX, PDF, CSV and raster images. It hashes the source before and after extraction, rejects source changes, and records source/normalized hashes and parser version. DOCX processing retains paragraph/table order, hyperlink text, nested-table text and superscript/subscript markup. Merged cells and nested relationships require review. Plain technical symbols use NFC normalization so `x²` does not become `x2`.

`parsing/links.py` retains actual DOCX hyperlink targets and PDF URI annotations in source-linked Markdown. Ambiguous PDF placement remains reviewed with page/bounding-box context. Unresolved “here” references are flagged, not assigned guessed URLs. Source links are not crawled. The console renders safe HTTP(S)/mailto anchors and keeps untrusted HTML inert; see [0.16.1 changes](CHANGES_0.16.1.md).

Native PDF processing retains source page markers and numbered section hierarchy, removes repeated margin furniture using source positions, and flags inferred table rows. The optional Docling backend normalizes layout items, preserves available cell-loss diagnostics, reconciles conflicting OCR as review evidence, and supplements missing raster text. `strategy_used` always identifies the backend that produced the result. A fallback does not count as a successful Docling validation.

Both simple and complex native paths scan for damaged Unicode and incomplete mathematical text. Diagnostics retain the source page, bounding box, raw text, alternate OCR reading when requested, source hash and stable ID. OCR alternatives do not replace symbols or values automatically. Chart labels can be recovered without verifying their spatial associations. Local fidelity OCR has a strict region budget.

`chunking/chunker.py` consumes normalized Markdown, independently of file parsing. It selects `faq`, `paper`, `mixed` or `schedule` based on content structure. Questions styled as headings, bold text or numbered standalone blocks retain their answer and category. Question/Answer table aliases and additional source columns are retained. Technical manuals with incidental questions keep section context. Table fields retain their column labels, and an exceptional oversized unit receives an explicit warning and review flag.

The normal chunk budget is 1,800 characters. The embedding budget is a separate provider-token limit. Split FAQ answers repeat their question and share `unit_id`, `part_index` and `part_count`. Source file/hash, pages, heading path, category and table row remain available to downstream code. Changes in parser/chunker versions or normalized text create new chunk identities.

Fenced source code retains indentation and literal page/review markers. An oversized code unit stays intact for review instead of having its syntax silently rewritten.

## Review and storage

`documents.id` is a logical document identity. `document_versions` contains each source checksum, parser setup, original path and normalized artifact hash. Submission copies and hashes source bytes before queuing a durable ingestion job. Same-source submission to an existing document is a no-op only when parser/chunker versions, classification and extraction setup also match.

Workers claim bounded jobs with leases and heartbeat while parsing or embedding. Ingestion attempts use separate artifact directories so a stale attempt cannot overwrite the files referenced by a newer attempt. Chunks retain their source ordinal; inspection is paginated in source order. Legacy rows without an ordinal are explicitly shown as having unknown order. Reparse into a new version to obtain the new provenance fields.

`review_decisions` is an append-only application history. The latest decision is chosen by sequence, not timestamps. Migration 004 distinguishes human source review from automatic clean-text policy acceptance; automatic decisions have `source_verified=false`. Ordinary ingestion needs no human forms. Flagged OCR, uncertain tables, formulas, charts, damaged text and missing links stay out of automatic indexing. Human rejection is never undone by ingestion. Excluded navigation, source furniture and generic visual placeholders cannot be approved. A database trigger prevents source text/provenance from being rewritten under an earlier acceptance. Publication changes the document's current-version pointer in the same transaction that supersedes old index jobs. Acceptance/review/publication use the same document-before-chunk lock order as embedding and cleanup. See [0.16.3](CHANGES_0.16.3.md).

Full/Shared schedule CSV/XLSX uses `schedule_rows` with positive integer technology and typed ordered milestones. Physical CSV rows survive quoted multiline records and blank lines. Workbook rows retain their sheet and per-row kind; migration 005 makes the primary key version/sheet/row. Exact SQL preserves repeated business rows. Local planning resolves supported calendar/filter grammar or requests clarification. Count/results/source lists share a repeatable-read snapshot, with source/filter-bound pagination tokens. Generic FAQ files take the document path instead. See [schedule contracts](SCHEDULE_SEARCH.md).

## Embeddings and derived index

Cohere Pro indexes approved documents and Fast embeds queries in their shared vector space. The default dimension is 1,024. `COHERE_API_KEY` and `GSME_EXTERNAL_CONTENT_APPROVED=yes` are required for external calls. Token counting happens after approval is rechecked; requests use `truncate="NONE"`. Finite, nonzero vectors and matching dimensions are required. See [Cohere model documentation](https://docs.cohere.com/docs/cohere-embed) and [Embed API](https://docs.cohere.com/reference/embed).

Fake vectors are deterministic workflow fixtures in a separate `_synthetic` collection. Their rankings do not measure semantic quality. Qdrant uses cosine distance, deterministic version-scoped point IDs and tenant/model filters. PostgreSQL owns citation text, source identity and review state. Index retries reuse an existing point only when model and text hash match. Oversized candidates fail individually without blocking healthy neighbors; automatic retries are capped at five attempts. Correct the cause and republish to retry failed jobs. A publish during active indexing preserves that running job.

`search_evidence()` checks that approved indexed evidence exists before calling an embedding provider. It overfetches Qdrant candidates and rechecks every hit against current PostgreSQL tenant, version, approval, model and collection state. It returns source text, file/hash, document/version/chunk IDs, heading path, pages, table row, split identity and review provenance. Revoked or superseded evidence is filtered immediately; reconciliation removes stale derived points afterward. Dense search is the default. Optional Qdrant lexical fusion should be evaluated on the same reviewed questions before adoption.

`repair_missing_vectors()` compares scoped approved index jobs with Qdrant and queues missing points. It makes no embedding call itself; the normal worker still checks approval and external permission. Source-reviewed evaluation cases must identify expected evidence by version ID, chunk ID and source hash.

## Application boundaries

The authenticated `/api/retrieve` endpoint and CLI `retrieve` support dense/hybrid candidate strategies followed by mandatory Cohere reranking (or paired synthetic providers for tests). PostgreSQL scope IDs prefilter Qdrant; exact document/category/filename facets cannot be silently lost behind unrelated hits. Same-unit expansion never imports arbitrary unapproved neighbors. Diagnostic text is revalidated too if approval changes during the rerank call. Routes for exact schedules and mixed requests keep SQL rows separate from ranked passages. See [the retrieval contract and benchmark workflow](RETRIEVAL.md).

The source-search home is `127.0.0.1:8010/`; document preparation and exception-review controls remain at `/admin`. Normal search sends at most 30 eligible passages to reranking and displays up to 5 cited matches; `rerank_input_count` reports the actual count after eligibility/deduplication. Its token, reviewer allowlist, origin checks and loopback bind are workstation controls. The public parsing demo has no review or storage routes. Docker copies only source/package files and excludes local secrets and data. PostgreSQL and Qdrant Compose ports bind to loopback.

Migration SQL and browser assets are package resources, so a wheel install includes them. Migrations apply in order under an advisory lock. Never edit an applied migration to change its behavior; add a forward migration.

Source review of complex formulas/tables, a representative GSME question set, production identity, backups, retention and load testing remain necessary before broader deployment. The current audit records which claims were actually checked.
