# Storage audit and live verification 0.15.2

Audited on Windows, branch `parsing-and-chunking-revised`, starting Git commit `dfaf0db`. The supplied 0.15.1 ZIP and checkout differed only in migration 002; the checkout already had `a.attname::text`, and the applied migration was left unchanged. No forward migration is needed for this release.

| Severity | Finding | Fix | Verification |
| --- | --- | --- | --- |
| High, reproduced | Psycopg 3.3.6 has `Cursor.executemany()`, not `Connection.executemany()`; both chunk and schedule ingestion failed. | Use `db.cursor().executemany()` in both paths. | Fresh isolated migrations 001/002; live FAQ and Full/Shared schedule tests pass. |
| Medium, reproduced | Failed ingestion stored only a generic error label. | Record safe operation stage and exception class, without exception values, source text or secrets. | Live invalid-DOCX and forced schedule-write failure tests. |
| Medium, observed | Stored chunks omitted page warning codes and source anchors even though raw warnings were in the manifest. | Attach exact page-located warning codes and stable unit/chunk anchors; expose them, backend and original/manifest paths in local inspection. | Focused attribution test; live ingestion tests. |
| Medium, tested risk | A write error during schedule publication or a stale worker lease could threaten current publication. | Existing transactional publication and attempt guards were retained; regression tests now exercise both failure paths on PostgreSQL. | Live rollback and expired-attempt tests pass. |
| Low, prior coverage gap | The standalone fixture stopped before revocation. | Verify rejected evidence is not returned and reconcile its derived Qdrant point. | Live fixture: one synthetic hit before revocation, zero after, one stale point removed. |

Baseline: `136` tests, `OK (skipped=3)` with test service URLs unset. All three skips were `tests.test_storage_integration` and were **not** counted as integration passes. The isolated PostgreSQL 17.6 and local Qdrant run then passed the live suite and fixture. Final full run with real test URLs: **140 tests, OK, zero skips**. Synthetic hash vectors are deterministic plumbing checks, not a semantic-quality score.

## Already migrated Windows database

Replace the project source files with this release; preserve your private `.env` and `data/`. Do **not** replace database volumes or rerun an edited 001/002 migration. After installation, the following is safe against an already migrated database (the migration command should report no newly applied files):

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[storage,test]"
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe tools\demo_storage_fixture.py
```

For the **specific failed ingestion job** from the old `executemany` error, use its existing job ID and tenant, then run:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli retry YOUR_JOB_ID --tenant YOUR_TENANT
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli worker-once --ingestion-only
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli status YOUR_JOB_ID --tenant YOUR_TENANT
```

The retry is unnecessary for a new synthetic fixture, which creates its own test document. Keep the fake provider for that fixture. Do not enable Cohere or submit the real manual for embedding until a reviewer has source-verified the exact chunks and the organization permits external document calls.

## Remaining limits

The Cohere SDK call shape was checked against installed SDK 5.21.1 and current official Embed 5 documentation: Pro and Fast share an embedding space and support the configured dimensions and input types. Its `tokenize` call for the Embed 5 model and actual API response remain unverified without an authorized key; it fails closed if unavailable. No real GSME document was embedded. There is no labeled, source-reviewed GSME query set, so Recall@5, MRR, wrong-page rate, dense-versus-hybrid quality, and production latency are not established. Local CLI authentication and file storage are development adapters, not a production identity, backup, or retention solution. See [storage design and commands](STORAGE_INCREMENT.md).
