# GSME Muse document pipeline

Version 0.18.1 prepares and retrieves GSME/Muse FAQ and technical-document evidence, with exact Full/Shared tapeout schedule search. Clean text is accepted automatically; uncertain extraction stays held. PostgreSQL/Qdrant support versioned evidence and incremental vector reuse. Hybrid retrieval and mandatory reranking feed Command-first/Ollama-fallback cited answers. Optional strategies include local expansion/multi-query, semantic boundaries and exact extractive compression. [Current provider/resource audit](docs/CHANGES_0.18.1.md), [schedule verification](docs/CHANGES_0.18.0.md), [answer evaluation](docs/CHANGES_0.17.0.md), and [submission/demo guide](docs/SUBMISSION_GUIDE.md).

| Input | Processing |
| --- | --- |
| FAQ DOCX/PDF | Bold or numbered questions, question headings with answer paragraphs, and Question/Answer tables |
| Technical PDF/DOCX | Chapter context, paragraphs, tables, OCR, and explicit formula/chart fidelity warnings |
| PowerPoint PPTX | Slide text/links and FAQ tables; notes and image/chart evidence kept separately |
| Excel XLSX | Visible cells/FAQ tables with physical rows; formulas/merged or ambiguous records reviewed |
| Images | Local Tesseract OCR with a review flag |
| Generic CSV | Column-preserving table evidence; FAQ columns become question–answer units |
| Full/Shared schedule CSV/XLSX | Typed milestone records, local calendar planning, exact filters and source-linked pagination |

Normal ingestion needs no reviewer name or per-chunk notes. Clean text is accepted by an explicit automatic policy, not labelled human-verified. OCR, uncertain tables, formulas, chart claims, damaged text and missing links remain held aside. A successful parse does not verify facts. Generic visual placeholders are excluded. Acceptance/review decisions are stored separately from parsed content. [Automatic ingestion details](docs/CHANGES_0.16.3.md).

## Run on Windows

Use PowerShell in this folder. Keep your existing private `.env` when updating an installation.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[storage,test]"
```

For a new installation, copy `.env.example` to `.env` and fill in the database password, the matching URL-encoded password in `GSME_DATABASE_URL`, a reviewer name, and a random review token of at least 20 characters. Start Docker Desktop, then run each command separately:

```powershell
docker compose -f compose.local.yaml up -d
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m gsme_rag.storage.admin
```

Open **http://127.0.0.1:8010/**. Connect with your local token and workspace, choose a document and click **Ingest document**. Clean text is indexed without review forms; only extraction issues need optional attention. Start with `examples/synthetic_reviewed_faq.docx` and fake vectors to test locally. Choose Cohere and permit external content for real embeddings. [Step-by-step instructions](docs/STORAGE_INCREMENT.md).

The main page is now a question box, cited source matches and a collapsible Documents panel. Engineering controls and exception review are separate at `/admin`. Search reranks up to 30 eligible passages and shows up to 5 results; the actual count is visible. Real reranking additionally requires `GSME_RERANK_CONTENT_APPROVED=yes` and the interface's send-content confirmation. [Interface and source-backed checks](docs/CHANGES_0.16.4.md). [Retrieval setup and evaluation](docs/RETRIEVAL.md).

The smaller parsing-only demo remains available with `python -m gsme_rag.api.run` at `http://127.0.0.1:8000/`. It is the only app deployed by `Dockerfile`/`render.yaml`. The local operator console binds to loopback and has its own authenticated API at `/docs`.

## OCR and complex PDFs

Install Tesseract with English data, add it to PATH or set `TESSERACT_CMD`. The parser and health checks use the same executable lookup. Install optional Docling dependencies with `pip install -e ".[pdf]"`. Its first conversion can download models and be slow on CPU.

`GSME_PDF_BACKEND` selects `auto`, `native`, or `docling`. Automatic routing uses native extraction for long PDFs and specialized FAQ layouts. A forced request still reports the actual `strategy_used`: `docling_layout_ocr_to_markdown` confirms Docling conversion; a native fallback must be assessed separately. Neither OCR nor layout extraction verifies equations or chart mappings.

```powershell
$env:GSME_PDF_BACKEND = "native"
.\.venv\Scripts\python.exe -m gsme_rag.cli parse "examples\GSME_Parsing_Validation_0.14.3.pdf" -o validation\sample.md
.\.venv\Scripts\python.exe -m gsme_rag.cli chunk validation\sample.md -o validation\chunks.json
```

## Tests and source audits

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe tools\demo_storage_fixture.py
```

The service tests require `GSME_TEST_DATABASE_URL` pointing to an isolated database whose name starts with `gsme_rag_test`, and `GSME_TEST_QDRANT_URL`. They use synthetic evidence and fake vectors. They do not call Cohere. See the [storage guide](docs/STORAGE_INCREMENT.md#verification) for setup.

`tools/audit_sources.py` records local normalized Markdown, warnings, provenance, chunks, timing, and optional PDF page renders. Keep its output under ignored `validation/`. Small synthetic source fixtures stay in `examples/` because the tests use them. Real originals, data, `.env`, release ZIPs and generated validation output are excluded from Git and Docker builds.

- [Current audit, fixes and remaining source work](docs/PROJECT_AUDIT.md)
- [Architecture and evidence contract](docs/TECHNICAL_GUIDE.md)
- [Storage and console runbook](docs/STORAGE_INCREMENT.md)
- [Retrieval and mandatory reranking](docs/RETRIEVAL.md)
- [Tapeout schedule API, calendar rules and source validation](docs/SCHEDULE_SEARCH.md)
- [Internship submission and demonstration](docs/SUBMISSION_GUIDE.md)

Historical audit notes are available through Git history; the repository keeps the current operating documentation.
