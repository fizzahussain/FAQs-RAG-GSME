# Parsing and chunking technical handoff

Version 0.10.0 · 9 October 2026

## Decision and scope

Use deterministic, structure-aware normalization and automatic chunk routing for the known FAQ and document formats. Keep Full and Shared schedule CSVs in a separate typed-record path for exact lookup. The current application is a parsing and chunk preparation demo. It does not implement a database, an embedding index, live retrieval, an LLM, or the future KPI tool. Its output is an inspectable candidate for those systems, not evidence that retrieval accuracy has already been measured.

The pipeline accepts DOCX, digital PDFs with selectable text, and CSV. OCR is deliberately outside this release. Parsing produces Markdown and one document metadata envelope. Chunking accepts only Markdown plus that envelope; it never reopens the uploaded document. Exact schedule filters instead read and validate CSV rows directly. Both downstream routes share a source identity, so they can later be joined without pretending that semantic similarity is an exact date filter.

| Input | Normalization | Default output |
| --- | --- | --- |
| Bold-question FAQ DOCX or PDF | Preserve headings, bold questions, answers and PDF page markers | One question and answer per semantic unit |
| FAQ table DOCX or two-column PDF | Preserve a Markdown table; infer a missing two-column header or PDF columns with a warning | Category, question and answer per semantic unit |
| Narrative or mixed document | Preserve ordinary paragraphs, headings and tables | Paragraph evidence, FAQ pairs and table rows as appropriate |
| Full or Shared schedule CSV | Preserve a readable table and classify the known schedule schema | Typed rows for exact filters; zero default semantic chunks |

## What was corrected in this release

1. The headerless DOCX FAQ test no longer makes a separate 28-character chunk containing only its bold title. The short title and explanatory sentence form one context passage; each real FAQ row is separate.
2. PDF table chunks no longer repeat a filename-derived heading or a full four-column Markdown table. The semantic text holds only a source category, a question and its answer. Page and row location remain metadata.
3. PDF prose lines that are visibly soft-wrapped at a normal line gap join into one paragraph. Bold questions, headings and page boundaries remain explicit. A digital PDF may itself extract letters with unwanted spaces; those are not guessed away.
4. An empty FAQ answer creates a review warning and no semantic evidence chunk. An answer longer than the character budget splits at text boundaries and repeats its question, with a shared unit ID and part numbers.
5. Original schedule exports with `Tech (nm), Flavor, Trial, Final, Tapeout, Est. Ship` and their common empty trailing column now work in structured filtering. Canonical headers such as `technology_nm` also work. A nonempty unexpected column is rejected for structured use instead of silently dropped.
6. Optional schedule discovery chunks use the physical CSV source row from parser metadata. If Markdown is edited after parsing, this row link is withheld with a warning. The structured CSV reader always computes the original physical row itself.
7. Chunk responses omit the document hash, schedule nulls, classification diagnostics, constant channel and one-part splitting metadata from every FAQ chunk. Document hashes and Full/Shared classification remain in the parser envelope; the overall response states the strategy and channel.

## Chunking policy

The `auto` choice applies rules per block, not one label to the entire file. A standalone bold or numbered question ending in a question mark anchors following answer paragraphs until the next question, heading or table. A PDF question split across bold lines is joined first. An italic rhetorical question inside a life-story paragraph remains prose. A Markdown table divider identifies rows; FAQ columns produce a compact question-and-answer projection, while other tables repeat their column headers with each intact row. Source category headings replace sibling headings rather than inheriting an earlier `General` category.

| Choice | Rule | Role |
| --- | --- | --- |
| `auto` | FAQ pairs and rows by structure, separate ordinary paragraphs, split long prose with bounded overlap | Default for unattended known and mixed content |
| `layout_aware` | FAQ pairs and rows by structure, group neighboring ordinary prose within a section or page | Manual experiment for documents with related prose |
| `paragraph_aware` | FAQ pairs and rows by structure, keep ordinary paragraphs apart | Manual experiment for narrative |
| `hybrid` | Group prose by section and use overlap when long sections split | Manual experiment when context crosses prose boundaries |
| `fixed_size` | Split by size with paragraph and sentence fallback | Baseline only; may separate evidence and is blocked for schedule discovery |

The size control is in **characters**, not model tokens. Long FAQ answers can split while keeping their question and row identity. Non-FAQ table rows remain intact so values are not detached from their headers; if such a row exceeds the budget, `oversized_unit` requires inspection. The parser does not infer an answer from a question or repair illegible source text with an LLM. Changes to the routing policy should be driven by a reviewed retrieval benchmark, not by the apparent neatness of chunks.

