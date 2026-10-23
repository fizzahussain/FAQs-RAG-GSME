# Parsing and chunking release 0.14.7

Checkout audited: branch `parsing-and-chunking-revised`, starting commit `9735a6d`, version 0.14.6. The supplied latest native manual output was a validation artifact, not the source checkout.

## Safety boundary

- Chunks expose `evidence_status`: `pending` by default, `excluded` for dotted-leader TOC navigation and source-document furniture. `review_required=false` never means approved. Embedding eligibility needs a separate recorded approval for every chunk and a `source_verified` approval for review-marked chunks. Rejected and excluded chunks remain in audit output but are ineligible.
- Chunk IDs include parser and chunker versions; a new parse/chunk version cannot silently reuse an old approval. Source hash, pages, heading path and stable unit IDs remain in records.
- The native PDF fallback runs local text-fidelity diagnostics and review-marks suspect formula pages. On the exact manual, physical page 30's malformed formula unit is now review-required with a `formula_unavailable` diagnostic and source box. This does not transcribe or correct the equation.

## Source checks

- Full native manual: `strategy_used=pymupdf_table_fallback`, 1,420 chunks, 64.327 seconds. Page 30 formula chunk 197 is review-required/pending; page 101 chart and page 163 formula chunks remain review-required/pending. Thirty-six TOC navigation chunks are excluded. All artifacts remain in ignored local `validation/`.
- Four-page local slice of original physical pages 2, 30, 101 and 163: `strategy_used=docling_layout_ocr_to_markdown`, 32 chunks, 62.93 seconds. Slice page 1 reproduces Docling's 22-of-183 cell loss in a 30×3 TOC grid; parser warning records page and box, and the page remains unapproved. Slice pages 2, 3 and 4 preserve formula/chart review gates. Slice page numbers must be mapped back to the original physical pages before citation.

## Not delivered in this stage

There is no PostgreSQL/Qdrant service, approved prose subset, embedding model, GSME-labeled question set, or search evaluation in this checkout. No document content was sent to an external model. Do not treat any `pending` chunk as indexable. Storage, worker recovery, filtered dense/lexical/hybrid retrieval and benchmark metrics require a separate implementation and source-verified approvals.
