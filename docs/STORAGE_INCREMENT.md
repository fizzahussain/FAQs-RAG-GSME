# Embeddings and storage increment

## Scope and audited baseline

The storage increment began with the 0.14.8 scaffold. The supplied `GSME_RAG_Storage_0.15.1.zip` matched the current checkout except for migration 002: the checkout already contained the required `a.attname::text` correction. That migration was already applied to the local working database and was not rewritten in this audit. Parser, chunker, review markers, Full/Shared classification, and public demo endpoints remain unchanged by 0.15.2.

The 0.15.1 tests were a non-service baseline. The actual Windows checkout had the exact validation PDF and local PostgreSQL 17.6 and Qdrant services listening. Docker API access was denied to this process, but the services were reachable directly. The already migrated working database reported both 001 and 002 applied. A separate `gsme_rag_test_codex_20261027` database was created and migrated from empty for live tests; the working database was not modified.

Baseline on this Windows checkout: **136 tests, OK, 3 skipped**, all three being real-service integration tests when test URLs were unset. The first live isolated run reproduced the psycopg `Connection.executemany` failure. After using `db.cursor().executemany()` for both evidence and schedule inserts, the live integration suite passed. The fixture also ingested, indexed, retrieved, no-oped on resubmission, revoked, and removed its synthetic point. The 0.15.2 result is in [0.15.2 changes](CHANGES_0.15.2.md); the authorized one-chunk Cohere run is in [0.15.3 changes](CHANGES_0.15.3.md).

## Decisions and alternatives

| Decision | Why | Alternative deferred |
| --- | --- | --- |
| PostgreSQL for versions, decisions, jobs and Full/Shared rows | Transactional publication, exact typed filters and a single source of truth | A file-only ledger cannot safely coordinate concurrent review/version changes. |
| Qdrant for derived evidence vectors | Dedicated vector index with tenant filter and optional native text ranking | pgvector could reduce the number of services; benchmark it if operating two stores costs more than the search benefit. |
| One local worker and bounded batches | Straightforward retry and deterministic point IDs | Kafka, Celery and Kubernetes add operations before throughput is measured. |
| Dense first; optional Qdrant text fusion | A measurable baseline before more retrieval tuning | A separate sparse embedding model or reranker is not needed for this milestone. |
| Cohere Pro documents and Fast queries at one dimension | The vendor documents a shared space and different latency/quality roles | A local model is the privacy fallback, but needs its own tokenizer, measured quality and a distinct collection. |
| Source-verified per-chunk approval | OCR, inferred tables and charts can misstate source facts | Automatic approval would permit unsupported evidence. |

## Authority and review rules

`documents.id` is the logical identity. `document_versions.source_sha256` identifies source bytes per version. `documents.current_version_id` is the only published version pointer; its composite foreign key ensures it belongs to that document. A transaction publishes a new version and supersedes the old. The 002 migration backfills 001 metadata before dropping the legacy document checksum columns. Existing 001 rows do **not** gain an inferred current version or review approval.

Each completed parse retains private `normalized.md` and `review_manifest.json` artifacts containing parser warnings and source diagnostics. The `inspect VERSION_ID --tenant TENANT` CLI shows bounded chunk text, source pages and current review state for source comparison. The original is kept under `data/originals`; the artifact and original are excluded from the deliverable ZIP.

The latest review decision is selected by monotonic `decision_order`; history remains in `review_decisions`. Every evidence chunk begins pending or excluded. Ordinary FAQ prose, inferred PDF tables, OCR, formulas and chart gaps all require an explicit source-verified approval before embedding. Generic visual-gap placeholders and excluded chunks cannot be approved. Review decisions refer to version-scoped DB chunk IDs while source chunk IDs remain available for citations. Rejection immediately removes the chunk from the authoritative view; Qdrant deletion is asynchronous cleanup, and search checks the view again.

The local `review` CLI requires an allowlisted real reviewer identifier and a prompted local authorization token. This is a local administrator tool, not an enterprise identity solution. Do not expose it through the public Render app. Synthetic fixtures are created and reviewed by test code only, never counted as real source approvals. Production approvals require the organization's actual reviewer authorization workflow and source comparison.

## Data flows

