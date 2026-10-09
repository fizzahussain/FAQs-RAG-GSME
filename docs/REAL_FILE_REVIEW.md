# Review of the supplied parsing and chunking cases

Historical 0.8.0 experiment. The current schedule policy and metadata contract are in [TECHNICAL_GUIDE.md](TECHNICAL_GUIDE.md); schedules now default to typed rows without semantic chunks.

This review uses the attached source files and pasted demo outputs, checked
against version 0.8.0. The pasted text files are saved browser results; they
are not additional document formats. Counts use a maximum of 1,800 characters
unless stated otherwise.

| Source | Parsing check | Recommended semantic chunking | Structured preview |
| --- | --- | --- | --- |
| Bold-question FAQ DOCX | 31 questions and answers; no warning | `layout_aware`: 31 question-answer chunks | Not applicable |
| Four-page, two-column FAQ PDF | 31 rows; inferred layout warning; three answers span pages 1–2, 2–3, or 3–4 | `layout_aware`: 31 table-row chunks with category and page span | Not applicable |
| Life-story DOCX | Eight ordinary paragraphs; no headings or FAQ entries | `paragraph_aware`: eight section chunks | Not applicable |
| Full schedule CSV | 33 rows, with repeated technology/flavor combinations | `layout_aware`: 33 table-row chunks | 33 typed records |
| Shared schedule CSV | 14 rows | `layout_aware`: 14 table-row chunks | 14 typed records |
| `no name in file.csv` | Same 14 data rows as the shared schedule, but its name provides no schedule type | Select **Shared schedules** at upload, then use `layout_aware`: 14 row chunks | 14 typed shared records |
| Headerless FAQ table DOCX from the test pack | Three rows preserved; `table_header_inferred` warning | `layout_aware`: one introductory section and three rows | Not applicable |

The Word and PDF FAQ files each contain 31 corresponding question-answer
entries. Their normalized question and answer text was compared entry by
entry; no missing or mismatched pair was found in this sample. The PDF's
`pdf_table_inferred` warning remains because positional extraction on other
PDF layouts can still be wrong. A page range in the PDF table means that the
answer continues on the next page. The visible word “here” in two answers
does not supply a destination URL in the available source PDF.

## Why the recommended chunking differs

At 1,800 characters, fixed-size chunking makes five chunks for the Word FAQ
and seven for the PDF. Complete answer text stays in one fixed-size chunk for
27 of 31 Word entries; a complete PDF row stays together in 26 of 31 cases.
Layout-aware chunking retains all 31 entries as separate units in each file.
Both `hybrid` and `paragraph_aware` also preserve the FAQ rows, but neither
offers an advantage for these particular FAQs. The synthetic headerless table
gets five chunks under `paragraph_aware` because the short title and
introduction become separate sections; layout-aware produces four.

The life story has eight distinct paragraphs. `paragraph_aware` creates eight
passages, matching these author-provided boundaries. Default layout-aware and
fixed-size make four larger chunks, and hybrid also makes four with limited
sentence overlap. Rhetorical questions inside the prose are preserved as
text; no question-answer records are invented. A later retrieval benchmark
must decide whether shorter or longer narrative passages rank better.

The schedule CSVs need two outputs. Markdown row chunks with headers are for
future semantic retrieval. Typed records with integer `technology_nm`, date
values, flavor, source row number, and Full/Shared provenance are for exact
filtering. The full file has 33 records and the shared file has 14. Filtering
`28 / HPC+ RF` with tapeout on or after `2026-10-01` returns two rows from
each file; technology and flavor alone are not unique identifiers. The
generic-name copy needs an explicit Shared selection. A contradictory clear
filename and selection is rejected.

## Boundaries of this result

The app validates parsing and chunk preparation. It does not run embeddings,
semantic retrieval, a persistent structured database, or answer generation.
The typed CSV preview demonstrates exact filters in memory. Chunk counts and
keyword checks cannot prove that a future RAG system will retrieve the right
evidence. Before choosing a production strategy, test reviewed questions
against an index and measure whether the correct source appears in the top
results, including failure cases and citations.
