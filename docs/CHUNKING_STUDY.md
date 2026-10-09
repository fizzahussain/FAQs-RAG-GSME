# Chunking study: how to defend the decision

Historical strategy comparison. Current automatic routing and structured schedule rules are in [TECHNICAL_GUIDE.md](TECHNICAL_GUIDE.md).

## Scope

The parser produces Markdown from DOCX, selectable-text PDF, or CSV. The
chunker consumes only that Markdown. This study measures chunk boundaries;
it does not claim to measure semantic retrieval or answer generation.

## Strategies in this deliverable

| Strategy | Rule | Useful comparison |
| --- | --- | --- |
| `fixed_size` | Break Markdown by paragraphs, sentences, then words when needed | A simple baseline; may separate a table header from its row |
| `layout_aware` | Keep recognized FAQ pairs and table rows together; carry section headings | Current candidate for FAQs and schedules |
| `hybrid` | Apply layout-aware rules, with overlap between long prose chunks | Candidate for long narrative and unfamiliar prose |
| `paragraph_aware` | Keep narrative paragraphs as independent units, with headings when present | Documents without headings, such as the life-story example |

`max_chars` is a character limit, not a model-specific token budget. A table
row or repeated question/heading that exceeds this limit stays intact and
produces `oversized_unit`. Read the warning; a large value is deliberate.

## What the report actually measures

- Evidence retention: does one chunk contain a known excerpt in full?
- Lexical hit@3 and mean reciprocal rank: a small keyword-only ranking probe.
- Chunk count, average and maximum length, oversized count, and warnings.

An excerpt missing from the Markdown is marked invalid and excluded from
strategy scores. Fix parsing first. Evidence retention does not show that a
semantic index will retrieve the chunk, and lexical hit@3 is not vector recall.
Short excerpts can match unrelated chunks; choose a distinctive sentence or
complete row and review the matching chunk IDs manually.

The uploaded life story has eight normal paragraphs but no headings. At the
default 1,800 characters, `fixed_size`, `layout_aware`, and `hybrid` each make
four chunks, while `paragraph_aware` makes eight. It does not invent FAQ
entries for rhetorical questions inside the narrative. Hybrid overlap repeats
complete sentences if they fit the configured overlap budget; an overlap may
be absent when no complete sentence fits.

The four-page numbered FAQ PDF produces 31 inferred question/answer rows.
Check inferred columns and page references against the source. At 400
characters, 12 rows exceed the limit and remain whole with warnings. The full
schedule CSV has 33 rows. Each row is a good candidate for semantic chunking,
while the typed schedule preview handles exact fields and dates independently.

In the supplied Word FAQ, fixed size leaves 27 of 31 complete answers in a
single chunk; the supplied PDF leaves 26 of 31 complete rows in one chunk.
Layout-aware keeps all 31 units intact in each case at the default size. The
PDF also has three answers that span pages. The parser records page ranges in
the normalized table and the chunk metadata. In the synthetic headerless FAQ
table, paragraph-aware makes separate title and introduction chunks, while
layout-aware keeps the introductory prose together and the three rows intact.

The localhost page now suggests a method after parsing and lets you inspect
one strategy at a time. The comparison interface and evidence-excerpt fields
were removed from the page. CLI comparison remains available for technical
evaluation; it does not imply that the future semantic search has been tested.

## Repeatable run

```bash
gsme-rag compare examples/narrative.md --cases examples/narrative_cases.json --max-chars 400 --overlap-chars 60 --output narrative_report.json
gsme-rag compare examples/faqs.md --cases examples/faq_cases.json --max-chars 400 --overlap-chars 60 --output faq_report.json
```

To evaluate a CSV, parse it first and supply cases whose evidence appears
exactly in the normalized Markdown:

```bash
gsme-rag parse examples/shared_schedules.csv --output shared.md
gsme-rag compare shared.md --cases examples/schedule_cases.json --output shared_report.json --max-chars 400 --overlap-chars 60
```

With your own files, prepare at least five FAQ questions, five schedule rows,
and five narrative questions. For each, record the question and one exact
evidence excerpt in a JSON list like `examples/narrative_cases.json`. Keep
the same normalized Markdown, cases, and size across strategies. Compare
errors before picking a winner.

## Decision gate

1. No silent loss of facts or table columns during normalization.
2. FAQ questions stay with answers and table rows retain their headers.
3. Narrative evidence fits in a chunk without excessive repeated text.
4. Investigate every failed case and oversized warning against the source.
5. Once embeddings and an index exist, rerun the same questions and score
   actual retrieval recall@3/5 before finalizing the production strategy.

If the source is a scanned PDF, the current parser reports no selectable
text. OCR, LLM extraction, and external services remain future experiments.
