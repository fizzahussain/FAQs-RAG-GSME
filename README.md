# GSME Muse

Local-first, source-grounded FAQ, manufacturing-document and tapeout-schedule assistant.

## Start locally (Windows PowerShell)

Use Python 3.11+ and Docker Desktop with Linux containers. From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "pip>=26.2"
.\.venv\Scripts\python.exe -m pip install -e ".[storage,hosted,test]"
Copy-Item .env.example .env  # First setup only; never overwrite an existing .env.
docker compose -f compose.local.yaml up -d
.\.venv\Scripts\python.exe -m gsme_rag.hosted.app
```

Configure the local database URL/password and the required provider keys in your real `.env` before launching. Never commit it. The launcher applies additive database migrations.

- **Chatbot:** http://127.0.0.1:8020
- **Operator console:** run `.\.venv\Scripts\python.exe -m gsme_rag.storage.admin`, then open http://127.0.0.1:8010/admin
- **Operator source-search screen:** http://127.0.0.1:8010
- **Parsing-only preview:** `python -m gsme_rag.api.run` is a separate application, not the complete chatbot.

If a port is occupied, use the existing server or stop its original terminal before restarting. Do not start a second copy.

## Admin workspace

Open **http://127.0.0.1:8010/admin** after starting the operator console. Connect once using your local operator token; keep this service private.

- **Documents:** find a source by name, add a file, or click **Update** to preserve its logical identity. Version history and IDs are tucked away. Shared publication always needs explicit confirmation.
- **Source review:** starts with **Needs review**, not every accepted/excluded chunk. Filter by source page, compare the original PDF/image beside the text, select the displayed page/table passages and record one checked-source note for the group. A page shortcut selects only displayed pending passages, skips missing-link answers, and never approves unseen records.
- **Jobs & logs:** see recent parsing/indexing work, stored failure reasons and enrolled automatic retry times. Open a job's document without copying IDs. Failed local parsing can be retried separately; no embedding is authorized by refreshing jobs or retrying local parsing.
- **Test search:** inspect retrieved evidence and before/after reranking positions. Real provider requests still need explicit content permission; **Local demo** is clearly marked as synthetic.
- **Schedules:** inspect exact rows in a readable table, with the raw response available for provenance.

**FastAPI / Swagger:** use **API reference** in the sidebar, or open http://127.0.0.1:8010/docs. Protected API calls require the operator token in the `X-GSME-Token` header; the admin page supplies it after you connect. Office files need Word/Excel or a downloaded original because the browser has no native Office preview.

## What searches what?

**Chat** uses dense semantic vectors plus BM25 keyword search, rank fusion, Cohere reranking, and cited answers. Hosted chat explicitly enables hybrid retrieval. The legacy `GSME_HYBRID` flag controls other entry points; it does not turn hosted hybrid off. Default candidate/result limits are 30 and 5; source context can be added separately. Relevance scores are not correctness probabilities.

**Tapeout schedules** uses exact PostgreSQL records: Full/Shared kind, technology, exact flavor, milestone dates and calendar filters. No LLM invents a tapeout date. These are imported source snapshots, not live booking availability.

Example schedule questions:

- `Show both schedules`
- `Show shared 28nm schedules flavor "HPC+ RF" between 2026-11-01 and 2026-12-31`
- `Show shared final deadlines before 2026-11-01`
- `Next 3 full tapeout dates`

Relative dates depend on the displayed as-of date and timezone (default Asia/Karachi). Select one milestone per date lookup. Conflicting or unsupported constraints request clarification.

## Ingest and update documents

The chatbot **+** uploads a new owner-only document to its browser workspace. Parsing and approved-text indexing advance automatically. Wait for Ready to chat. Supported inputs: PDF, DOCX, PPTX, XLSX, CSV and images, up to the hosted upload limit of 20 MiB.

**A filename is not an identity.** Uploading the same filename through + is not a shared-library replacement.

To update a shared FAQ, PDF or schedule:

1. Open the authenticated local operator console and select the current document.
2. Click **Update selected document**. This preserves its logical document ID.
3. Choose the replacement file and ingest. Pick Full/Shared for a schedule CSV.
4. Check the extracted text/rows. Authorize external processing only when permitted.
5. Explicitly confirm and click **Publish shared version** for that exact current version.

New questions/rows in an updated source are included in its replacement snapshot. Removed rows stop appearing in current schedule search. Previous versions/originals remain for provenance. Identical content with the same document ID and extraction profile is a no-op; changed parser settings may require a new version.

There is no automatic folder watcher. Updates require submission. Shared publication is pinned to a version, so a temporary visibility gap can occur between promotion and explicit republication; this is not a zero-downtime deployment promise.

## Source fidelity and review

Ordinary clean text may be accepted by an automatic policy; that is **not human source verification**. OCR, inferred tables, damaged fonts, formulas and uncertain chart associations remain held until source-checked. Generic visual-gap placeholders are never embedded.

An HTTP 200 conversion or an indexed document does not prove complete PDF coverage. Inspect the original, normalized Markdown, warnings/diagnostics and held units in the operator console. Never approve everything just to make a status green.

Tesseract with English data is required for local OCR. Optional layout parsing:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[pdf]"
```