- **FAQ, DOCX, PDF, image:** local path submission -> durable private copy and streamed SHA-256 -> PostgreSQL ingestion job -> existing parser/Markdown/chunker -> pending version-scoped evidence -> source-verified review -> publish version -> bounded index job -> exact provider token count -> `prepare_candidate()` -> document embedding -> derived Qdrant point -> PostgreSQL-filtered search evidence with citation.
- **Full/Shared CSV:** local path submission -> original copy -> existing CSV parser and typed schedule validator -> typed `schedule_rows` with physical row number and Full/Shared kind -> atomic publication -> exact PostgreSQL filters. It does not need embeddings or chunks by default.
- **Replacement:** new version is processed separately. The old published version remains current until a reviewed new document version or validated new schedule version is published. Search excludes the old version immediately at publication. Reconciliation deletes stale points.

The original sync `/v1/parse`, `/v1/chunk` and `/v1/schedules/preview` demo endpoints stay limited to 20 MiB. The new local CLI `submit`/`status`/`retry`/`worker` path handles large local files; it is not mounted in FastAPI. The parser itself may still consume significant memory/CPU for large PDFs; this increment does not claim large-scale performance.

## Embeddings and index

`EmbeddingProvider` has document, query and token interfaces. Cohere requires both `COHERE_API_KEY` and `GSME_EXTERNAL_CONTENT_APPROVED=yes`. The [official Embed v5 models](https://docs.cohere.com/docs/cohere-embed) identify `embed-v5.0-pro` for indexing, `embed-v5.0-fast` for low-latency queries, a shared vector space and dimensions including 1024. The [Embed API](https://docs.cohere.com/reference/embed) supports `search_document`/`search_query`, up to 96 texts per call, `output_dimension`, float vectors and `truncate="NONE"`. The installed SDK 5.21.1 exposes those parameters. Its `tokenize(..., model="embed-v5.0-pro", offline=False)` requested provider tokenization successfully for one source-approved FAQ chunk; the [tokenize reference](https://docs.cohere.com/reference/tokenize) describes the endpoint. The worker fails closed on token-count errors rather than approximating and silently truncating. The authorized Pro document/Fast query smoke used a locally supplied key; it does not establish model quality on a representative GSME corpus.

The fake provider uses deterministic hash vectors and a real BPE tokenizer for test budget enforcement. It lives in a separate Qdrant collection suffixed `_synthetic`; its scores cannot measure semantic quality. Upserts use deterministic point IDs and a small Qdrant payload: tenant and version IDs for filtering, model version and text hash for retry checks, and text only because optional Qdrant lexical fusion needs it. Source filename, page, category, review status and citation text remain in PostgreSQL. Search reads returned point IDs, then fetches authoritative evidence in one PostgreSQL query filtered by tenant, currently published version, current approval and indexed model. Results use DB text/citations. It overfetches progressively up to 1000 Qdrant candidates to compensate for stale points. A saturated window may still yield fewer results; it never returns stale evidence.

Qdrant 1.20.0 and client 1.20.x are pinned because native BM25 scoring on a full-text payload index arrived in 1.20. Dense search is default. Optional `GSME_HYBRID=true` creates that index and uses RRF fusion of dense and lexical results on the same approved points. No separate sparse embeddings are generated. Keep the flag off until dense and hybrid are measured against the same reviewed case set.

## Local Windows PowerShell setup

### Local evidence console

The 0.15.4 operator console is a **separate** FastAPI application bound to `127.0.0.1:8010`. The existing `127.0.0.1:8000` parsing demo and its public deployment have no storage, review, or search routes. Do not reverse-proxy or expose the operator console publicly. It is a single-workstation administrator tool, not a production identity/authorization system.

After the storage dependencies and migrations below are ready, add these private lines to your existing `.env` (do not replace its working database URL):

```text
GSME_AUTHORIZED_REVIEWERS=your-local-reviewer-id
GSME_REVIEW_AUTH_TOKEN=your-random-secret-of-at-least-20-characters
```

Generate a token locally with ` .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))" ` and paste it into `.env`. Never commit `.env` or place the token in a shared screenshot. Start the console in a new PowerShell window:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.admin
```

Open `http://127.0.0.1:8010/`. Enter the token, tenant, and reviewer ID. Submit a small FAQ DOCX first, parse its job, inspect the original, Markdown, warnings, and each chunk, then approve only the claims you verified. Publish that version. Choose **Fake** and index once for a no-external-transfer plumbing test; or choose **Cohere** and explicitly authorize sending approved text. Repeat **Index one batch** until `indexed` is zero. Inspect the index-job status and the approved chunk's embedding preview (model, dimension, point ID, first eight coordinates), then search for an answerable question and inspect citations. The preview is not an explanation of what each vector coordinate means. Reject a chunk and rerun search to verify that the revoked evidence disappears; **Remove stale points** reconciles Qdrant afterward. Cohere and fake points use different collections, and a version published for one model cannot be silently moved to another collection.

The UI returns grounded evidence, not an AI-written answer. It never bulk-approves chunks; source verification and a rationale are required for each approval. Large PDFs may take a long time and consume memory. The browser upload limit is 200 MiB; use the CLI for larger sources. The console is intended for one local operator and synchronous actions; use the CLI worker for unattended processing.

Run these commands from the extracted project folder in PowerShell with Docker Desktop running and Python 3.12 or 3.13 installed:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[storage,test]"
Copy-Item .env.example .env
notepad .env
```

Set the same strong password in `GSME_POSTGRES_PASSWORD` and the URL-encoded password in `GSME_DATABASE_URL`. Keep `.env` private. For the synthetic demo, leave Cohere lines commented. Then:

```powershell
docker compose -f compose.local.yaml up -d
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe tools\demo_storage_fixture.py
```

The fixture creates a synthetic FAQ, explicitly records its synthetic source review, indexes it with fake vectors and retrieves the evidence with tenant/version/citation fields. It then revokes the approval, verifies zero search hits, and deletes the stale point. Rerunning the same document/version with `submit --document-id` is a no-op. The fake provider is a plumbing test, not evidence that GSME retrieval is good.

For a real local CSV, classify ambiguous names explicitly and process the job:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli submit .\examples\shared_schedules.csv --tenant local-test --schedule-kind shared
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli worker-once --ingestion-only
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli schedules --tenant local-test --kind shared --technology-nm 28
```

`submit` prints `job_id`; inspect with `status JOB_ID --tenant local-test`, retry a failed parse with `retry JOB_ID --tenant local-test`. `worker-once --ingestion-only --job-id JOB_ID` targets that parse job. For a real FAQ, submit and process, then run `inspect VERSION_ID --tenant local-test` and compare the original with its pending chunks and private diagnostic manifest. Have an authorized source reviewer approve specific chunks via local `review` (it prompts for the token), run `publish` with tenant, document and version IDs, then run `worker-once --provider cohere --index-only --tenant TENANT --version-id VERSION_ID` and `search`. Cohere indexing refuses an unscoped worker invocation, so turning on the local API key cannot accidentally batch every tenant. The CLI does not expose a public ingestion or review endpoint.

To run the real integration tests against an **isolated** database (name must start `gsme_rag_test`) and Qdrant 1.20.0:

```powershell
$env:GSME_TEST_DATABASE_URL = "postgresql://gsme_rag:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/gsme_rag_test"
$env:GSME_TEST_QDRANT_URL = "http://127.0.0.1:6333"
.\.venv\Scripts\python.exe -m unittest tests.test_storage_integration -v
```

Create the isolated database first. For example, `docker compose exec -T postgres createdb -U gsme_rag gsme_rag_test`. The test uses a unique Qdrant collection; it is not intended for a production database.

The original demo still runs with:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.api.run
```

Open `http://127.0.0.1:8000`. The public Render deployment must retain its existing parsing-only configuration; do not copy local `.env` there.

## Evaluation and limits

`evaluation/synthetic.json` documents the synthetic fixture format. `evaluation/gsme_reviewed_template.json` remains empty until real source evidence IDs and questions are reviewed. The evaluator refuses an empty set. On a reviewed case file, run `... storage.cli evaluate .\evaluation\your_cases.json --provider cohere` for dense and repeat with `GSME_HYBRID=true` plus `--hybrid` on the same cases. It reports Recall@5/10, failures, p50/p95 query latency, cumulative ingestion time and embedding calls, PostgreSQL table/index bytes and a lower-bound vector+text size estimate. The Qdrant footprint includes additional index and WAL overhead. Tenant/version/revocation leakage is tested by integration cases and must remain zero. Synthetic findings must be labeled synthetic.

Current limitations: local durable files are a developer adapter, not shared object storage; the local CLI uses one administrator database credential and does not implement end-user authentication; the production reviewer workflow needs an organization identity provider; parser/OCR fidelity still requires human source review; no target-corpus latency/quality or cross-store throughput has been measured. Backups, retention policy, independent worker deployment and capacity/load testing are future operational work.
