# Parsing and chunking release 0.14.3

## What changed

- Repeated top/bottom PDF text and page-number furniture are filtered from native text before Markdown construction. The original PDF and its source hash remain intact.
- A manual with multiple chapter/numbered-section headings and only incidental questions routes to paper instead of FAQ. Explicit `chunk_faqs` remains available for real FAQ input.
- One-row or implausibly oversized PyMuPDF table regions no longer suppress their source text. The page is reviewed and the original selectable lines are retained. Empty spacer columns are removed from accepted tables.
- Nested review markers retain the outer review state, including prose after an inner table.
- Large raster/vector evidence gets an explicit reviewed visual-gap unit. Text labels and percentages associated with a chart are marked for review as a page when the chart detector fires. Docling picture items likewise get a reviewable visual-gap unit.
- Whole-page OCR results containing only a repeated manufacturing header/footer are stripped to avoid indexing boilerplate as OCR evidence.
- Added seven regression tests and a synthetic three-page PDF for local OCR, table, manual routing and chart checks.

## Validation

The complete local suite passed with 86 tests on Python 3.12, PyMuPDF 1.28.2 and Tesseract 5.3.4. The synthetic PDF was rendered and visually inspected. The 175-page manufacturing manual was reparsed with native PyMuPDF and Tesseract: paper route, zero Q&A chunks, the problematic one-row formula regions retained and marked for review, the 15-column S100 table reduced to its populated columns, and repeated page furniture removed from the text layer. Chart-bearing page 101 is entirely review-marked. A real Docling model conversion and a Windows rerun remain unverified.

## Important limits and ingestion gate

The recovery of a formula's source text does not validate its mathematical layout. A visible chart can still require human or layout-aware interpretation; an OCR label is not a verified category-to-value association. False table detection and spatial reading order can differ across PyMuPDF versions and source PDFs. Image references include logos and repeated assets; the new visual warning is conservative and may overflag some pages. The absence of `review_required` on prose does not certify every adjacent graphic. Do not build trusted embeddings from review-marked chunks, formula pages, or figure-derived claims until an evidence reviewer has checked the original pages. This release does not add a reviewer workflow, embeddings or retrieval.

On Windows, install Tesseract separately and verify `tesseract --version` in a fresh PowerShell window. Install `.[test]` to run tests, and optionally `.[pdf]` to trial Docling on a small page subset. A 175-page PDF takes the native route automatically; setting `GSME_PDF_BACKEND=docling` deliberately overrides that cost guard. The API `/v1/health` reports whether OCR is available on the server actually doing the parsing.