Use the actual `strategy_used` value: a native fallback is not a successful Docling validation.

## Models, storage and logs

- Document embeddings: Cohere `embed-v5.0-pro`; query embeddings: `embed-v5.0-fast`; default 1,024 dimensions.
- Reranking: configured Cohere reranker, required for generated document answers.
- Answers: configured Cohere Command or optional Gemini adapter; Ollama is an optional operational answer fallback. Changing the answer provider does not remove embedding/reranking dependencies.
- PostgreSQL owns versions, acceptance/reviews, schedules, jobs and chat history.
- Qdrant holds the derived vector/text index.
- `data/` holds originals, normalized artifacts and `logs/console.log`.

Use a reply's **Request details** and request ID to correlate server logs. Quota exhaustion is not repaired by retries, a new upload, or assumed account switching. Keep company-content permissions separate for embedding, reranking, answers and optional vision providers.

## Test and audit

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -B tools/verify_hosted_ui.py --guest
.\.venv\Scripts\python.exe -B tools/verify_hosted_ui.py
.\.venv\Scripts\python.exe -B tools/verify_console_ui.py
.\.venv\Scripts\python.exe -m pip check
```

Database integration tests require `GSME_TEST_DATABASE_URL` pointing to a **separate database whose name starts with `gsme_rag_test`**, and `GSME_TEST_QDRANT_URL`. Original-source tests also require their documented fixture paths; skipped tests are not passes. Browser tools use isolated synthetic fixtures, not a real provider-quality benchmark.

Live evaluations need explicit source approval, a bounded request budget and available provider quota. Keep source-specific reports under ignored `validation/`; do not publish confidential extracts. Unit tests, live ranking metrics, source-coverage checks and human/agent answer judgments are different forms of evidence.

## Security and demo limits

The operator console is loopback-only and token-authenticated. Keep it, PostgreSQL and Qdrant off the public internet. Hosted source authorization is checked before candidate retrieval, provider transmission and source opening. Office archive expansion is bounded before format parsing.

Public FAQs are intentionally guest-accessible. Browser uploads are owner-only through an opaque seven-day session, not a verified company identity. Clearing cookies loses access; it does not automatically erase server files. Google Workspace sign-in requires real company OAuth configuration.

For company hosting, configure HTTPS, persistent storage, backups/restores, retention, service monitoring, provider capacity and the intended access policy. The existing Dockerfile/Render descriptor launches the **legacy parsing preview**; it is not a production deployment of the hosted chatbot.

Never commit real secrets, company originals, vectors or evaluation extracts. Do not delete `data/` or Docker volumes as repository cleanup. See CHANGELOG.md and the generated Word handover for current verification results and remaining limitations.
