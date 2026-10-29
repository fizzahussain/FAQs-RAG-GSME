# Code audit and readability pass 0.15.1

This pass starts from the 0.15.0 embeddings and storage project. It does not change the parser and chunker algorithms or relax review approval. Comments are short and placed at decisions that are easy to miss in a demo. They avoid narrating ordinary Python syntax.

## Changes

- An unchanged submitted version is detected by streamed checksum before another large local file copy. The document lock checks again before inserting, so concurrent submissions remain idempotent. Failed or duplicate copies are removed even if a database transaction fails.
- Schedule and evidence inserts use PostgreSQL `executemany` with iterators rather than one round trip per row or another large list. Their typed fields and physical row numbers are unchanged.
- Schedule queries add only the filters provided. This makes the exact and date indexes more useful than nullable `OR` clauses. Tenant and current version are still mandatory filters, and values remain SQL parameters.
- Qdrant search returns IDs and scores. PostgreSQL supplies approved text and citations after checking tenant, version, review and model. This avoids moving overfetched text twice.
- A worker reuses a validated Qdrant collection configuration within its process. Collection checks still happen on the first batch.
- Long OCR and embedding work renews its job lease. Claim attempts guard completion and failure updates, so an old worker cannot publish after another worker reclaims the job. Search still checks current approval in PostgreSQL if an interrupted Qdrant upsert leaves a point behind.
- Unused repository methods, imports and redundant model version attributes were removed. Existing public parsing routes, OCR decisions, review markers, Full and Shared classification, and the Render demo remain.
- Comments now explain version identity, physical CSV lines, FAQ routing, overlap, batch retries, review locks and evidence filtering in the source itself.

## Checks and remaining limits

The final local suite ran 136 tests with six skips. Three skips need the exact original manual and three need real PostgreSQL and Qdrant services. Ruff found no unused imports in `src`. SQL migration syntax was checked with a PostgreSQL parser. These checks do not substitute for a local database migration and service integration run. The Cohere tokenizer and Embed 5 calls still need an authorized API key and a permitted source-content test. Large document memory use and target-corpus retrieval quality remain unmeasured.

## Next test on the office machine

Start Compose, apply migrations, run `tests.test_storage_integration`, then run `tools/demo_storage_fixture.py`. Keep real-source review and external embedding disabled until the source comparison and content permission are in place. See `docs/STORAGE_INCREMENT.md` for exact PowerShell commands.
