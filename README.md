# GSME RAG parsing and chunking

Version 0.10.0 prepares evidence for two future search paths. DOCX and digital PDF files become normalized Markdown, then a separate chunker creates semantic evidence. Schedule CSVs also become readable Markdown, while a typed row reader handles exact technology, flavor and date filters. There is no database, embedding index, LLM, OCR or answer generation in this release.

| Source | Default output | Search path |
| --- | --- | --- |
| FAQ prose | One question with its answer | Semantic candidate |
| FAQ table | One category, question and answer per row; long answers split with the question repeated | Semantic candidate |
| Narrative | Paragraph evidence with bounded splitting and overlap for long prose | Semantic candidate |
| Other table | Column headers with each intact row; warn on oversized rows | Semantic candidate |
| Full or Shared schedule CSV | Validated typed records with physical CSV row numbers | Structured exact filters |

Schedule rows are **not chunked by default**. Optional semantic discovery rows can be enabled after their Full or Shared label is verified. Unlabeled schedule files cannot enter either downstream path. The parser keeps the original Markdown as a preview, while the typed records remain the source for exact dates and future KPI calculations.

## Run locally on Windows

Extract the ZIP and open PowerShell inside the directory containing `pyproject.toml`. Run one command at a time:

```powershell
py -3.12 -m venv .venv
Test-Path .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m gsme_rag.api.run
```

If Python 3.12 is unavailable, use `py -m venv .venv` with Python 3.11 or newer. `Test-Path` must print `True`. Prefer the explicit environment Python; bare `pip` might install outside the environment. Open `http://127.0.0.1:8000/` or `/docs`, and stop the server with Ctrl+C.

macOS or Linux: replace the first command with `python3 -m venv .venv` and the `.\.venv\Scripts\python.exe` prefix with `.venv/bin/python`.

## Demonstration

1. Parse a bold-question FAQ DOCX. Automatic chunking should pair each question with its answer and category.
2. Parse the two-column FAQ PDF. Review the `pdf_table_inferred` warning against the source. Each resulting chunk contains its category, question, answer and page metadata; a filename-derived title and table header are not repeated in semantic text.
3. Parse a headerless FAQ table DOCX. The first question must remain a data row. A short document title and explanation should not become separate heading-only chunks.
4. Parse narrative prose. Italic rhetorical questions inside paragraphs remain prose, not fabricated FAQ records.
5. Parse Full and Shared schedule CSVs and use **Exact schedule filters**. Technology is an integer, dates are dates, and each output preserves the original CSV row. Original export names such as `Tech (nm)` and normalized column names are supported. For a generic filename, explicitly select Full or Shared before proceeding.

## API

- `POST /v1/parse`: upload DOCX, digital PDF or CSV; returns Markdown, document metadata and relevant warnings.
- `POST /v1/chunk`: pass Markdown and the metadata from `/v1/parse`; default `strategy=auto`; returns concise semantic chunks and warnings. A changed Markdown hash is reported. PDF category and page information stay on the chunk. Split FAQ answers repeat their question and have a shared unit ID.
- `POST /v1/schedules/preview`: upload a schedule CSV and filter by `technology_nm`, exact case-insensitive `flavor`, and `tapeout_on_or_after`. It reads typed records independently of chunking and does not store them.
- `GET /v1/health`: readiness check.

The browser page at `/` is a simple demo. Uploads have a 20 MiB limit and temporary files are removed after each request. No persistent state is provided. `max_chars` is a character budget, not an embedding token limit. A generic non-FAQ table row that exceeds it stays intact with an `oversized_unit` warning; no values are silently separated from their columns.

## Deployment

Push this directory's contents to the GitHub branch connected to Render. `render.yaml` defines build `pip install .`, start `python -m gsme_rag.api.run` and health `/v1/health`; `.python-version` requests Python 3.12. Existing Render services following that branch redeploy after a push. Check the assigned public URL and `/v1/health` before the demo. Use synthetic or approved documents on a public service without authentication.

For design decisions, metadata fields, real-file findings, validation and limits, see [Technical guide](docs/TECHNICAL_GUIDE.md).
