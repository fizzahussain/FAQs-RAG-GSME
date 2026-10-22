# GSME RAG parsing and chunking

Version 0.14.5 prepares evidence for two future search paths. DOCX, PDF and standalone image files become normalized Markdown, then a separate chunker creates semantic evidence. Schedule CSVs also become readable Markdown, while a typed row reader handles exact technology, flavor and date filters. There is no database, embedding index, LLM or answer generation in this release.

| Source | Default output | Search path |
| --- | --- | --- |
| FAQ prose | One question with its answer | Semantic candidate |
| FAQ table | One category, question and answer per row; long answers split with the question repeated | Semantic candidate |
| Narrative | Paragraph evidence with bounded splitting and overlap for long prose | Semantic candidate |
| Other table | Column headers with each intact row; warn on oversized rows | Semantic candidate |
| Embedded or standalone image | OCR text flagged for source review; unreadable graphics warned about | Semantic candidate after review |
| Full or Shared schedule CSV | Validated typed records with physical CSV row numbers | Structured exact filters |

The default chunker detects FAQ pairs and Question/Answer table columns, while paper sections with headings such as Methods and Results remain prose in mixed documents. The chunk response shows `content_route` (`faq`, `paper`, `mixed`, or `schedule`) so this decision is visible. Schedule rows are **not chunked by default**. Optional semantic discovery rows can be enabled after their Full or Shared label is verified. Unlabeled schedule files cannot enter either downstream path. The typed records remain the source for exact dates and future KPI calculations.

Version 0.14.3 adds technical-manual routing, repeated PDF margin-text filtering, protection against giant/one-row false tables, nested review-marker handling, and an explicit reviewable visual-evidence gap for substantive figures. Chart-bearing pages with percentage labels are conservatively review-marked. These guards do not reconstruct equations or interpret charts; see [0.14.3 changes and limits](docs/CHANGES_0.14.3.md).

## Run locally on Windows

Extract the ZIP and open PowerShell inside the directory containing `pyproject.toml`. Run one command at a time:

```powershell
py -3.12 -m venv .venv
Test-Path .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m gsme_rag.cli parse "examples\GSME_Parsing_Validation_0.14.3.pdf" -o validation.md
.\.venv\Scripts\python.exe -m gsme_rag.api.run
```

If Python 3.12 is unavailable, use `py -m venv .venv` with Python 3.11 or newer. `Test-Path` must print `True`. Prefer the explicit environment Python; bare `pip` might install outside the environment. The sample CLI command writes `validation.md` and metadata in the current folder. Open `http://127.0.0.1:8000/` or `/docs`, and stop the server with Ctrl+C.

The `test` extra installs the HTTP client used only by the test suite. PyMuPDF is constrained below major version 2 because its table output changed. On a machine without Tesseract, two OCR integration tests report skips; install it and rerun to execute those cases. The API opens bounded PDF uploads from memory so a corrupt upload cannot keep its temporary file locked on Windows.

**OCR:** Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) and make `tesseract --version` work in a new terminal. The base Python install then reads embedded DOCX images, standalone PNG/JPEG/TIFF/BMP scans, scanned PDF pages and large raster images on otherwise digital PDF pages. CSV is already text data. If Tesseract is missing or an image cannot be decoded, the parser warns instead of inventing text. **Optional complex PDF path:** run `.\.venv\Scripts\python.exe -m pip install -e ".[pdf]"` for Docling and its model dependencies, which can be a large download. Its first run may fetch models and take time on a CPU. Docling reconstructs page layout and tables. Set `DOCLING_DEVICE=cpu` (PowerShell: `$env:DOCLING_DEVICE = "cpu"`) to force CPU. A compatible GPU can be selected later with `DOCLING_DEVICE=cuda` and an appropriate PyTorch build. Charts are **not** automatically interpreted as numeric facts.

Automatic PDF routing uses Docling only for complex files of up to 12 pages; longer files use the native path so the synchronous demo stays usable. For an intentional long local conversion, set `$env:GSME_PDF_BACKEND = "docling"` before running the CLI. Set it to `"native"` to force the lightweight path. Specialized two-column FAQ PDFs retain their deterministic parser.

OCR, uncertain table, and visual-gap chunks have `review_required: true`. A warning about a graphic does not automatically make nearby selectable prose false, but that prose does not contain verified visual facts. Chart-bearing pages may be flagged in full when labels can be misassociated. Check review-marked evidence against the source before indexing. Removing a review marker from Markdown does not verify its contents; a changed-Markdown warning will also appear.

The synthetic [three-page validation PDF](examples/GSME_Parsing_Validation_0.14.3.pdf) is safe to upload to the local demo. It contains a selectable table, a raster image with `OCR TEST: YIELD 92 PERCENT` and `LOT A17 / REVIEW NUMBERS`, and a vector chart. One observed OCR run returned `LOT Al7` (letter `l` instead of digit `1`), so successful OCR still requires source review. With Tesseract on PATH, expect paper routing, reviewed table rows, reviewed OCR, and a reviewed chart page. Without Tesseract, expect `image_ocr_unavailable` and a reviewed visual gap, not recovered image text. Reparse after changing OCR installation; an old parse result is not updated automatically. `visual_evidence_missing` means the diagram has not been semantically interpreted. This is a structural test, not a test of Docling model accuracy.

