# GSME Muse document pipeline

Version 0.15.5 prepares reviewed GSME/Muse FAQ and technical-document evidence for retrieval. It parses source files, creates source-linked chunks, records human review in PostgreSQL, and stores approved embeddings in Qdrant. Evidence search returns source text and citations. Answer generation and target-corpus retrieval evaluation are the next stage.

| Input | Processing |
| --- | --- |
| FAQ DOCX/PDF | Bold or numbered questions, question headings with answer paragraphs, and Question/Answer tables |
| Technical PDF/DOCX | Chapter context, paragraphs, tables, OCR, and explicit formula/chart fidelity warnings |
| Images | Local Tesseract OCR with a review flag |
| Generic CSV | Column-preserving table evidence; FAQ columns become question–answer units |
| Full/Shared schedule CSV | Validated typed rows with exact technology, flavor and date filters |

Every semantic chunk starts pending or excluded. Checking an image, formula or table means comparing it with the original source; a successful parse does not verify its facts. Generic visual placeholders are excluded. Approvals are stored separately from parsed content.

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

Open **http://127.0.0.1:8010/**. This local FastAPI console contains the upload, parsing, review, storage, embedding-preview and evidence-search workflow. Use the reviewer name and token from `.env`. Start with `examples/synthetic_reviewed_faq.docx` and fake vectors to test the workflow. Choose Cohere for real embeddings once source review and external-content permission are in place. [Step-by-step instructions](docs/STORAGE_INCREMENT.md).

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

Historical audit notes are available through Git history; the repository keeps the current operating documentation.
