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

Open **http://127.0.0.1:8010/**. The server loads `.env` at startup, so restart it after editing token, reviewer or provider settings. Migrations include 003 for source ordering/immutable evidence/retry state, 004 for automatic clean-text acceptance provenance, and 005 for sheet-aware workbook schedules. They are packaged under `src/gsme_rag/storage/migrations/`. [Exact schedule search and CLI](SCHEDULE_SEARCH.md) and [submission checklist](SUBMISSION_GUIDE.md) describe the 0.18.0 increment.

## Normal use: find sources

In 0.16.4 the home page is a question box and cited source matches. Connect with your local token, then search. **Documents** opens the compact upload/library panel. **Settings & help** contains provider selection, logs and advanced tools; it is not necessary to inspect individual chunks to use clean evidence. Real Cohere mode needs the configured external/rerank permissions and visible content consent. Local demo rankings are not semantic quality results.

The main result is a source extract, not a generated answer. Up to 30 eligible passages are reranked and up to 5 matches are displayed. The actual count appears after search. Review-held content stays excluded. Use `/admin` only when handling an extraction exception or inspecting the pipeline. [Current verification and remaining gaps](CHANGES_0.16.4.md).

Since 0.16.5, the optional **Also generate a cited answer** checkbox enables Command-first generation with local Qwen/Ollama operational fallback. It needs separate `GSME_ANSWER_CONTENT_APPROVED=yes`, not just rerank permission; see [answer setup and limits](CHANGES_0.16.5.md). Without that opt-in the existing source-extract workflow is unchanged. Missing evidence still leads to abstention, not a second model guessing an answer.

For the locally evaluated corpus, use workspace `local-test` and collection `gsme_evidence_v1`. A PowerShell `$env:GSME_COLLECTION` override from an older synthetic demo takes precedence over `.env`; remove that override or set the intended collection before starting the server. Other tenant/collection/provider combinations do not automatically expose this corpus.

## Advanced evidence console (`/admin`)

The advanced console retains four screens: **Documents**, **Review & index**, **Ask sources**, **Schedules**. The normal path is **Ingest document → Ask sources**, not review of every chunk. Clean text is accepted automatically; extraction problems stay stored but out of search. See [automatic ingestion](CHANGES_0.16.3.md). Detailed IDs and manual review are advanced tools.

Mandatory reranking needs no new SQL migration. See [retrieval setup and evaluation](RETRIEVAL.md), including the separate `GSME_RERANK_CONTENT_APPROVED` permission. The old unreranked search remains a labelled diagnostic baseline. Ingest a new version to apply the 0.16.1 hyperlink/parser changes to older documents; old chunks are not modified in place.

1. Enter the token and workspace in **Connection & workspace** and connect. A reviewer name is needed only for manual exception review. Choose **Local demo** or **Cohere** as the working mode.
2. In **Documents**, choose a file and click **Ingest document**. It parses, accepts eligible clean text, publishes and indexes it. Cohere needs permission in `.env` and the visible document-level send-content checkbox. No review rationale is needed. To replace a logical document, supply its ID under replacement details first.
3. Read the accepted/held counts. Searchable text leads directly to **Ask sources**. Held content stays out of search; it can remain unresolved without rejecting every chunk. Inferred PDF/Docling table rows remain held even when other paragraphs are clean. Use a faithful unmerged DOCX FAQ table or corrected original where available; do not silently clear extraction flags.
4. Optional: inspect extraction issues in **Review & index** and manually approve only what you checked, with a review note. **Parse only** retains the strict manual workflow. **Ingest clean text (no review notes)** applies automatic ingestion to an existing parsed version without overriding any earlier review. Processing is bounded to twenty batches per request; failed/future-due jobs stay visible and can be continued with the ingestion button or scoped worker.
5. In **Ask sources**, enter a question and search approved evidence. Cohere asks for per-request external confirmation. Open source details or ranking diagnostics when needed. Clickable URLs are source links, not ingested website content. Results are evidence candidates, not generated answers or confidence percentages. Embedding previews remain available from eligible indexed review cards; coordinates do not explain semantic meaning.
6. Reject a chunk to revoke it; search excludes it immediately. Advanced **Clean stale vectors** removes the stale point. Publishing a new version similarly removes the previous version from authoritative search immediately.

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

For stage logs, start the server normally and use **Settings & help → Recent logs** on the home page (or the advanced console's connection/log controls), or watch `data/logs/console.log` in PowerShell:

```powershell
Get-Content .\data\logs\console.log -Tail 50 -Wait
```

Logs identify stages, counts, durations and request IDs without recording source text or credentials. Reranking preflight names the missing settings. If you authorize Cohere reranking, add `GSME_RERANK_CONTENT_APPROVED=yes` to your private `.env`, preserve the existing API key/general permission, and restart. A browser checkbox alone does not enable the server setting.

Release 0.16.2 fixes Windows newline-only Markdown 409s and supports explicit selected-chunk batch review. See [the release notes](CHANGES_0.16.2.md). You can leave unverified chunks pending; only approved chunks are indexed. Batch approval still requires checking every selected source record and leaves a separate audit decision for each.

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
