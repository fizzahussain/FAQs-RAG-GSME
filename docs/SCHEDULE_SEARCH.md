# Exact tapeout schedule search — 0.18.0

Schedule facts come from validated source records in PostgreSQL, not semantic similarity or an LLM. FAQ and manufacturing questions continue through hybrid/dense retrieval, mandatory reranking and optional cited generation. This separation prevents a relevant FAQ passage from inventing a tapeout date.

## Input and authority

- Known Full/Shared CSV exports accept canonical columns or `Tech (nm), Flavor, Trial, Final, Tapeout, Est. Ship`.
- XLSX supports separate named `full` / `shared` sheets, including the existing combined workbook. Physical sheet names (including original whitespace), row numbers, original file and SHA-256 remain available. Ordinary FAQ workbooks still take the document path.
- Dates accept ISO, US month/day/year export strings, or real Excel date cells. Technology must be a positive integer. All four milestones are required and must be ordered trial <= final <= tapeout <= estimated ship. A source exception requires explicit source review/schema work, not silently changing its dates.
- Duplicate business rows are preserved. Blank CSV lines do not shift source-row attribution. Blank trailing columns are allowed, nonempty extra cells are rejected.
- Formula/error cells, hidden schedule data/headers, merged cells, damaged flavor text, unknown columns and mixed schedule/non-schedule populated sheets block typed publication. No macros, formulas or external workbook links are executed.
- Whole-file and source hash checks still apply. Workbook publication is atomic across both schedule kinds. A failed replacement leaves the previous published version searchable.
- Acceptance means typed schema validation, **not human verification or live booking availability**. Import time is not a claimed upstream update date. Source files are not automatically crawled or refreshed.

## API

Run migration 005 before using workbook schedules/new queries:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m gsme_rag.storage.admin
```

The existing local token and loopback/origin controls apply. No Cohere key, external-content permission, embedding or reranking is needed for a schedule-only query.

`POST /api/schedule-search` accepts:

```json
{
  "tenant": "local-test",
  "question": "Next shared 28nm tapeout",
  "timezone": "Asia/Karachi",
  "limit": 20
}
```

The question can be empty when explicit filters are supplied:

```json
{
  "tenant": "local-test",
  "schedule_kind": "both",
  "technology_nm": 28,
  "flavor": "HPC+ RF",
  "date_field": "final_deadline",
  "date_from": "2026-11-01",
  "date_to": "2026-12-31",
  "deadline_status": "all",
  "limit": 20
}
```

Supported fields:

| Field | Meaning |
| --- | --- |
| `schedule_kind` | `full`, `shared`, `both`; unspecified kind asks for clarification |
| `technology_nm`, `flavor` | Exact integer and case-insensitive exact flavor; no fuzzy category guessing |
| `date_field` | `trial_deadline`, `final_deadline`, `tapeout_date` (default), `estimated_ship_date` |
| `date_from`, `date_to` | Inclusive bounds on that milestone |
| `deadline_status` | `all`; `open` means final deadline >= as-of; `closed` means final deadline < as-of |
| `next_dates` | Nearest 1–100 distinct milestone dates, preserving all rows tied on those dates; defaults start to today when no start is supplied |
| `as_of`, `timezone` | Optional explicit reference date; otherwise current date in IANA timezone, default Asia/Karachi |
| `document_id` | Select a particular logical source; otherwise preserve independent/overlapping sources |
| `limit`, `offset`, `snapshot` | 1–999 rows per page; use returned `next_offset` and `snapshot` for continuation |

Count, source list and rows are read in one repeatable-read database snapshot. Responses include total matches, `has_more`, stable version/sheet/row record IDs, source/normalized hashes, import time and authenticated original-source URL. Offset pagination requires a snapshot; changing sources, filters, scope or reference day returns **409** rather than silently mixing result sets. Deterministic ordering preserves equal-date rows. A separate new document is not assumed to supersede another: replace using its existing `document_id`.

`GET /api/schedule-catalog?tenant=local-test` lists exact kind/technology/flavor combinations and per-source date coverage. Optional document scope applies. Catalogs explicitly signal more than 1,000 facets/sources rather than claiming completeness.

`POST /api/retrieve` with `route="auto"` uses the same planner. `route="schedules"` also accepts exact filters; pagination fields there are `schedule_limit`, `schedule_offset`, `schedule_snapshot`. The legacy `tapeout_on_or_after` maps only to a tapeout lower bound and cannot conflict with new filters. Mixed requests keep schedule results separate from ranked document passages; generated prose does not reinterpret the schedule rows. Legacy GET `/api/schedules` remains a bounded, simple compatibility endpoint; use the new envelope for totals/pagination.

## Natural-language contract

Examples:

- `Show shared 28nm schedules flavor "HPC+ RF"`
- `Next 2 full tapeout dates`
- `Shared schedules next month`
- `Shared trial dates between 2026-11-01 and 2026-11-30`
- `Full final deadlines on or before 2026-11-10`
- `Full estimated ship dates next month`
- `Shared open schedules`
- `List both schedules`

Today/tomorrow, this/next week, this/next month and next N days are supported. Weeks are Monday–Sunday; month windows cover the whole calendar month. Next N days includes today as day one. `after` / `before` are exclusive; `on or after` / `on or before` are inclusive. Calendar boundaries and leap years are tested. Estimated ship dates remain estimates.

Use ISO dates to avoid ambiguous numeric dates. Quote flavors or use the explicit field. Multiple technologies in one question, unknown flavor tokens, negation, unsupported quarter/month-name expressions, conflicting typed/prose filters and ambiguous milestone requests ask for clarification. This is deliberately a bounded grammar, not unrestricted natural-language understanding. A score or second LLM cannot authorize an inferred business filter.

## CLI

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli schedules --tenant local-test --catalog
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli schedules --tenant local-test --question "Next shared 28nm tapeout"
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli schedules --tenant local-test --kind both --date-field final_deadline --date-from 2026-11-01 --date-to 2026-12-31
```

CLI schedules now returns the result envelope, not only a list. The existing interface redesign is deferred; new filter/pagination/catalog capabilities are available through CLI and authenticated API `/docs`. Existing automatic routing can use supported questions; its current frontend still asks for provider consent before a Cohere-mode request, even when the backend ultimately routes locally.

## Repeatable verification

```powershell
# Private real-source audit: no external model calls; isolated test database only.
.\.venv\Scripts\python.exe tools\evaluate_schedule_sources.py --source-dir "D:\Desktop\GSME\faq test files"
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Set `GSME_TEST_DATABASE_URL` to an isolated `gsme_rag_test*` database and `GSME_TEST_QDRANT_URL=http://127.0.0.1:6333` for the full integration suite. The real-source audit derives a test database URL from local configuration if needed; it never publishes the benchmark fixtures into the development workspace. Private report/originals are under ignored `validation/schedule-acceptance/`.

On 10 November 2026, independent CSV/XLSX comparisons matched all **33 Full + 14 Shared records**. **44/44 real-source API checks passed** (22 question/filter cases against CSV and workbook independently). This verifies imported-value/filter agreement, not that the schedules remain current on the GSME website. Integration regressions also cover source replacement, failed updates, tenant isolation, tied nearest dates, SQL-literal injection, catalog scope and pagination invalidation.
