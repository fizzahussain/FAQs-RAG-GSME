# GSME RAG Pipeline Demo

This project is the foundation of the GSME RAG pipeline. Its current implementation separates parsing from chunking so later retrieval, embedding, storage, and generation stages can be added without renaming the Python package.

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
Chunker: Markdown to layout-aware chunks
```

The parser never creates chunks. The chunker never opens the original source file.

There is no database, embedding model, semantic search, structured search, or OCR in this version.

## Project structure

```text
src/gsme_rag/
├── parsing/
│   ├── parser.py
│   ├── docx.py
│   ├── pdf.py
│   ├── csv.py
│   └── markdown.py
├── chunking/
│   └── chunker.py
├── api/
│   ├── app.py
│   ├── routes.py
│   └── schemas.py
├── models.py
├── text.py
└── cli.py
```

The `parsing` package can only normalize source files to Markdown. The `chunking` package receives Markdown text and has no source-file parsing dependencies. The `api` package connects the two stages through separate endpoints.

## Stage 1: parsing

The parsing endpoint accepts one supported file and returns normalized Markdown.

| Input | Markdown normalization |
| --- | --- |
| DOCX | Headings, formatted paragraphs, lists, and native tables |
| Born-digital PDF | Reading order, headings, bold text, page markers, and detected table layout |
| CSV | One Markdown table containing the original headers and rows |

Parsing preserves useful structure but does not select retrieval chunks.

## Stage 2: chunking

The chunking endpoint accepts Markdown text. It does not receive a DOCX, PDF, or CSV file.

The layout-aware chunker:

- keeps a bold or numbered question with its following answer
- creates one chunk per Markdown table row while repeating the table header
- keeps heading context with every chunk
- tracks PDF page markers when they are available
- splits long sections using the configured character limit
- returns deterministic chunk identifiers and source metadata

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
  "max_chars": 1800
}
```

Response shape:

```json
{
  "source_file": "Muse_FAQs.docx",
  "max_chars": 1800,
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
      "character_count": 59
    }
  ],
  "warnings": []
}
```

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

## Test

```bash
pip install -e ".[test]"
python -m unittest discover -s tests -v
```

The tests verify the separation by checking that `/v1/parse` returns Markdown without chunks and that `/v1/chunk` works without receiving the original document.
