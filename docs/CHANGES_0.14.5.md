# Parsing and chunking release 0.14.5

## Evidence fixes

- Restored numbered headings from selectable PDF text when Docling emitted them as prose. OCR-only image text remains review-marked evidence and cannot become a heading.
- Reconciled differing Docling and supplemental OCR into one review unit that preserves both readings. The parser does not guess which reading is correct.
- Marked every Docling-inferred table as review-required. Oversized generic rows split only at labeled field boundaries and repeat row identity; evidence is not truncated.
- Removed repeated marginal headers, footers, and physical page numbers from Docling retrieval evidence. Canonical source-document metadata from a repeated header is retained once.
- Suppressed generic visual placeholders for repeated logos and small decorative assets. Substantive raster images and captioned figures retain visual review warnings.
- Recovered selectable page text when Docling retained less than one quarter of a substantial text layer. Recovery is explicitly review-marked because reading order came from the fallback layout.
- Deduplicated figure captions even when Docling emits the caption item before its picture.
- Disabled Docling's internal OCR and use the bounded Tesseract supplement instead, eliminating the repeated sparse-region OSD failures seen on the 175-page manual.

## Regression coverage

Focused tests cover numbered heading restoration, OCR conflicts, figure/table caption deduplication, repeated-logo suppression, repeated furniture cleanup, sparse-page recovery, table review propagation, oversized row splitting, page attribution, and the synthetic chart/OCR cases. The complete suite passes with 109 tests on the validated Windows environment.

## Remaining boundaries

- Visual review identifies that a substantive chart or diagram is unresolved; it does not interpret geometry or assert category-to-value mappings.
- OCR strings and reconstructed/merged table cells remain review-required until checked against the original page.
- A recovered selectable page preserves text, not guaranteed spatial layout. Complex contents pages and multi-column regions therefore remain review-required.
- Embeddings, retrieval, a reviewer workflow, and answer generation are intentionally not included.
