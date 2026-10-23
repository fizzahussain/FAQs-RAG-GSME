# Parsing and chunking technical guide

Version 0.14.6. This is a preparation demo: it has no vector index, database, answer generator or KPI calculation.

Docling conversion diagnostics are returned separately from normalized Markdown as `evidence_diagnostics`: stable evidence ID, original source SHA-256, page, PDF-coordinate bounding box, raw selectable text, optional independent OCR reading, and review reason. A grid-matcher loss retains every raw grid cell and marks the dropped ones. Do not treat a successful HTTP conversion or recovered TOC prose as verified table structure. `review.py` keeps reviewer decisions in a separate JSON ledger; `eligible_for_embedding` denies review-required chunks without explicit approval and always denies generic visual-gap placeholders. No embedding operation is implemented here.

## Flow and choices

1. `parsing/parser.py` dispatches DOCX, PDF, CSV and PNG/JPEG/TIFF/BMP images and writes normalized Markdown plus document metadata. It never chooses chunk boundaries.
2. `chunking/chunker.py` consumes Markdown. `detect_content_route` identifies FAQs from styled questions or Question/Answer table columns; a document that also has paper section headings takes the mixed route. A chaptered technical manual with only incidental questions takes the paper route. Methods, Results and similar sections preserve their rhetorical questions as prose. `chunk_faqs` and `chunk_papers` expose the two independent paths. FAQ pairs stay together; papers use paragraphs, sections and table rows. `auto` selects the path without uploader input. PDF page numbers are retained when available.
3. Schedule CSVs have a separate typed record reader (`parsing/schedules.py`) for exact technology, flavor and date filters. They do not need semantic chunks. Full/Shared must be resolved from an unambiguous filename or supplied explicitly. Optional semantic discovery preserves source row number but never replaces typed filtering.

| Strategy | Use |
| --- | --- |
| `auto` | Default per-block routing; preserves FAQ and table boundaries, separates prose paragraphs |
| `layout_aware` | Groups neighboring prose within a section while preserving FAQ/table boundaries |
| `paragraph_aware` | Keeps ordinary paragraphs separate |
| `hybrid` | Groups prose and overlaps when long prose splits |
| `fixed_size` | Comparison baseline; not for schedule rows or production ingestion |

## PDF paths

The special two-column FAQ layout remains deterministic and carries a `pdf_table_inferred` warning. Simple selectable-text PDFs use PyMuPDF's lightweight formatting path. Complex PDFs of up to 12 pages use Docling if the optional `pdf` extra is installed. Larger PDFs use native extraction by default and report `docling_skipped_large_pdf` when Docling is installed; `GSME_PDF_BACKEND=docling` explicitly overrides the size guard for local processing. Docling reconstructs layout/tables and emits source page markers. Bounded Tesseract OCR then supplements large raster regions; disabling Docling's unconditional OCR avoids repeated orientation-analysis failures on sparse regions. If an image-dominant page yields no readable content, the document falls back to native extraction and reports `docling_empty_fallback`. It emits a figure caption if one is present; an uncaptioned substantive picture produces `figure_needs_review`, not an invented description. Repeated logos and small decorative assets do not create generic evidence chunks. Vector-rich pages with visible labels also emit a review warning; this parser does not read graph values or claim visual interpretation.

Without Docling, PyMuPDF detects table cells and preserves their headers and rows. Every inferred native PDF table now carries `review_required: true`, since reconstructed cells need checking. Sparse image pages go through Tesseract, including a bounded raster fallback if PyMuPDF OCR returns empty. Large raster images on digital pages are OCRed individually, with a six-region-per-page bound and hash cache. Docling results are also checked for image text missing from the converted page. When readings differ, one review unit preserves both original readings instead of placing both in trusted chunks. `ocr_needs_review` means recognized text might contain incorrect numbers or cell associations. `figure_needs_review` marks images whose facts may be absent. Tables with merged headers or mathematical symbols require review against the source.

