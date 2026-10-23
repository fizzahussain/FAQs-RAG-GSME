# Direct audit of the attached 0.13.0 archive

Release 0.14.2 · 19 October 2026

## Source and method

This audit used the exact attached `gsme-rag-0.13.0(1).zip`. Its archive integrity check passed, its 68 tests passed, and the source was extracted into an isolated folder. I then ran the existing tests, supplied FAQ and schedule examples, and eleven additional generated stress cases. This release is based on that archive, retaining its optional Docling path, DOCX header/footer and cell OCR, and explicit FAQ/paper functions. It supersedes the earlier 0.14.0 ZIP that had been reconstructed from an older 0.10 source before the attachment became available.

## Findings and corrections

| Finding in attached 0.13.0 | Impact | Fix and evidence |
| --- | --- | --- |
| A scanned PDF page could return only `<!-- page: 1 -->` when PyMuPDF's OCR bridge failed despite Tesseract being installed | No semantic evidence from a readable scan | Bounded raster-to-Tesseract fallback; generated scanned-page regression now recovers its text and flags the chunk |
| Detected ordinary native PDF tables had no review marker, although formula tables did | Inferred cell boundaries looked trustworthy without verification | Every inferred table gets a review marker; generated two-row paper table stays row-preserving and flagged |
| OCR review text immediately after a complete FAQ answer could be appended to that answer | Wrong source attribution during retrieval | Split review content from a completed answer; when the image is the only answer, retain one flagged Q&A unit; both cases have tests |
| Any styled FAQ question routed an entire mixed document through the FAQ path | A bold rhetorical question under Methods could become a made-up FAQ pair | Automatic mixed route keeps paper headings and their descendants as prose; explicit `chunk_faqs` and `chunk_papers` remain available |
| A repeated large image in a PDF could be OCRed on many pages; raster regions were unbounded per page | Slow ingestion on image-heavy files | Six-region per-page cap, one OCR result per image digest, 30 MiB per-image input guard |
| Empty and protected PDFs lacked clear early checks | Unhelpful errors for inaccessible input | Explicit no-page response and locked-PDF error |

## Measured results

The revised archive passes **79/79 automated tests** in the available Python environment, including all 68 original tests. Additional tests cover DOCX embedded images, scanned PDFs, missing Tesseract, numeric/table review flags, mixed paper/FAQ headings, an 80-page selectable-text PDF, a 1,000-row FAQ table, vector graphics with selectable labels and corrupt DOCX input.

| Available file | Observed result |
| --- | --- |
| Bold-question FAQ DOCX | 31 question-answer chunks; 8 General, 12 TapeOut Services, 7 Additional Services, 4 Shared Block tapeout |
| FAQ table PDF | 31 category-labeled row chunks with page provenance and `pdf_table_inferred` warning |
| Life-story DOCX | 6 narrative sections; embedded rhetorical questions stay prose |
| Full schedule CSV | 33 typed exact records and zero default semantic chunks |
| Shared schedule CSV | 14 typed exact records and zero default semantic chunks |
| Generated scanned PDF | Recognized `2027`, preserved page 1 and marked its chunk for review |
| Generated native PDF table | Two row chunks, both marked for review |
| Generated 80-page PDF | First and last semantic chunks retain pages 1 and 80 |
| Generated 1,000-row FAQ Markdown | 1,000 distinct row chunks with their own question and answer |

The suite and smoke checks were run from the extracted project with `PYTHONPATH=src python -m unittest discover -s tests -v`. Use the documented virtual environment commands in `README.md` for Windows. The Docker image and optional Docling conversion were not executed in this environment; the attached 0.13 historical audit describes earlier Docling investigations, which are not a new measurement for this release.

## Limits and the ingestion gate

This is a parsing and chunking demo, not an indexed retrieval system. `review_required` is a warning, not an implemented approval workflow. **An ingestion worker must exclude flagged chunks until a reviewer checks them against the original.** Text OCR cannot reliably infer graph trends, merged tables, formulas, units, small symbols or handwriting; scanned table words may be flattened. The actual long engineering paper files mentioned earlier were not attached to this turn, so their visual extraction was not remeasured. A deployed Docker instance, concurrent upload load and final retrieval accuracy also remain unmeasured.

Before production indexing, assemble a reviewed source-to-evidence dataset across FAQ, narrative and actual technical papers: expected question/answer pairs, table cells, figure facts and page citations. Measure answer preservation, false pairing and page/category errors for semantic chunks separately from exact schedule/KPI filter correctness. For charts and complex scanned tables that fail this gate, evaluate a layout/vision extractor on those same pages and keep its uncertain output out of the trusted index until verified. A CPU can run the included Tesseract path; a GPU is only relevant if a measured Docling or vision workload needs it.
