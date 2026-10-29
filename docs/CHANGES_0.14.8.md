# Storage preparation release 0.14.8

This is the first bounded Stage C increment. `migrations/001_evidence_index.sql` defines authoritative document, version, chunk, review, and index-job records. `compose.local.yaml` exposes local PostgreSQL and Qdrant on loopback only; `.env.example` shows the required configuration. `.env` is ignored. The schema has not yet been applied to a live database in this release.

`prepare_candidate()` requires a source-verified approval even for ordinary prose, a source SHA-256, tenant identity, model version, and a caller-supplied tokenizer. It rejects over-budget text rather than truncating it. The deterministic point ID is scoped to tenant, source, chunk, and model version. This function only prepares a candidate: it does not embed, upsert, or alter review state. The SQL view `approved_current_evidence` is a relational safety boundary design, not a working search endpoint.

The parser and chunker algorithms did not change, so their provenance versions remain 0.14.7. The package/API version is 0.14.8.

## Local setup check (PowerShell)

1. Copy `.env.example` to `.env` and replace the example password in both values. Do not commit `.env`.
2. Run `docker compose -f compose.local.yaml config --quiet` to validate configuration, then `docker compose -f compose.local.yaml up -d` to start the services if Docker Desktop is available.
3. Apply `migrations/001_evidence_index.sql` with a PostgreSQL client against the local database. This is schema preparation only; it does not import documents or create vectors.
4. Run `./.venv/Scripts/python.exe -m unittest discover -s tests -q` from the repository root.

The Compose syntax check passed locally. Docker printed a warning that `C:\Users\HP\.docker\config.json` could not be read; the services were not started and the SQL migration was not applied, so database behavior is not integration-tested.

## Not yet delivered

No local embedding model or labeled GSME query set has been selected; no source-verified prose subset has been approved. There is no PostgreSQL adapter, durable worker/outbox execution, Qdrant upsert/retry/delete reconciliation, permission-filtered search, lexical/hybrid index, or evaluation. Consequently no manual content has been indexed. Do not use the schema/view alone to claim cross-store revocation safety: searches must re-check live PostgreSQL approval, tenant, and version state before returning any Qdrant hit. Pin and test the Qdrant image version before any shared or production deployment.
