# Parser and chunker audit

Version 0.13.0 · 15 October 2026

## Improvements in 0.13.0

- DOCX previously only warned that embedded images were ignored. Paragraph, table cell, header and footer images now run through local Tesseract, with source-location warnings. Standalone PNG/JPEG/TIFF/BMP scans also normalize to Markdown.
- The native PDF path now OCRs substantial raster regions within digital pages. Small PDFs processed by Docling receive a supplemental image text pass when an embedded raster was omitted from the converted text.
- OCR results are marked for source review on the affected chunk; non-OCR text remains unflagged. A repeated image is OCRed once per DOCX file.
- Automatic chunking exposes its FAQ or paper route. `chunk_faqs` preserves question-answer units; `chunk_papers` handles sections, prose and technical tables without reinterpreting bold questions as FAQ pairs. Schedule CSVs stay on the exact structured path.
- The Docker image installs the Tesseract executable for hosted OCR, and `/v1/health` reports `ocr_available`. A native Python Render instance may still lack OCR until migrated to Docker.

OCR reads visible text in figures but does not infer graph trends, pixel coordinates, data series, or image-only relationships. Rasterized table text may be flat text rather than a correct row-column structure. On the provided engineering slide, the optional Docling image conversion returned success with zero tables and almost no useful table text after roughly a minute of CPU work; switching to it for this sample would degrade the result. These cases require inspection or a separately validated vision/table stage before indexing.

This audit checked the code, automated tests and the supplied FAQ DOCX/PDF, life story, Full/Shared CSVs, and two long manufacturing PDFs. These checks measure structure and source preservation. They do not measure final RAG retrieval accuracy.

## Findings and corrections

| Priority | Finding in 0.11.0 | Correction in 0.12.0 |
| --- | --- | --- |
| High | OCR text from image pages could become an ordinary semantic chunk even when the parser warned that numbers may be wrong | The normalized Markdown carries review markers and only affected chunks expose `review_required: true`; one chunk warning directs a source check |
| High | OCR text nested under a Docling picture could be flattened into normal prose | Picture OCR is marked for review; figure captions are also marked, without invented descriptions |
| High | Docling could report success while returning no text from an image-heavy engineering page | Empty OCR pages now trigger native OCR/table fallback with `docling_empty_fallback`; recovered text is marked for review |
| High | A PDF with bold numbered technical steps and right-column text could be routed as a Q&A table | The two-column FAQ detector now also requires actual question evidence; the supplied FAQ PDF still produces 31 category-labeled rows |
| High | A malformed schedule could normalize as ready while the exact-search route rejected its dates or columns | Schedule normalization now runs the typed record validator; inconsistent rows and duplicate columns fail before discovery chunks can be made |
| Medium | A filename containing both `full` and `shared` silently selected one kind | Such filenames are rejected as ambiguous |
| Medium | Malformed PDF/DOCX uploads could raise an unhandled parser exception in the API | Invalid containers now become a clear HTTP 422 response |
| Medium | Docling import and runtime failures were both reported as “unavailable” | Missing package and conversion failure now have distinct warning codes |
| High | Installed Docling would process a 175–188 page PDF synchronously on CPU, taking too long for the demo | Automatic routing uses the native path above 12 pages; deliberate local Docling processing requires `GSME_PDF_BACKEND=docling` |
| Medium | Technical tables containing formulas could be used without a targeted review signal | Formula tables get a source-review warning and their rows carry `review_required: true` |
| Medium | A vector-only PDF page could be called empty | Drawn content with no text is reported for visual review |
| Low | Pages with many large figures produced an overwhelming warning per page | Native figure warnings are summarized with an explicit page-range location |

## What is working

- FAQ question and answer pairs remain together; table FAQs preserve category and PDF page location. The headerless Word FAQ test keeps its first data row. Cross-page FAQ answers stop before unrelated chapters.
- Narrative paragraphs are not turned into FAQs just because a question appears inside prose.
- Full and Shared schedule data are typed records for exact filters; semantic schedule chunks are off by default. The source row and schedule kind remain available when discovery is requested.
- Normalized Markdown and chunking remain separate stages. The default `auto` route uses block structure. `fixed_size` remains a comparison baseline.
- The native PDF path reconstructs many simple tables. A real one-page manual test exercised Docling on CPU; a scanned engineering screenshot exposed an empty Docling result and motivated the fallback. On image-heavy pages it attempts Tesseract when installed and identifies uncertain text. No chart values are fabricated.

## Still requiring review

- `review_required: true` is a review signal, not a confidence score or approval workflow. Do not put flagged chunks in a retrieval index until a person compares values to the source. Editing Markdown changes its hash; clearing a marker alone is not proof of verification.
- OCR of engineering numbers, units, merged cells, equation symbols and plots can be wrong even when text is returned. Some source charts have no reliable text representation. Neither PDF backend guarantees complete reading order for unfamiliar layouts.
- The 1,800-character setting is not a tokenizer limit. Retrieval benchmarks with annotated questions and expected passages are still needed before choosing an embedding model and production chunk budget.
- The public FastAPI demo handles one upload per request and has a 20 MiB limit. Long CPU Docling conversions need more time and memory than a synchronous free web instance may provide; use the native default on long files or explicitly force Docling for an offline run. No indexing, access control, background job queue, database or KPI computation is included.

## Verification

The 68-test suite includes FAQ, narrative, CSV, API, table, image OCR marker and routing regressions. Run it with the environment Python using `python -m unittest discover -s tests -v`. The original FAQ PDF yields 31 category-labeled rows, while the FAQ DOCX yields 31 question-answer chunks. The source Full and Shared CSVs yield 33 and 14 exact records and zero default schedule chunks. A real engineering slide image returned readable words but scrambled table cells under Tesseract, so its chunk stays review marked and should not be indexed as a validated table. The Docker image has not been built in this workspace, where Docker is unavailable. Unknown chart trends and OCR values remain review work.
