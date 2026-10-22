# GSME parsing and chunking validation — 0.14.5

Validated on Windows with the repository `.venv` (Python 3.13.7), PyMuPDF 1.28.2, Docling 2.141.0, and `C:\Program Files\Tesseract-OCR\tesseract.exe`. Tesseract language data exposes `eng` and `osd`. The installed package and emitted parser metadata both report 0.14.5.

The original PDFs were the authority. The attached historical manual parse is embedded in `C:\Users\HP\.codex\attachments\336f46dc-b137-457a-abf6-c0a8d8a19721\Pasted text.txt`; its matching historical chunk JSON is `C:\Users\HP\.codex\attachments\d8e35413-8388-4c06-9617-7107b9905a9a\Pasted text.txt`. Current normalized Markdown and JSON are in the validation directories listed below.

## Issue findings

| Source page(s) | Observed historical output | Expected | Root cause | 0.14.5 change and regression |
| --- | --- | --- | --- | --- |
| Synthetic 1 and 3 | Docling emitted 1.1, 1.3, 1.4, and 3.1 as prose; the incidental question inherited 1.2. | Preserve chapter/numbered-section context without promoting raster OCR. | Docling label choice lost numbering while OCR and layout headings shared a path. | Restore headings only from selectable PDF lines. Tests cover restoration and prevention of OCR headings. The question now has 1.3 context; 1.4 remains a heading; page 3 uses 3.1. |
| Manual all pages; visually checked 1, 10, 75, 139 | Confidentiality text, document number, date, page numbers, and the logo entered evidence repeatedly. | Remove marginal repetition; retain genuine source metadata once. | Docling did not consistently label page furniture, and the same logo raster occurs on all 175 pages at the same top-margin box. | Position/repetition filter removes furniture. One canonical metadata block retains document type, number, and date. Final Docling: 0 confidentiality chunks and 1 document-number occurrence. Native: neither appears in evidence. |
| Manual all pages; substantive examples 10, 75, 139 | Historical output had 298 generic visual chunks; an intermediate run had one on every page. | Review real figures/charts, not logos or decoration. | Every repeated logo was classified as a picture; each picture generated a semantic placeholder. | Repeated image hashes and images below the substantive-area threshold are suppressed unless captioned. Full Docling has 74 visual-gap chunks, each on a distinct substantive page, instead of 175/298 generic chunks. |
| Manual tables; visually checked 1, 75–77, 139 | Historical output had 688 table-row chunks but only 106 reviewed. | Every uncertain Docling/native inferred row reviewed; preserve headers and numeric/merged structure. | Review markers did not cover every table path. | All table Markdown is enclosed in review markers and review state propagates through row splitting. Final full Docling: 668/668 table units reviewed. Native: 682/682. Pages 75–77 and 139 were rerun from the source; 9/9 table units reviewed. |
| Manual 75, 76, 77, 139 | Historical chunk indices 761, 776, 783, 1372, 1373 were 2,187–3,231 characters; page 139 also duplicated extracted material. | Stay within 1,800 characters without truncating or separating values from column meaning. | A whole wide table row was treated as indivisible, and Docling repeated table captions as text. | Oversized generic rows split at labeled fields and repeat row identity. Table/figure captions are page-locally deduplicated. Final full maximum: 1,796; representative final maximum: 1,759; zero oversized chunks and zero duplicate groups on pages 75–77/139. |
| Manual 2–9 | Docling retained only small fragments of selectable contents pages. | Do not silently lose a substantial selectable text layer. | Docling classified most contents-page text outside its exported reading order. | If Docling retains under 25% of a substantial selectable page, text-layer reading order is recovered into an explicit review block. Exactly pages 2–9 were recovered. |
| Synthetic 2 | Historical OCR could produce both `A17` and `Al7`, or promote image text. | Preserve readings for inspection, never guess, and require review. | Independent OCR streams were appended without reconciliation. | Matching text becomes one reviewed OCR unit; disagreements become one conflict unit containing both originals. Current Tesseract reads exact `LOT A17`; `Al7` is absent, and the chunk remains reviewed. |
| Synthetic 3 | Chart labels/caption could duplicate or imply a visual mapping; page context could leak from page 2. | Keep labels and one caption, do not claim mapping, attribute page 3 correctly. | Concatenated markers, omitted selectable labels, and duplicate caption objects. | Markers are line-separated, labels are recovered under review, caption values are deduplicated, and chapter boundaries clear inherited context. Page-3 opening has `source_page_start=3` and no page-2 heading. |

## Final runs

| Document/path | Backend requested | `strategy_used` | Route | Chunks | Reviewed | Max chars | Notes |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| Synthetic three-page PDF | native | `pymupdf_table_fallback` | paper | 21 | 10 | 205 | Pages 1–3; 2/2 table rows reviewed; exact A17; Figure 2 once. |
| Synthetic three-page PDF | Docling | `docling_layout_ocr_to_markdown` | paper | 22 | 5 | 219 | Genuine Docling result, no fallback; exact A17; headings restored; chart labels reviewed. |
| MFG-GSME-0002.pdf | native | `pymupdf_table_fallback` | paper | 1,422 | 792 | 1,749 | Pages 1–175; 682/682 table units reviewed; 45 substantive visual gaps. |
| MFG-GSME-0002.pdf | Docling | `docling_layout_ocr_to_markdown` | paper | 1,880 | 966 | 1,796 | Pages 1–175; 668/668 table units reviewed; 74 substantive visual gaps; pages 2–9 recovered. |
| Manual pages 75, 76, 77, 139 subset | Docling | `docling_layout_ocr_to_markdown` | paper | 22 | 17 | 1,759 | Final caption-dedup code: zero duplicate groups; 9/9 table units reviewed. Subset pages 1–4 map to original pages 75, 76, 77, 139. |

Full tests: **109 passed**. No embeddings or retrieval were implemented.

## Remaining limitations

- Docling logged that 22 of 183 cells in one 30×3 grid matched neither a row nor column band and were dropped. The affected inferred table remains review-required; it is not validated merely because its chunks fit the size budget.
- Page 75–77 wide tables have visual columns and image rows. Text is preserved with field context, but row/header associations remain inferred and require source review.
- The parser retains chart labels but does not assert their spatial A/B-to-percentage mapping.
- OCR and recovered selectable-page reading order remain review-required. There is no human-review workflow yet.
