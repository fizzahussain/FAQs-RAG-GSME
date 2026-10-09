# Parsing and chunking: technical decision and audit

Version 0.9.1 · Week 1 parsing; week 2 chunking · 9 October 2026

## Scope and current status

The project prepares two future retrieval channels. FAQ prose, FAQ tables, other documents and optional schedule descriptions produce semantic evidence chunks. Full and Shared schedules are validated as typed rows for exact filters. The current app previews the results in memory. It does **not** persist metadata, embed content, retrieve by vector similarity, call an LLM, or answer questions. Digital PDFs only; an image-only PDF reports `no_pdf_text`.

The parser and chunker have a hard interface: `parse_to_markdown(file)` returns normalized Markdown plus a document metadata envelope, and `chunk_markdown(markdown, metadata)` does not open the file. A schedule schema also has a typed record reader; the exact-filter endpoint reads these records directly. In a future ingestion worker, the parser should emit both outputs in one transaction, then send each to its chosen index.

```text
DOCX / digital PDF ── parser ── Markdown + source metadata ── auto chunker ── semantic candidates
CSV schedule ──────── parser ── Markdown preview + metadata
                 └── typed row validation ── exact filters / future structured store
                                      └── optional labeled row projection ── semantic candidates
```

### Answer to the structured-data question

No chunking is needed for exact schedule lookup. The record is already the atomic unit: technology is an integer, dates are dates, flavor is a string, and source row distinguishes repeated combinations. A table-row chunk is useful only as a **separate, optional** natural-language discovery projection. Never use semantic similarity to decide an exact date or which schedule type a row belongs to. Markdown is readable normalization, not the canonical type system for date filtering.

## Four manual methods, and the operational default

| Method | Behavior | Useful for | Problem with using it everywhere |
| --- | --- | --- | --- |
| `fixed_size` | Split at size boundaries with paragraph/sentence/word fallback | Baseline to show what arbitrary size loses | FAQ pairs and table rows can be detached |
| `paragraph_aware` | FAQ and table units stay intact; ordinary prose is a paragraph per unit | Narrative, mixed prose with short paragraphs | Many small chunks; headings and context can be repeated |
| `layout_aware` | FAQ pairs and table rows atomic; neighboring prose combines within section/page | FAQ and table documents | Can merge unrelated narrative paragraphs |
| `hybrid` | Layout rules plus sentence overlap when a prose section needs splitting | Long narrative near boundaries | Duplicated text, bigger index, no benefit for short Q&A or rows |
| `auto` **default** | FAQ pairing and table rows by block, prose paragraph units, sentence overlap only when prose exceeds budget | Unattended ingestion of known and mixed formats | Unknown layouts still need review and empirical retrieval tests |

The four selectable methods are experiments; `auto` is the policy for future unattended ingestion. The browser resets to Auto after a parse. The backend routes each block, so the caller need not know whether a whole document is a table, FAQ, or life story. Bold or numbered standalone questions ending in `?` become anchors followed by answer paragraphs until another question/table/heading. A numbered question split across bold PDF lines is joined before pairing. The PDF parser normalizes same-size category headings as siblings; the chunker also replaces sibling headings even for older Markdown that starts with `## General` and has no document title. A Markdown table divider identifies a table; every data row repeats its header. Other paragraphs stay prose. Rhetorical italic questions inside a paragraph are not treated as FAQ anchors. Headings and page markers become context and provenance. When a unit exceeds `max_chars`, prose and Q&A answer bodies split while retaining their anchor; table rows remain intact and warn `oversized_unit`. The limit is **characters**, not embedding tokens.

The code does not use an LLM for type classification. A lightweight LLM could help with nonstandard layouts later, but should be added only after a labeled failure set shows deterministic rules failing, with cost, latency and review of uncertain results measured. Do not claim the current pipeline understands any arbitrary document.

## Metadata contract

The response carries metadata; nothing is written to a database today. Keep it with the future stored row or vector:

