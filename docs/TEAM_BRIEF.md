# Weekly team brief

## What has been built

We separated file parsing from chunking. DOCX, selectable-text PDF, and CSV
are normalized to inspectable Markdown. The chunker accepts only Markdown.
Its first strategy groups FAQ question/answer pairs, table rows with column
headings, and ordinary prose under their section headings. A size-based
baseline, a hybrid strategy with sentence overlap, and a paragraph-aware
strategy allow direct comparison.

Schedule CSVs additionally support a typed-record preview with exact field
and date filters in memory. This does not require a database and is separate
from the Markdown chunks used for future semantic retrieval.

This iteration preserves wide CSV rows that previously risked truncation.
Chunks carry source file, heading path, PDF page when available, table row
number, and an ID linking pieces of a long section. Oversized table rows
remain whole with a warning.

## What can be demonstrated

1. Upload a file and inspect normalized Markdown before chunking.
2. Edit or paste Markdown and inspect chunks under each strategy.
3. Compare all strategies on the same Markdown and one known evidence case
   in the browser, or on many cases with the CLI.
4. Show a normal narrative document whose facts can be chunked even though
   the document does not contain the user's question.
5. Upload the full schedule CSV in section 4, filter by technology, flavor,
   and a tapeout date, and inspect the source row numbers.

## What remains uncertain

PDF tables and multi-column layouts need review against original pages.
An evidence excerpt can be split even when the document is fully parsed.
The lexical ranking probe is not semantic retrieval. We have not chosen a
database, embedding model, LLM, or OCR service for this phase.

## Next decision

Test our actual FAQs, two schedule CSVs, and narrative/technical documents.
Collect incorrect Markdown and chunk output with settings. Fix observed
losses, then benchmark real retrieval with the same questions. Price and
infrastructure decisions follow measured data volume and query needs.
