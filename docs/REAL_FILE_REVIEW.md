# Review against supplied files

The October 2026 review used `lifestory.docx`, `FAQs Table Format.pdf`, and
`full_schedules(1).csv`. The two pasted text files were saved browser output,
not additional source documents. Counts below use version 0.7.0.

| Source | Parser result | Default 1,800-character chunk counts |
| --- | --- | --- |
| Life story, eight paragraphs without headings | Narrative Markdown, no warning; adjacent italic runs render as one span | 4 fixed, 4 layout, 4 hybrid, 8 paragraph aware |
| Four-page numbered FAQ PDF | 31 inferred FAQ table rows; `pdf_table_inferred` asks for page-by-page review | 7 fixed, 31 each for layout, hybrid, and paragraph aware |
| Full schedule CSV | 33 Markdown rows, and 33 independently validated typed records | 3 fixed, 33 each for layout, hybrid, and paragraph aware |

The PDF parser is specialized for numbered questions in two columns. It does
not prove reliable extraction from every possible PDF table. Review the
question, answer, category, and page for representative rows against the PDF.
Links shown as “here” in the FAQ text do not include their destination URLs.

The schedule CSV has repeated technology and flavor combinations. For
example, `28 / HPC+ RF` appears on three different tapeout dates. The
structured preview preserves all rows, converts technology to an integer and
dates to date values, and can filter by tapeout date. The Markdown table and
row chunks remain available for future semantic retrieval. No database or
embeddings are used.

For a small boundary check, two exact evidence excerpts per file were scored
with the keyword-only comparison. At 400 characters, the life story retained
both excerpts with hybrid and one with each of the other strategies. The FAQ
retained both excerpts under all strategies, but layout-aware variants kept
12 oversized rows intact. The schedule retained both excerpts with row-aware
strategies, while fixed size kept one. This is diagnostic evidence, not a
retrieval benchmark; two examples cannot establish a production winner.

The life-story questions are part of prose. The parser should preserve them
as text, and the chunker should keep useful passages. It should not invent a
FAQ answer for each question. Compare paragraph-aware and hybrid results for
readability and retrieval after an index exists.
