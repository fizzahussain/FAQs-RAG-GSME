# GSME RAG: Parsing and Chunking Lab

This is a runnable lab for the first two stages of the GSME RAG pipeline. Parsing output is inspectable and chunking decisions are comparable before retrieval, storage, and answer generation are added.

```text
DOCX, digital PDF, or CSV
            |
            v
Parser: file to normalized Markdown
            |
            v
Editable and inspectable Markdown
            |
            v
Chunker: Markdown to comparable chunks
```

The parser never creates chunks. The chunker never opens the original source file.

Schedule CSVs can also be inspected as typed records with in-memory filters.
There is no database, embedding model, semantic retrieval, or OCR.

## Project structure

```text
src/gsme_rag/
├── parsing/
│   ├── parser.py
│   ├── docx.py
│   ├── pdf.py
│   ├── csv.py
│   ├── schedules.py
│   └── markdown.py
├── chunking/
│   ├── chunker.py
│   └── evaluate.py
├── api/
│   ├── app.py
│   ├── routes.py
│   └── schemas.py
├── models.py
├── text.py
└── cli.py
docs/
├── CHUNKING_STUDY.md
└── TEAM_BRIEF.md
```

The `parsing` package normalizes source files to Markdown and can separately
read the known schedule CSV fields as typed records. The `chunking` package
receives Markdown text and has no source-file parsing dependencies. The `api`
package connects the stages through separate endpoints.

## Stage 1: parsing

The parsing endpoint accepts one supported file and returns normalized Markdown.

| Input | Markdown normalization |
| --- | --- |
| DOCX | Headings, formatted paragraphs, lists, and native tables |
| Born-digital PDF | Reading order, headings, bold text, page markers, and detected table layout |
| CSV | One Markdown table containing the original headers and rows |

Parsing preserves useful structure but does not select retrieval chunks.
Schedule CSVs have an optional typed-record preview for exact filtering. The
extra output never enters the chunker. Other CSV schemas still parse to
Markdown but are not accepted by the schedule preview.

## Stage 2: chunking

The chunking endpoint accepts Markdown text. It does not receive a DOCX, PDF, or CSV file.

The default layout-aware chunker:

- keeps a bold or numbered question with its following answer
- creates one chunk per Markdown table row while repeating the table header
- keeps heading context with every chunk
- tracks PDF page markers when they are available
- splits long sections using the configured character limit
- returns deterministic chunk identifiers and source metadata
- records table row numbers and links parts of a long section with `unit_id`

For a chunking comparison, select `fixed_size` (simple size baseline),
`layout_aware` (the default), `hybrid` (layout-aware with overlap in long
narrative sections), or `paragraph_aware` (ordinary narrative paragraphs as
separate units). `overlap_chars` only affects `hybrid` and must be no more
than one quarter of `max_chars`. Table rows stay together in layout-aware and
hybrid modes. When a row exceeds the size limit, the response includes an
`oversized_unit` warning; it does not silently break a record. Hybrid repeats
only complete trailing sentences that fit within the overlap budget.

CSV normalization preserves extra columns in uneven rows, adds neutral labels
for missing headers, and warns about them. Ordinary prose remains prose without
invented questions. Review inferred PDF table layouts against the source pages.

The choice is a candidate for retrieval experiments, not a measured retrieval
recommendation yet. The default `max_chars` is a character count, not tokens.

## Run the demo

Python 3.11 or later is required.

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS or Linux
source .venv/bin/activate

pip install -e .
gsme-rag-api
```

Create the virtual environment only once in a fresh project folder. If your
prompt already starts with `(.venv)`, use that environment and skip the
`python -m venv .venv` step. Check `pyproject.toml` in the extracted folder:
the current lab is version `0.7.0` and contains a `tests` directory.

Open:

```text
http://localhost:8000
```

The browser demonstrates the boundary directly:

1. Upload a source file
2. Parse it to Markdown
3. Inspect or edit the Markdown
4. Send the Markdown to the separate chunking endpoint
5. Inspect the returned chunks

Change the strategy and run step 4 again on the same Markdown to compare
boundaries. An ordinary narrative example is in `examples/narrative.md`.

The browser also compares all four strategies and can check one question
against an exact evidence excerpt. Leave the question and excerpt blank to
compare chunk sizes and warnings alone.

For schedule CSVs, section 4 previews typed rows independently from Markdown.
Try technology `28`, flavor `HPC+ RF`, and tapeout on or after `2026-10-01`.
Several rows may share a technology and flavor but have different dates.

Interactive FastAPI documentation is available at:

```text
http://localhost:8000/docs
```

## API

### Parse a file to Markdown

```text
POST /v1/parse
```

Multipart form field:

| Field | Required | Value |
| --- | ---: | --- |
| `file` | Yes | `.docx`, `.pdf`, or `.csv` |

Example:

```bash
curl -X POST http://localhost:8000/v1/parse \
  -F "file=@Muse_FAQs.docx"
