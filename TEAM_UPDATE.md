# GSME RAG: weekly progress and next steps

## Completed so far

- Separated parsing from chunking. The parser accepts DOCX, selectable-text PDF, and CSV and returns normalized Markdown. The chunker accepts Markdown independently.
- Provided FastAPI endpoints and a browser demo to inspect and edit the Markdown before chunking.
- Built layout-aware chunking for FAQ question/answer blocks, Markdown tables, and headed prose sections with source metadata.
- Added a size-based baseline, a hybrid mode with sentence overlap, and a paragraph-aware narrative mode, so the same Markdown can be compared under four strategies.
- Kept table rows intact in layout-aware and hybrid modes. Rows exceeding the chosen limit now produce an explicit warning.
- Added an in-memory typed schedule preview for exact technology, flavor, and date filtering alongside CSV-to-Markdown normalization.

These are implementation results, not retrieval accuracy results. No embeddings, search index, database, answer generation, or OCR are in this demo. The schedule filter reads an uploaded CSV directly.

## Current issue to review

Complex source tables may lose layout during PDF-to-Markdown conversion. Markdown table rows can also become too large for the requested chunk size; these remain intact with a warning. Verify table output against the original file and record any misplaced cells, headers, or rows. The chunker cannot restore information that parsing lost.

## Run the comparison

1. Install and start the app using the README commands; open `http://localhost:8000`.
2. Upload the same FAQ file or one of the two separate schedule CSVs, then inspect its Markdown against the source.
3. Run `layout_aware`, `fixed_size`, `hybrid`, and `paragraph_aware` on the *same* Markdown and maximum size. Record the number of chunks and any warnings.
4. For a non-FAQ example, upload `lifestory.docx` and compare its paragraph boundaries. Its internal questions are rhetorical prose, not FAQ rows.
5. For a schedule CSV, use section 4 to filter typed rows by technology, flavor, and tapeout date.
6. Save the JSON outputs or screenshots. Send the source file, the normalized Markdown, the settings, and the chunk output for any incorrect table or split.

| Source and question | Expected evidence | Strategy | Size / overlap | Chunk count | Evidence kept together? | Issues |
| --- | --- | --- | --- | ---: | --- | --- |
| FAQ: one real question | Its question and full answer | | | | | |
| Schedule: one real row | Header and all row values | | | | | |
| Narrative: why review? | Change-in-process paragraph | | | | | |

## Next work

1. Collect representative documents, including a narrative document and complicated tables, and write real questions with the expected source passage or schedule row.
2. Use the comparison to fix observed parser or chunking errors. Keep one change at a time so results can be attributed to it.
3. Add retrieval to measure whether the expected evidence appears in the top results. Chunk appearance alone is insufficient for a final strategy choice.
4. Decide storage after defining production schedule queries and reviewing the existing pricing analysis. The demo already validates and filters typed records in memory.
5. Add answer generation with citations after retrieval is dependable.

## Recommendation to present now

Use layout-aware chunking as the initial candidate for FAQs and tables; compare paragraph-aware and hybrid for prose. Keep fixed size as the baseline. Use typed schedule records for exact CSV filters. The final retrieval strategy depends on tests with real questions and retrieval, so no strategy is claimed to win yet.