The 0.14.3 native path filters repeated margin headers and footers, rejects full-page and one-row false table detections while retaining their selectable source lines, and removes fully empty spacer columns from accepted tables. Recovered source lines from rejected formula layouts remain review-marked; their visual mathematical relationships have **not** been reconstructed. Large raster/vector content creates a separate reviewed `visual_evidence_missing` unit. A chart page with multiple percentage labels is conservatively marked for review as a whole. These heuristics may overflag figures; they are a safety boundary, not a visual understanding model.

## DOCX and image OCR

`parsing/docx.py` reads image relationships in paragraphs, table cells, headers and footers using the local `parsing/ocr.py` Tesseract helper. Repeated identical image bytes are OCRed once. Text follows the paragraph that holds the image; table cell text remains in the cell, and that table's rows are flagged for review. Header/footer OCR follows the main document and has a warning location; DOCX page numbers are not inferred. `parsing/image.py` converts standalone PNG/JPEG/TIFF/BMP files to review-marked Markdown. A missing system Tesseract installation yields `image_ocr_unavailable`, and `/v1/health` exposes `ocr_available` to diagnose the hosted instance. Image-only documents require OCR; CSV is already structured text.

The parser falls back to the native path if Docling cannot load or convert a file, with `docling_unavailable` or `docling_failed` in the response. Missing models, proxy dependencies and conversion errors are not mislabeled as a missing package. The Docker Render configuration installs Tesseract and the native PDF path, but not the optional Docling models. For local Docling, install Tesseract and run `pip install -e ".[pdf]"` in the venv; this extra includes substantial ML dependencies and may download models on first use. `DOCLING_DEVICE=cpu` is suitable for initial testing; a compatible CUDA setup can use `DOCLING_DEVICE=cuda`. Hundreds of pages are better processed with the CLI than a synchronous public request.

## Metadata and boundaries

Parser metadata carries source ID, hashes, filename/type, classification and (for schedules) Full/Shared and the physical CSV row map. Chunk records contain ID, text, type, source ID/filename, relevant category/page/table row, FAQ question where recognized, and split-part coordinates only when applicable. The API does not repeat parser diagnostics in each chunk. It warns if Markdown was edited after parsing.

A detected FAQ question must end in `?` and be a standalone bold or numbered block; prose with rhetorical questions remains prose. The answer ends at the next question, heading or table. For PDFs it also stops at a new page after a complete answer sentence, and never spans more than two pages without a new anchor. This prevents an early question absorbing a later chapter; unusual cross-page answers require review. A missing answer creates no evidence chunk. OCR or uncertain table Markdown is marked for review; affected chunks expose `review_required: true` and a single chunk-stage warning. Do not index those chunks until they are checked against the original PDF.

Markdown tables become row units. FAQ columns use a question-and-answer projection with a category if present. Non-FAQ rows repeat column headers. An oversized generic row is split only at labeled fields, with row identity repeated in every part; it is never silently truncated. The budget is characters, not embedding tokens.

## Validation and limitations

Run `python -m unittest discover -s tests -v` from the project root using the environment Python. The regression tests cover FAQ layouts, category changes, headerless tables, narrative paragraphs, schedule classification/rows, and native PDF tables and page boundaries. Representative pages from the supplied 188-page manufacturing slides and 175-page manual were checked with native fallback: tables were reconstructed, the image-only engineering table required OCR review, and formula tables still require inspection. This is structural validation, not a retrieval score. The optional Docling path was exercised on one manual page on CPU; it produced tables but took about 77 seconds on this machine. An image-heavy engineering page returned no Docling text, then native OCR fallback yielded a review-marked chunk. Source-to-output review on target hardware is still required before production use.

Schedule CSVs are validated by the same typed row reader during normalization and exact preview. A malformed date or duplicate column is rejected before optional semantic discovery. A filename containing both Full and Shared is rejected as ambiguous.

For semantic retrieval, benchmark reviewed question-to-evidence pairs at top-k with wrong-category and wrong-page failures. For structured retrieval and the future KPI tool, test exact date and numeric filters against typed schedule rows. A database, access controls, embeddings and answer generation belong to later stages.