macOS or Linux: replace the first command with `python3 -m venv .venv` and the `.\.venv\Scripts\python.exe` prefix with `.venv/bin/python`. On CPU-only Linux, install matching CPU builds of PyTorch and torchvision from the [official PyTorch CPU index](https://pytorch.org/get-started/locally/) before the Docling extra; mismatched wheels can cause an image-processor import error.

For long PDFs, use the CLI locally: `.\.venv\Scripts\python.exe -m gsme_rag.cli parse "your.pdf" -o output.md` and `.\.venv\Scripts\python.exe -m gsme_rag.cli chunk output.md -o chunks.json`. Parsing also writes `output.metadata.json` beside the Markdown. The API demo handles one request at a time and limits uploads to 20 MiB; free web hosts may time out while running Docling on hundreds of pages.

## Demonstration

1. Parse a bold-question FAQ DOCX. Automatic chunking should pair each question with its answer and category.
2. Parse the two-column FAQ PDF. Review the `pdf_table_inferred` warning against the source. Each resulting chunk contains its category, question, answer and page metadata; a filename-derived title and table header are not repeated in semantic text.
3. Parse a headerless FAQ table DOCX. The first question must remain a data row. A short document title and explanation should not become separate heading-only chunks.
4. Parse narrative prose. Italic rhetorical questions inside paragraphs remain prose, not fabricated FAQ records.
5. Parse Full and Shared schedule CSVs and use **Exact schedule filters**. Technology is an integer, dates are dates, and each output preserves the original CSV row. Original export names such as `Tech (nm)` and normalized column names are supported. For a generic filename, explicitly select Full or Shared before proceeding.

## API

- `POST /v1/parse`: upload DOCX, PDF, CSV, PNG, JPEG, TIFF or BMP; returns Markdown, document metadata and relevant warnings.
- `POST /v1/chunk`: pass Markdown and the metadata from `/v1/parse`; default `strategy=auto`; returns concise semantic chunks and warnings. A changed Markdown hash is reported. PDF category and page information stay on the chunk. Split FAQ answers repeat their question and have a shared unit ID.
- `POST /v1/schedules/preview`: upload a schedule CSV and filter by `technology_nm`, exact case-insensitive `flavor`, and `tapeout_on_or_after`. It reads typed records independently of chunking and does not store them.
- `GET /v1/health`: readiness check.

The browser page at `/` is a simple demo. Uploads have a 20 MiB limit and temporary files are removed after each request. No persistent state is provided. `max_chars` is a character budget, not an embedding token limit. A generic non-FAQ table row that exceeds it is split only at labeled field boundaries. Each part repeats the row identity and stays within the budget; values are not silently detached from their column meaning.

## Deployment

Push this directory's contents to the GitHub branch connected to Render. `render.yaml` configures a Docker build, and `Dockerfile` installs Python dependencies and the Tesseract executable. The health path is `/v1/health`. An existing Python runtime service may need its runtime changed to Docker in Render; check the service's runtime and public `/v1/health` response after deploying. Use synthetic or approved documents on a public service without authentication.

`Dockerfile` installs Tesseract and English language data, while `render.yaml` selects the Docker runtime. If an existing Render service still uses Python, switch its runtime to Docker in Render or create a new Docker service; a code push by itself cannot install a system executable into that Python runtime. Check `/v1/health`: `ocr_available` must be `true`. The hosted image does **not** install Docling. For complex PDF conversion use the optional local setup or a larger worker.

For design decisions, metadata fields, real-file findings, validation and limits, see [Technical guide](docs/TECHNICAL_GUIDE.md).

For the direct audit of the attached 0.13.0 ZIP, fixes and limits, see [Audit](docs/AUDIT.md). Earlier notes are preserved in [Historical audit](docs/AUDIT_0.13.md).

## Docling validation and page context (0.14.5)

Install the optional PDF dependencies with `.\.venv\Scripts\python.exe -m pip install -e ".[pdf,test]"`. In PowerShell, run `$env:GSME_PDF_BACKEND = "docling"`, then parse the three-page test PDF again. Inspect `strategy_used` in the `/v1/parse` JSON: `docling_layout_ocr_to_markdown` confirms that Docling produced the result. `pymupdf_table_fallback` means the native backend ran; inspect `docling_unavailable`, `docling_failed`, or `docling_empty_fallback` warnings. Chunk the new normalized Markdown and verify that the page 3 opening is no longer under the page 2 heading. The synthetic PDF filename remains 0.14.3 because its contents are unchanged.

Version 0.14.5 also suppresses repeated logo-only visual placeholders, removes repeated PDF margin furniture while retaining canonical source metadata once, recovers selectable pages that Docling reduced to fragments, and keeps every inferred Docling table row review-required. See [0.14.5 changes and validation](docs/CHANGES_0.14.5.md).
