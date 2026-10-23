# 0.14.4

- At page boundaries, clear inherited section context when a new top-level chapter starts near the beginning of the page, so introductory text is not attached to the previous page section. Continuing pages retain their headings.
- FAQ answer collection skips page boundary markers so answers can still span pages.
- Added validation PDF context and continuation tests. Docling conversion on Windows remains to be validated.

## Windows validation on 21 October 2026

- The current checkout declares version 0.14.4. Global Python 3.13.7 exercised the checkout through `PYTHONPATH=src` with PyMuPDF 1.28.0: 87 tests ran, with 85 passing and two Tesseract integration tests skipped.
- The repository `.venv` is incomplete: the project and its test/runtime dependencies are not installed there, so its import failures are environment failures rather than code-test failures.
- Tesseract was not discoverable on the launching terminal's `PATH`. Native parsing of the three-page validation PDF therefore emitted `image_ocr_unavailable` and retained reviewed visual-gap evidence instead of claiming the raster text.
- Forcing `GSME_PDF_BACKEND=docling` produced `strategy_used=pymupdf_table_fallback` with `docling_unavailable`. This run did not validate Docling or image OCR.
- Native page-by-page inspection preserved page 1 prose and reviewed table rows, kept the page 2 raster as an unresolved visual gap, and review-marked the chart-bearing page 3. The page 3 opening chunk had no inherited page 2 heading, while the later Chapter 3 chunks had the correct context.