| Level | Field | Purpose |
| --- | --- | --- |
| Document | `source_id`, `content_sha256` | Stable content identity and reprocessing detection |
| Document | `normalized_sha256`, `parser_version`, `schema_version` | Identify the exact Markdown and parser contract used |
| Document | `source_file`, `document_type`, `document_class` | Display source and route schedule vs document |
| Document | `schedule_kind`, `schedule_kind_source`, `classification_status` | Full/Shared, label provenance (filename or user), and readiness |
| Chunk | `chunk_id`, `unit_id`, `index`, `part_index`, `part_count` | Deterministic chunk IDs and reconstruction of split units |
| Chunk | `chunk_type`, `category`, `heading_path`, `source_page_start/end`, `table_row_number`, `source_row_number` | Explicit FAQ category, evidence type, location and citation context |
| Chunk | `strategy_used`, `routing_reason`, `retrieval_channel` | Audit how a semantic unit was formed |
| Structured record | `record_id`, `source_id`, `source_file`, `source_row_number`, `schedule_kind/source`, typed fields | Exact row identity, filters and provenance |

`source_id` is based on source bytes plus its schedule label, and chunk IDs include source identity, Markdown hash, strategy, index and content. Identical bytes with the same label can share a source ID across filenames; a production store should add tenant, collection, logical document key and ingestion version to prevent cross-collection collisions. The demo makes no claim to provide tenancy or access control. Editing normalized Markdown causes `markdown_edited` and a new normalized hash; the displayed source ID still identifies the original upload.

For a generic schedule filename, schema recognition gives `document_class=schedule` but **cannot** determine Full versus Shared from row count, dates, or matching another uploaded CSV. The parser returns `needs_schedule_kind`. Both the chunk endpoint and exact-filter endpoint reject unlabeled rows with HTTP 422. Reparse with a user-supplied Full/Shared label, whose provenance is recorded. A conflicting filename and user label is also rejected.

## Real-file observations

These are deterministic structure checks using the attached examples, not retrieval accuracy measurements.

| Attachment | Auto result | Baseline / noteworthy behavior |
| --- | --- | --- |
| Bold-question FAQ DOCX | 31 question–answer chunks | Fixed size yields 5 large chunks and can sever pairs |
| Four-page FAQ-table PDF | 31 table-row chunks | Fixed size yields 7 chunks; row metadata retains page or page range |
| Life-story DOCX | 8 prose chunks | Layout-aware merges to 4 sections; rhetorical questions stay prose |
| Full schedule CSV | 33 typed rows; 0 default chunks | Optional projection: 33 labeled row chunks |
| Shared schedule CSV | 14 typed rows; 0 default chunks | Optional projection: 14 labeled row chunks |
| Generic filename copy | review required | Explicit Shared selection yields 14 typed rows and optional 14 chunks |

The automated suite includes 40 tests covering DOCX, PDF, CSV, headerless FAQ tables, mixed Markdown, category boundaries, wrapped PDF questions, page ranges, labels, IDs and ambiguous source blocking. Validation stops at parsing/chunk integrity: there is no embedding model or retrieval corpus for recall@k, MRR or answer faithfulness yet. Next, assemble 20–50 realistic questions with gold source rows or passages, compare retrieval after indexing, and tune token budgets/overlap on that evidence. Large merged cells, multi-column PDF ordering and ambiguous table header inference remain manual-review risks.

## External review and why this implementation stays small

Docling describes hierarchical chunking with metadata and token-aware refinements, including repeating table headers. Unstructured isolates tables, groups by title and only splits oversized elements. LangChain offers Markdown header splitters. These support a structure-first design, while their general-purpose dependency surfaces would add complexity to this bounded demo. They are candidates if new documents defeat the current parser. Reddit discussions about mixed prose/tables and LinkedIn practitioner posts raise the same table-context concern; they are anecdotal, not benchmark evidence.

- Docling official docs: https://docling-project.github.io/docling/concepts/chunking/
- Unstructured official docs: https://docs.unstructured.io/concepts/chunking
- LangChain source: https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/markdown.py
- Reddit discussion (practitioner anecdote): https://www.reddit.com/r/Rag/comments/1v2vkie/how_do_people_usually_handle_chunking_for/
- LinkedIn discussion (practitioner anecdote): https://www.linkedin.com/posts/v-tsybin_how-to-work-with-tables-in-a-rag-system-activity-7466447615799422976-Wv9j

## Next engineering steps

1. Agree on benchmark questions and expected source rows/passages, including date comparisons, repeated schedule combinations, long answers and PDF cross-page tables.
2. Choose an embedding model, count with its tokenizer, and measure retrieval metrics. Tune chunk sizes and overlap from those results.
3. Add a structured store for typed schedules and a semantic index for chunks; carry metadata through ingestion and deletion/versioning.
4. Add retrieval routing and join results by `source_id`; only then add answer generation with citations.
5. Add authentication, upload controls and operational logging before using confidential source files on a public deployment.
