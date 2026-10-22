# Windows validation report for 0.14.4

Validated through 22 October 2026 with `examples/GSME_Parsing_Validation_0.14.3.pdf` and the hash-identical copy in `D:\Downloads`.

## Environment

| Check | Result |
| --- | --- |
| Parser process | `D:\Desktop\GSME\RAG CODE\.venv\Scripts\python.exe` (Python 3.13.7) |
| Installed project | `gsme-rag 0.14.4` in the repository virtual environment |
| PyMuPDF | 1.28.2 |
| Docling | 2.141.0 with Torch 2.14.1 and torchvision 0.29.1 |
| Tesseract | 5.5.3 at `C:\Program Files\Tesseract-OCR\tesseract.exe` |
| OCR languages | `eng`, `osd` |
| Windows PATH | Tesseract directory added to the persistent user PATH; reopen terminals to inherit it |

Both recorded runs used the same virtual-environment interpreter and a process PATH containing the Tesseract directory. Docling used `DOCLING_DEVICE=cpu`.

## Backend results

| Requested path | `strategy_used` | Parse warnings | Route | Chunks | Result |
| --- | --- | --- | --- | ---: | --- |
| Native | `pymupdf_table_fallback` | `table_needs_review` (p1), `ocr_needs_review` (p2), `visual_evidence_missing` (p2, p3) | `paper` | 21 | Pass |
| Forced Docling | `docling_layout_ocr_to_markdown` | `table_needs_review` and `docling_heading_restored` (p1), `ocr_needs_review` and `visual_evidence_missing` (p2), `docling_heading_restored`, `figure_needs_review`, `visual_evidence_missing`, and `docling_text_omission` (p3) | `paper` | 23 | Pass with reviewed recovery |

The forced Docling run did not fall back. Tesseract emitted a non-fatal orientation-detection diagnostic for sparse content, while Docling completed with success and returned `docling_layout_ocr_to_markdown`.

## Page comparison

| Page | Native | Forced Docling | Finding |
| --- | --- | --- | --- |
| 1 | Pass | Pass | Selectable prose, table headers, A17/B08 rows, and incidental question preserved. Repeated header/footer and page furniture removed. Both reconstructed table rows are review-required. |
| 2 | Pass | Pass after OCR reconciliation | The current run reads exact `LOT A17` once. The attached historical `A17`/`Al7` disagreement is covered by a regression that retains both engine readings together in one `conflicting-image-ocr` review unit without selecting either. The caption is preserved once. |
| 3 | Pass | Pass with reviewed recovery | Formula, one chart caption, and labels `A: 20 percent` / `B: 80 percent` are present. Docling originally omitted the labels; they are recovered verbatim from selectable PDF text with `docling_text_omission` and review-required. All six page-3 chunks are review-required and report page 3. The opening chunk does not inherit page 2's `2.1 Expected behavior` heading. |

## Defects fixed

- OCR preprocessing enlarged the embedded PNG and changed `A17` to `Al7`. Bounded single-image rasters now use their original pixels first; the bounded render remains a fallback.
- When Docling and supplemental OCR still disagree, their original readings are retained together in one review-required conflict unit with `ocr_reading_conflict`; neither reading is promoted to trusted evidence.
- Existing Docling OCR could be emitted as an unreviewed heading. It is now normalized as review-required image OCR with a visual-gap marker.
- Docling table rows were not always review-required. All reconstructed Docling tables now carry review provenance.
- Docling omitted selectable numeric chart labels. Missing numeric source lines are recovered verbatim and review-marked rather than treated as verified chart relationships.
- Numbered headings omitted or flattened by Docling are restored from selectable source lines, including sections 1.1, 1.3, 1.4, and 3.1 in the synthetic PDF.
- Consecutive Docling headings could discard page-opening evidence, and Docling chapter levels could preserve the previous page's heading. Page-opening text is retained and `Chapter N` is normalized as a top-level heading.
- Markdown normalization and the chunker both separate concatenated control markers. The attached `review-end`/page-3 failure now reports page 3 correctly even when historical Markdown is rechunked.
- A Docling picture caption and its following caption item are deduplicated; Figure 2 appears once.
- Two native-specific tests depended on whether optional Docling was installed. They now explicitly force the native backend.

## Tests

- Focused parser/OCR/Docling regressions: pass.
- Full suite in the same Tesseract-enabled virtual environment: **96/96 pass, 0 skipped**.
- `git diff --check`: pass (line-ending notices only).

## Remaining limitations

- OCR and reconstructed table text remain evidence requiring source review even when characters match this synthetic file.
- OCR conflicts are preserved for inspection, not resolved automatically. A reviewer must consult the source image.
- The formula is preserved as plain selectable text; mathematical layout and symbol fidelity are not independently verified.
- Chart labels and captions do not verify spatial category-to-value relationships. The parser deliberately retains a visual-evidence gap.
- Docling omitted the selectable chart labels before the reviewed PyMuPDF recovery. This synthetic result does not establish completeness on manufacturing manuals, complex tables, or other figures.
- Representative original documents still need the same page-by-page evidence review before embeddings or retrieval work starts.