```

Response shape:

```json
{
  "filename": "Muse_FAQs.docx",
  "document_type": "docx",
  "strategy_used": "docx_structure_to_markdown",
  "markdown": "# General\n\n**01 What is Muse?**\n\nMuse provides...\n",
  "markdown_character_count": 68,
  "warning_count": 0,
  "warnings": []
}
```

### Chunk Markdown

```text
POST /v1/chunk
```

JSON body:

```json
{
  "source_file": "Muse_FAQs.docx",
  "markdown": "# General\n\n**01 What is Muse?**\n\nMuse provides...",
  "max_chars": 1800,
  "strategy": "layout_aware",
  "overlap_chars": 120
}
```

Response shape:

```json
{
  "source_file": "Muse_FAQs.docx",
  "max_chars": 1800,
  "strategy": "layout_aware",
  "overlap_chars": 0,
  "chunk_count": 1,
  "warning_count": 0,
  "chunks": [
    {
      "chunk_id": "chunk-123456789abc",
      "index": 0,
      "text": "# General\n\n**01 What is Muse?**\n\nMuse provides...",
      "chunk_type": "question_answer",
      "source_file": "Muse_FAQs.docx",
      "heading_path": ["General"],
      "source_page_start": null,
      "source_page_end": null,
      "table_row_number": null,
      "unit_id": "unit-123456789abc",
      "part_index": 0,
      "part_count": 1,
      "character_count": 59
    }
  ],
  "warnings": []
}
```

### Compare strategies

```text
POST /v1/compare
```

The JSON body accepts `markdown`, `source_file`, `max_chars`,
`overlap_chars`, and optional `cases` with `question` and `evidence` fields.
The report shows chunk sizes, warnings, evidence retention, and a keyword
ranking probe for the same Markdown. A case absent from the Markdown is
marked invalid. These are diagnostics, not semantic retrieval results.

### Preview typed schedule rows

```text
POST /v1/schedules/preview?technology_nm=28&flavor=HPC%2B%20RF&tapeout_on_or_after=2026-10-01
```

Upload a `.csv` file in the `file` multipart field. The known schema requires
`technology_nm`, `flavor`, `trial_deadline`, `final_deadline`, `tapeout_date`,
and `estimated_ship_date`. Dates accept `M/D/YYYY` or `YYYY-MM-DD`; the JSON
response shows ISO dates, CSV row numbers, total records, and matched records.
Invalid dates produce row-specific errors. Generic CSV normalization under
`/v1/parse` remains available for other headers. No database is required.

### Architecture description

```text
GET /v1/architecture
```

### Health check

```text
GET /v1/health
```

## Command line

Parse a source file to Markdown:

```bash
gsme-rag parse input.docx --output normalized.md
```

Chunk the Markdown in a separate command:

```bash
gsme-rag chunk normalized.md --output chunks.json --max-chars 1800
```

Repeat with `--strategy fixed_size`, `--strategy paragraph_aware`, and
`--strategy hybrid --overlap-chars 120`.

Compare all strategies with known evidence:

```bash
gsme-rag compare examples/narrative.md --cases examples/narrative_cases.json --max-chars 400 --overlap-chars 60 --output narrative_report.json
```

Read `docs/CHUNKING_STUDY.md` for the evaluation method and
`docs/TEAM_BRIEF.md` for the weekly update. The example cases demonstrate the
workflow; replace them with reviewed questions from your documents.

## Test

```bash
pip install -e .
python -m unittest discover -s tests -v
```

The tests cover parsing, chunking, comparison, and direct FastAPI route logic.
They do not start an HTTP server. Run the browser demo on your machine for the
final upload and interaction check.
