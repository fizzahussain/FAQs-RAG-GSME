# Storage and local console runbook

## Setup

Install `.[storage,test]` into the project virtual environment. For a new installation copy `.env.example` to `.env`. For an update preserve the existing `.env` and Docker volumes.

Use the same PostgreSQL password in `GSME_POSTGRES_PASSWORD` and `GSME_DATABASE_URL`; URL-encode the password in the URL. Set `GSME_AUTHORIZED_REVIEWERS=HP` or your own reviewer identifier, and set `GSME_REVIEW_AUTH_TOKEN` to a random secret of at least 20 characters. Generate one locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Keep these values private.

Start Docker Desktop, wait for its Linux engine, and run each PowerShell line separately:

```powershell
docker compose -f compose.local.yaml up -d
docker compose -f compose.local.yaml ps
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m gsme_rag.storage.admin
```

Open **http://127.0.0.1:8010/**. The server loads `.env` at startup, so restart it after editing token, reviewer or provider settings. Upgrade 0.15.5 adds migration 003 for source ordering, immutable evidence and retry state. Migrations are packaged under `src/gsme_rag/storage/migrations/`.

## Local evidence console

1. Enter the token, workspace `local-test`, and the exact reviewer ID configured in `.env`. Check connection to verify PostgreSQL and Qdrant are reachable.
2. Upload a small FAQ DOCX, PDF or `examples/synthetic_reviewed_faq.docx`. Click **Parse uploaded job**. For a new version of an existing document, supply its document ID before upload.
3. Load the chunks, download the original and inspect Markdown/warnings. Navigate through chunks in source order. For each chunk you checked, write a review note describing the page/paragraph and result, tick that chunk's source-check box, and approve. Reject uncertain content. A missing source answer must stay missing.
4. Select Fake vectors for a workflow test or Cohere for real embeddings. Click **Publish approved version**; this queues only source-approved evidence. For Cohere also enable external permission in `.env` and tick the send-content box. Click **Index one batch**, then inspect job states. Repeat while queued jobs remain. Zero newly indexed does not prove all jobs succeeded: failures, retries and next-attempt times are displayed with each chunk.
5. Embedding preview becomes available for a currently approved indexed chunk with the selected provider. It shows model, point identity, dimension and a short coordinate sample. Those coordinates do not explain semantic meaning. Search an answerable question and check its returned text and source citation.
6. Reject an approved chunk to revoke it. Search immediately excludes it. **Clean stale vectors** removes the stale Qdrant point. Publishing a new document version similarly removes the previous version from authoritative search immediately.

If PostgreSQL says a chunk is indexed but its Qdrant point was lost, use **Check for missing stored vectors**. It requeues missing points in the selected tenant/version/provider; the separate Index action recreates them after the normal approval checks.

The console's `/docs` exposes the same local authenticated API. It returns evidence rather than generated answers. The parsing demo on port 8000 runs independently; use `python -m gsme_rag.api.run` if that smaller demo is needed.

## Schedules

Schedule CSVs must contain the known six columns. A generic schedule filename requires Full/Shared selection. The typed rows are published transactionally after validation and are available through **Exact schedule search**. They do not need semantic chunks. Generic CSVs with Question/Answer columns use the ordinary review/embedding workflow.

## CLI

The CLI exposes `submit`, `status`, `retry`, `inspect`, `review`, `publish`, `worker-once`, `worker`, `search`, `schedules`, `reconcile`, `repair-index` and `evaluate`. Use `python -m gsme_rag.storage.cli COMMAND --help` for exact arguments. Inspection supports `--offset` and `--limit`. Cohere workers require an explicit tenant and version ID. Continuous workers can run outside the browser for long tasks.

Private originals live under `data/originals/`; each attempt's normalized Markdown and review manifest live under `data/artifacts/VERSION/ATTEMPT/`. PostgreSQL records their hashes and active paths. Do not edit approved evidence in place: repair the source and submit a new version. If parser versions, dependencies or backend setup changed, resubmit into a new version instead of reusing an old queued job.

## Verification

The full suite does not make external embedding calls. Set service-test URLs before starting Python to enable integration tests. The test database must be isolated and have a name starting with `gsme_rag_test`.

```powershell
docker compose -f compose.local.yaml exec -T postgres createdb -U gsme_rag gsme_rag_test
$env:GSME_TEST_DATABASE_URL = "postgresql://gsme_rag:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/gsme_rag_test"
$env:GSME_TEST_QDRANT_URL = "http://127.0.0.1:6333"
$env:GSME_PDF_BACKEND = "native"
# Optional: point source-specific checks at your private original manual.
$env:GSME_TEST_MANUAL_PDF = "D:\Downloads\MFG-GSME-0002.pdf"
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Create the test database once. Integration tests use distinct synthetic tenants/collections. `tools/demo_storage_fixture.py` is a separate synthetic indexing/revocation smoke; `--provider cohere` sends only its newly generated synthetic FAQ and requires the configured external permission. `tools/validate_pdf_backend.py` records a native or real Docling run; inspect `strategy_used`. `tools/audit_sources.py` can audit supplied documents into ignored local output.

## Troubleshooting

| Message | Next action |
| --- | --- |
| Docker Linux-engine pipe missing | Start Docker Desktop and wait for the engine |
| PostgreSQL unavailable/password rejected | Check services and the existing private URL; changing `.env` alone does not reset a volume's password |
| Reviewer not authorized | Match the reviewer field to the allowlist and restart after editing `.env` |
| No approved indexed vector | Approve, publish and index the chunk with the selected provider |
| Indexed zero | Inspect queued/failed jobs and retry timing; it may already be indexed |
| Token budget exceeded | Preserve evidence; fix source/chunking or the configured budget, then republish |
| Stored source or Markdown changed | Restore the original or submit a new version; approval cannot cover different bytes |

For real Cohere calls, set `COHERE_API_KEY` and `GSME_EXTERNAL_CONTENT_APPROVED=yes` locally. The earlier one-chunk provider smoke is limited evidence of API compatibility; a representative reviewed GSME evaluation set is still needed for quality claims. The empty `evaluation/gsme_reviewed_template.json` is deliberately not a benchmark. Production identity, backups, retention and capacity testing are additional operational work.
