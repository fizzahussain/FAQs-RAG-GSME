# Project audit and release 0.15.5

Audited on 29 October 2026, on the `embeddings-and-storage` branch. The scope covers parsing, Markdown normalization, chunking, source review, embedding preparation, PostgreSQL/Qdrant storage, the local console, package resources and the evidence-search boundary. The release is ready for developing retrieval against approved evidence. It does not certify every extracted formula, table cell or visual relationship.

## Findings fixed

| Finding and root cause | Change | Regression / verification |
| --- | --- | --- |
| Compatibility normalization changed `x²` to `x2` | Use NFC and retain technical symbols | `test_technical_symbols_are_not_compatibility_normalized` |
| Question headings became section context rather than Q&A | Recognize question headings while keeping their parent category | Heading-pair and source FAQ checks |
| Native numbered sections lost their chapter/depth | Derive numbered heading depth independently of font size | `test_native_numbered_sections_keep_chapter_and_nested_levels` |
| DOCX hyperlink/nested-table text could disappear; equation layout could flatten | Read hyperlink runs, preserve nested text/order and super/subscript markup; flag merged/nested cells; exclude untranscribed equation placeholders | DOCX source-retention regression; original DOCX comparisons |
| Escaped pipes, styled FAQ headers, extra columns or malformed rows lost meaning | Parse escaped boundaries, recognize header aliases, retain additional labeled fields, review malformed rows | Table roundtrip, styled-header, extra-column and oversized-row cases |
| Simple PDFs bypassed fidelity checks; inferred FAQ tables lacked review markers | Scan the simple native path and mark every inferred PDF table row | Simple-formula regression and real FAQ PDF runs |
| Margin filtering counted repeated occurrences rather than distinct source pages | Count distinct pages and protect matching body text; retain canonical source metadata separately | Repeated-header regression and full manual checks |
| Image-derived headings/context could leak; visual gaps could exclude a neighboring real caption | Keep image children as reviewed text, reset picture state at page boundaries, isolate OCR and visual-gap units | Image-heading/context and caption-isolation regressions |
| Unlocated Docling cell drops lost their raw evidence or could be assigned to an ambiguous page | Require a unique location and preserve unlocated dropped text with unknown page explicitly | Unlocated-cell regression and real 183-cell grid comparison |
| Fenced technical code could be changed or treated as page/review instructions | Preserve fenced code, indentation and literal markers | `test_code_keeps_indentation_and_literal_page_markers` |
| Parser/chunker identities stayed at 0.14.7 after algorithm changes | Share release constants; record extraction setup; prevent no-op reuse across revisions/setup changes | Parser-upgrade ingestion test and source provenance checks |
| Review stopped at 100 chunks and UUID order differed from source order | Migration 003 stores source ordinals, unit/split identity and paginated inspection | 105-chunk source-order integration test |
| Evidence could be rewritten beneath an earlier approval | Add database immutability enforcement; keep source checksums and decisions separate | PostgreSQL rewrite rejection test; source-change/tamper checks |
| Reclaimed ingestion attempts could overwrite each other's artifact files | Store artifacts per version and worker attempt | Lease protection plus attempt-specific paths |
| A bad candidate failed its healthy neighbors; retries could continue indefinitely | Fail invalid candidates individually, cap automatic attempts at five, allow explicit retry through publication | Mixed good/over-budget integration test |
| Invalid vectors, empty indexes or missing derived points were poorly handled | Validate finite/nonzero vectors, check authoritative indexed evidence, filter by tenant/model, add scoped missing-vector repair | Provider/vector tests, no-external-call empty-index test, repair integration test |
| Publication could reset a running index job; review used inconsistent lock order | Preserve active jobs and use document-before-chunk locking | Existing publication/revocation/lease integration checks |
| Console controls did not explain approval vs publication vs indexing | Show per-chunk review/index state and errors, paginate source order, disable unavailable previews, add typed schedule filters and service/OCR health checks | Authenticated console integration and JavaScript syntax check |
| CLI parse output omitted raw diagnostics; evaluation IDs could match the wrong source version | Write review sidecars and require version/hash-pinned source-reviewed evaluation references | CLI output inspection; evaluation validation regression |
| Wheel installs omitted migration SQL and browser files; Docker build context could include local files | Package SQL/assets, apply migrations under a lock, use selective Docker copies and explicit exclusions | Built wheel resource/integrity inspection and dependency check |

Comments explain the review/lease boundaries, source identities, token-vs-character limits and non-obvious parsing steps. Variable names such as `table_needs_review` now describe their actual role. Historical audit/change notes and two superseded inspection scripts were moved to ignored local `validation/` archives. The repository keeps three current guides, tests and the small synthetic fixtures required to reproduce regressions.

## Source verification

