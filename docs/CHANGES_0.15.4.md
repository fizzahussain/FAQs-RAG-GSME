# 0.15.4 — Local evidence console

The operator can now run a separate localhost-only browser app for the reviewed storage pipeline. The existing public FastAPI demo remains parse/chunk only. No schema migration is required beyond 001/002.

## What is implemented

- Upload a supported document to private durable storage, process its ingestion job, list versions, and inspect its original, normalized Markdown, manifest warnings, page-located chunks, and review decisions.
- Record one source-verified review decision at a time. Approval requires a reviewer from `GSME_AUTHORIZED_REVIEWERS`, a local token, a source-verification checkbox, and a rationale. Existing database checks still reject generic visual placeholders.
- Publish a version with at least one approved chunk, index a bounded batch into the fake or Cohere collection, inspect index-job state, preview an approved current vector, search with source citations, and reconcile revoked/superseded points.
- Operator token authentication, origin checks, loopback client check and loopback bind. The token is supplied in a request header; it is not written to the page or persisted in browser storage.

## Verification and boundaries

Focused tests cover missing/weak tokens, cross-origin and remote access, unlisted reviewer denial, explicit external-index consent, and public route separation. A real PostgreSQL/Qdrant test covers upload → pending → source review → publish → fake indexing → vector preview → search → revocation → deletion. It never calls Cohere. Once Docker Desktop started on 2026-10-29, the isolated console integration test passed and the **full suite ran 148 tests, OK, zero skips** against local PostgreSQL and Qdrant. The earlier 0.15.3 live Cohere one-chunk smoke remains the provider verification; this release does not claim a new live Cohere run or corpus retrieval-quality score.

The UI is not a production multiuser security boundary, does not generate answers, and does not auto-verify OCR, formulas, tables or charts. Embedding previews show coordinates and metadata, not semantic meaning. The current retrieval stage returns evidence with citations; answer generation, a reviewed GSME question set, quality benchmarking, production identity, backups, and load testing are separate next steps.