## Minimal metadata contract

Nothing is persisted in the current demo. The parser response contains `source_id`, source and normalized SHA-256 hashes, `source_file`, `document_type`, `document_class`, version fields and classification status. A schedule additionally has `schedule_kind`, the filename/user origin of that label, and `csv_source_rows` to maintain physical source locations through optional Markdown discovery. Those document-level values are sent once as the metadata object when chunking.

Each semantic chunk carries an ID, text, type, source ID and filename, index and only applicable category or location. A page end appears only when different from the start; heading paths appear when they add context beyond a single category. A split unit also carries `unit_id`, `part_index` and `part_count`. Schedule kind and physical source row appear only on optional schedule discovery chunks. Retrieval channel and selected strategy appear once on the chunk response, not on every item. This keeps a chunk independently attributable without filling FAQ output with schedule nulls or the same document hash repeatedly.

The structured response carries its Full/Shared label and label provenance once. Each typed record carries `record_id`, `source_id`, `source_file`, `source_row_number`, `schedule_kind`, integer `technology_nm`, `flavor` and four validated dates. Repeated technology and flavor values are legitimate separate records; do not deduplicate them. These are the facts a future schedule query or KPI calculation must use. The current application offers exact in-memory filters but does not compute or publish KPI metrics.

For an ambiguously named schedule file, the known header establishes that it is a schedule but cannot establish Full versus Shared. The parser reports `needs_schedule_kind`, and both downstream paths reject it until the user supplies a label. A clear contradictory filename and user label are also rejected. Neither the row count nor dates are used to guess schedule type.

## Evidence from supplied material

At the default 1,800-character budget, the supplied bold-question FAQ DOCX yielded 31 question-answer chunks. The supplied FAQ-table PDF yielded 31 category-labeled table-row chunks, and the separate QA PDF contained the corresponding 31 FAQ questions plus one introductory prose passage. Each had the same category distribution: 8 General, 12 TapeOut Services, 7 Additional Services and 4 Shared Block tapeout. The headerless table test has 3 FAQ rows plus one context passage. The life-story DOCX yielded 8 prose chunks and no invented FAQ question from its rhetorical prose. The original Full and Shared exports produced 33 and 14 typed records respectively; default semantic schedule chunk count is zero. These are structural and content-preservation checks, not retrieval scores.

The automated suite covers 50 scenarios including PDF category changes, wrapped questions, page spans, headerless Word tables, split FAQ answers, missing answers, narrative paragraphs, original and normalized CSV headers, blank trailing columns, physical CSV row numbers and ambiguous schedule labels. The two actual schedule exports were also read as typed records and checked for row counts. Do not claim 100 percent accuracy from these tests: unfamiliar table geometries, merged cells, reading order and unusual headings remain unmeasured.

## Limits and release gate for future retrieval

The PDF table recognizer uses positions and issues `pdf_table_inferred` because arbitrary multi-column PDFs can reorder cells. Inspect a new PDF type against the original before indexing. Digital PDF text extraction can return fractured words (the supplied QA PDF contains examples); automatically repairing them would risk inventing text. Image-only PDFs return `no_pdf_text`. Very large, unusual or malformed DOCX tables also require review. Plain Markdown supplied without parse metadata loses source page and physical CSV row provenance.

For the next phase, assemble reviewed questions with expected passages and schedule rows. Test semantic recall at several top-k values, category and page citations, and structured exact filters separately. Include repeated schedule values, cross-page answers, missing answers, unfamiliar table layouts and long FAQ answers. Choose an embedding model and use its tokenizer to calibrate a token budget before building the persistent index. The future KPI tool must aggregate typed, dated records under a separately approved metric definition. Add access controls before accepting confidential company files on the public demo.

## Run and deploy

Use Python 3.11 or newer. From the project root: `python -m pip install -e .`, `python -m unittest discover -s tests -v`, then `python -m gsme_rag.api.run`. On Windows, call the explicit `.venv\Scripts\python.exe` for all three commands. The browser demo is at `/`, API documentation at `/docs`, and the health route at `/v1/health`. Render uses `render.yaml`, builds with `pip install .`, starts `python -m gsme_rag.api.run` and injects `PORT`. Push the updated branch to trigger its redeploy, then verify the public health route and representative samples.
