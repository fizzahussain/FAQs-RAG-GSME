# GSME RAG: parsing and chunking demo

This is the first two weeks of a future RAG system. The parser normalizes DOCX, digital PDF and CSV files to Markdown. The chunker reads Markdown and document metadata, then prepares evidence for a future semantic index. The schedule CSV path separately validates typed rows for exact filters. No database, vector index, embeddings, LLM, OCR or answer generation is included.

## The decision

| Input | Parser output | Default downstream path |
| --- | --- | --- |
| FAQ prose | Markdown with bold questions | One question with its answer per semantic chunk |
| FAQ table | Markdown table | One row with repeated headers per semantic chunk |
| Ordinary narrative | Markdown paragraphs | One paragraph per semantic chunk; oversized prose splits with sentence overlap |
| Full or shared schedule CSV | Markdown table and typed schedule records | Typed records for exact filters; semantic row chunks are optional |
| Other CSV | Markdown table | Table rows with repeated headers for semantic inspection |

**Do not chunk a schedule CSV merely to perform structured search.** Exact technology, flavor and date filters use typed records. If discovery by natural language is later required, enable the optional semantic projection and keep every row labeled Full or Shared. The two channels have separate output metadata.

The four manual methods remain available as an experiment: `fixed_size` (baseline), `paragraph_aware`, `layout_aware`, and `hybrid`. The operational default is `auto`, which routes *each block*, so a mixed document can contain FAQ pairs, table rows and prose in one run. See [technical guide](docs/TECHNICAL_GUIDE.md).

## Run locally on Windows PowerShell

Extract the ZIP and open PowerShell in the folder containing `pyproject.toml`. If a previous `.venv` is broken, close old terminals, then remove or rename that old folder first. Run these commands one at a time:

```powershell
py -3.13 -m venv .venv
Test-Path .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m gsme_rag.api.run
```

If `py -3.13` is unavailable, use `py -m venv .venv` with your installed Python 3.11 or newer. `Test-Path` must print `True`; if it does not, repair the environment before installing. Use the explicit `.venv\Scripts\python.exe` commands rather than bare `pip` or `gsme-rag-api`, which may resolve to your user installation. Visit `http://127.0.0.1:8000/` for the simple browser demo or `/docs` for FastAPI documentation. Stop with Ctrl+C.

macOS/Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m gsme_rag.api.run
```

## Demo walk-through

1. Upload the bold-question FAQ DOCX, parse, then click **Chunk Markdown** with Automatic. Expect 31 FAQ pair chunks.
2. Upload the FAQ-table PDF and repeat. Expect 31 table-row chunks with header and page metadata.
3. Upload the life-story DOCX. Expect eight paragraph chunks, with rhetorical questions retained as prose.
4. Upload a Full or Shared schedule CSV. The parsed Markdown remains visible. Click **Preview schedule records** in section 3 and filter by fields and dates. Section 2 returns no schedule chunks by default. Check **Also create semantic discovery chunks** to inspect a row-per-chunk projection if wanted.
5. Upload `no name in file.csv` without choosing a schedule type. Parse metadata reads `needs_schedule_kind`, and section 2 is blocked. Choose **Shared schedules**, parse again and inspect both the row labels and source metadata.

## API contract

- `POST /v1/parse` accepts multipart `file` and optional `schedule_kind=auto|full|shared`. It returns Markdown, warnings and document metadata with SHA-256 source and normalized text hashes. Generic schedule names require an explicit label for downstream use.
- `POST /v1/chunk` accepts Markdown, optional parse `metadata`, `strategy=auto` by default, `max_chars`, `overlap_chars`, and `semantic_schedule_rows=false`. Each chunk has an explicit `category`, source ID, normalized hash, retrieval channel, route reason, type, heading path, page range, row number and part indexes. Sibling PDF categories replace one another; a wrapped bold question stays paired with its answer. Pass the entire `metadata` object from the parse response. Edited Markdown receives a warning and its own normalized hash.
- `POST /v1/schedules/preview` accepts a schedule CSV and optional `schedule_kind`; exact filters are `technology_nm`, `flavor` and `tapeout_on_or_after`. It returns typed records with row provenance and deterministic IDs. Generic schedule names without a label return HTTP 422.
- `GET /v1/health` returns `{"status":"ok"}`.

An upload is limited to 20 MiB. Source files are temporarily written and deleted after each request; the app does not persist response metadata. Character limits are not token limits. An oversized table row remains intact with an `oversized_unit` warning. Review inferred PDF tables against the original.

## Publish on Render

`render.yaml` and `.python-version` are included. Push the project folder to your GitHub branch, then in Render choose **New > Blueprint** and select that repository/branch. If the repository root has this `render.yaml`, Render uses its build and start commands. Alternatively make a **Web Service** with Python 3, build `pip install .`, start `python -m gsme_rag.api.run`, and health path `/v1/health`. It binds `0.0.0.0` and the Render `PORT`. Open the provided `https://…onrender.com/` URL; `localhost` works only on your machine. See [deployment instructions](docs/DEPLOYMENT.md).

Use approved or synthetic files for a public demo. The included web app has no login and does not yet implement production controls.