| Source / path | Observed result |
| --- | --- |
| Complete prose FAQ DOCX variants and prose FAQ PDF, native path | 31 Q&A chunks each. All 31 reference DOCX answers survive in each of the two styled DOCX variants and the prose PDF after whitespace/Markdown comparison. No oversized chunks. |
| Website-copy DOCX | 31 Q&A chunks. |
| Complete table FAQ PDF | 31 inferred rows; all review-marked. |
| Table DOCX / deliberately incomplete table PDF | 30 answer-bearing rows. The DOCX source has a genuinely blank first answer; the missing-answer case remains a warning rather than invented evidence. Merged DOCX cells require review. |
| Full/Shared CSVs | Typed schedule route; no semantic chunks by default. A generically named schedule is blocked until its kind is selected. Empty DOCX yields an explicit no-content warning. XLSX remains unsupported. |
| Three-page validation PDF, native | `pymupdf_table_fallback`, paper route, 21 chunks. Both table rows reviewed, one reviewed `LOT A17`, correct numbered context and page-3 attribution/review, chart labels retained, one Figure 2 caption, repeated header/footer absent. Latest run: 1.801 seconds. |
| Same PDF, real Docling | `docling_layout_ocr_to_markdown`, paper route, 24 chunks. The same source checks pass; OCR has no inherited later-section heading. Chart text remains reviewed without asserting its mapping. Latest run: 50.186 seconds; the earlier cold run took 91.789 seconds. |
| Full MFG-GSME-0002, native, 175 pages | `pymupdf_table_fallback`, paper route, 1,439 chunks, 901 review-marked, 674 table-row chunks, zero unreviewed table rows and zero oversized chunks. All 45 generic visual units are excluded; repeated confidentiality text is absent. Run: 96.770 seconds. |
| Full manufacturing slides, native, 188 pages | `pymupdf_table_fallback`, paper route, 802 chunks, 551 review-marked, 157 table-row chunks, zero unreviewed table rows and zero oversized chunks. All 188 visual-gap units are excluded. Run: 140.032 seconds. |
| Original manual pages 2, 30, 101, 163, real Docling slice | `docling_layout_ocr_to_markdown`, paper route, 32 chunks, 31 review-marked, zero oversized. Run: 73.301 seconds. The artifact records the parent source hash and slice-to-original map `1→2, 2→30, 3→101, 4→163`; slice page numbers are not original-document citations. |

The original three-page PDF and manual pages 2, 30, 101 and 163 were rendered and visually inspected. The source comparisons confirm text presence and the review gates; they do not approve the manual's unverified equations or chart claims. Timings are local Windows observations with other checks running, not controlled performance benchmarks. A fresh full 175-page Docling conversion was not run in this release.

## Exact remaining source work

- **Manual page 2:** the real Docling run reproduced the unsuppressed warning: **22 of 183 cells dropped from a 30×3 grid**. All 183 raw cells are retained. Eighteen dropped cells contain text and four contain dot leaders. All 30 visible contents entries survive as recovered prose, but this grid has zero exported structured table rows. Of 156 content-bearing cells, 155 match the recovered text exactly; all 156 match when straight/curly apostrophes are folded for comparison. This is text coverage, not verified cell-to-row/column structure.
- **Page 30:** the DPPM fraction remains damaged in extracted text. The raw region/bounding box and `formula_unavailable` diagnostic remain linked to the source. It needs verified transcription before use as an approved formula.
- **Page 101:** six chart percentages are retained as unmapped labels. Their package/category associations have not been approved.
- **Page 163:** control-chart fractions/symbol positions and the yield model need verified transcription. Source-page review flags remain in both backends.
- Other complex tables, diagrams and OCR passages still require source review. Corpus-wide approval and mathematical reconstruction are not automated.

## Service, provider and packaging verification

The final suite passed **170 tests, zero skips**, in 37.398 seconds with an isolated PostgreSQL database, real Qdrant, Tesseract, and the optional private-manual path configured; an earlier full run took 96.412 seconds. The suite uses fake embeddings and does not call Cohere. It covers source-order pagination, pending/rejected exclusion, tenant/version filtering, revocation before cleanup, replacement, retry/no-op behavior, missing-vector repair, typed schedules, and the authenticated console.

A separate live Cohere Pro/Fast smoke used only a newly generated synthetic FAQ: one document embedding, expected first citation, no-op resubmission, zero hits after revocation, and one removed Qdrant point. No private GSME document was sent in this release's provider smoke. This verifies provider/storage plumbing, not representative retrieval quality.

Validated environment: Python 3.13; FastAPI 0.162.0; PyMuPDF 1.28.2; python-docx 1.2.0; Docling 2.145.0 with TableFormer models 4.0.4; Cohere SDK 5.21.1; qdrant-client 1.20.0; psycopg 3.3.6; Tesseract 5.5.3 with English data visible to the parser process. Dependency checking reports no broken requirements. SQL migrations and all three browser assets are included in the wheel. `.env`, originals, generated validation output, release ZIPs and local data are excluded from Git and image copies. Migration 003 was tested on the isolated database and applied to the local development database.

The next retrieval work should use currently approved evidence and the version/hash-pinned evaluation schema. Build a reviewed GSME question set before claiming Recall@5/10 or answer quality. Production identity, backups/restore, retention and capacity tests remain operational work beyond the local administrator setup.
