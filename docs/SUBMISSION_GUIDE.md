# Internship submission and demonstration

## Deliverable and scope

GSME Muse local RAG prototype, version 0.18.1: FAQ and manufacturing-document evidence search with mandatory reranking and cited answers, plus exact Full/Shared tapeout schedule search. Primary generation is Cohere Command with local Ollama operational fallback. Gemini is optional, disabled and not live-validated. It must not be presented as the primary deployed model. [Latest provider/resource audit](CHANGES_0.18.1.md) and [optional model-routing proposal](MODEL_ROUTING.md).

Document path: original -> normalized Markdown -> provenance-linked chunks -> automatic clean-text acceptance / uncertain-evidence gate -> PostgreSQL + Cohere/Qdrant -> dense/BM25 fusion -> reranking -> bounded cited answer.

Schedule path: original CSV/XLSX -> strict schema/milestone validation -> versioned PostgreSQL records -> deterministic intent/date/filter planning -> exact SQL result rows and original-source references. Schedule-only lookup does not require embeddings, reranking or an external answer model.

The key engineering distinction is **evidence authority**: model relevance does not verify a source, an extracted chart label does not establish its mapping, and an imported schedule is not a live capacity guarantee.

## Setup / upgrade checklist

1. Preserve the existing `.env`, source data and Docker volumes. Replace previously shared credentials locally; changing `.env` alone does not rotate an existing PostgreSQL password. Do not include credentials in the submission.
2. Install with `.\.venv\Scripts\python.exe -m pip install -e ".[storage,test]"`.
3. Start Docker Desktop and `docker compose -f compose.local.yaml up -d`.
4. Run `.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate` (includes migration 005).
5. Start `.\.venv\Scripts\python.exe -m gsme_rag.storage.admin`; open `http://127.0.0.1:8010/` or authenticated API documentation at `/docs`.
6. Keep the working PostgreSQL URL and review token private. Real Cohere document retrieval needs the API key and explicit general/reranking permissions. Generated answers additionally need `GSME_ANSWER_CONTENT_APPROVED=yes` only when company-authorized. Schedule-only queries remain local.
7. Existing approved indexed documents remain usable. Re-ingest only when applying a changed parser/profile; do not unnecessarily re-embed the corpus. The verified two-sheet schedule workbook is already loaded into `local-test` (33 Full + 14 Shared rows, one source). Benchmark fixtures remain isolated in the test database. Replace this logical schedule document when updating it rather than importing additional overlapping CSV copies.

## Demo sequence

| Demonstration | What it proves |
| --- | --- |
| Known FAQ + paraphrase, open cited original | Semantic search, reranking and source traceability |
| Manufacturing question with verified text | Complex-document evidence retrieval and page context |
| Unsupported question | Explicit insufficient-evidence behavior, not an invented answer |
| `Next shared 28nm tapeout` | Local routing, nearest dates and exact source fields |
| Final-deadline range / next-month schedule query | Typed filtering and explicit calendar interpretation |
| Source update under existing document ID | Current-version replacement; stale rows not mixed with new |
| Held formula/OCR example | Evidence quality controls rather than falsely complete extraction |

Use already-indexed evidence for the live semantic demo, not a long cold Docling conversion. Use the CLI or `/docs` for advanced schedule filters while the requested interface design remains pending. Check the current source dates before choosing a question; no future match is a valid result, not a reason to fabricate one.

## Evidence to include

Release verification: **285 tests passed with no skips**, **44/44 real-source schedule checks passed**, and desktop/mobile browser checks passed. No new external API evaluation was run in 0.18.0; prior bounded semantic/answer evaluation remains identified separately.

- [Architecture and evidence contract](TECHNICAL_GUIDE.md)
- [Retrieval/reranking](RETRIEVAL.md) and [exact schedules](SCHEDULE_SEARCH.md)
- [0.17.0 answer evaluation](CHANGES_0.17.0.md) and [0.18.0 schedule release](CHANGES_0.18.0.md)
- Reproducible tests and synthetic fixtures in the repository; summarized results in release notes.
- Share private original documents / detailed validation artifacts only through company-approved channels, not public GitHub. `validation/`, `data/`, `.env` and generated packages are ignored.

## Honest acceptance boundary

This is a tested local internship deliverable, not a certified enterprise deployment. The small answer benchmark has no independent human semantic sign-off. Manufacturing formulas, uncertain tables/OCR, chart mappings, missing URLs and blank answers still require source-owner decisions or remain excluded from trusted evidence. No provider can solve missing authority by guessing.

Before company rollout: confirm user/document access policy, external-provider permission, hosted deployment, backups/restore, API budget and representative load/latency targets. Real company PPTX layouts still need examples. The supplied real schedule XLSX has now been tested; this does not validate every possible spreadsheet layout. Interface redesign remains explicitly outside this increment. Automatic website synchronization, notifications and booking are not part of the imported-schedule search contract.
