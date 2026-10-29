# Embedding and storage handoff 0.15.3

This release continues 0.15.2 on the existing `parsing-and-chunking-revised` checkout. The supplied 0.15.2 ZIP matched the checkout byte-for-byte. PostgreSQL migrations 001/002 were already applied; **no schema changed and no new migration is required**. Parsing, chunking, Full/Shared schedule typing and the public FastAPI demo were not altered.

## Confirmed changes

| Finding | Change | Check |
| --- | --- | --- |
| An unscoped Cohere worker could process all approved tenants once a key was enabled. | External indexing now requires explicit tenant and version IDs; `worker-once` can target a parse job ID. | Live PostgreSQL/Qdrant integration and CLI scope tests. |
| Qdrant repeated source filename, category, page, FAQ and chunk metadata already authoritative in PostgreSQL. | Payload now keeps tenant ID, version ID, model version, text SHA-256 and text for optional lexical fusion. | Live point-payload assertion; retrieval citations still come from PostgreSQL. |
| Cohere API failures could expose vendor exception details in a local traceback. | Safe operation/status guidance masks source text and credentials for token-count and embedding calls. | Mock SDK request-shape and error-sanitization test. |
| A general test suite could pass while pending/rejected evidence was never tested against a real job claim. | Added a live test that neither status reaches the embedding provider. | Real isolated PostgreSQL/Qdrant test database. |

Official [Embed 5 guidance](https://docs.cohere.com/docs/cohere-embed) confirms Pro/Fast shared space and 1024 dimensions. The [v2 Embed API](https://docs.cohere.com/reference/embed) and installed Cohere SDK 5.21.1 confirm request shape, 96-text maximum and `truncate="NONE"`. The [tokenization endpoint](https://docs.cohere.com/reference/tokenize) was exercised with the Pro model on one source-approved FAQ chunk. No semantic-quality score is claimed from that smoke test.

The key and permission flag were added locally. A first attempt stopped before any Cohere request because PostgreSQL authentication failed. After the database URL was restored, an ordinary sandboxed Cohere call failed at the network connection stage; an approved outbound connection succeeded. The same single reviewed job was retried with its original collection and completed. These environmental failures did not cause a pending chunk to be sent or a point to be published.

Verification before the credential change: **144 tests, OK, zero skips** with isolated PostgreSQL/Qdrant URLs, plus 18 focused storage tests after the worker-scope change. Final full run after the provider-error change and restored database connection: **145 tests, OK, zero skips**.

### Authorized real-source result

`FAQs QA Format.docx` produced 31 chunks using `docx_structure_to_markdown`. FAQ question 05 and its answer were compared with the original DOCX paragraphs; one exact chunk was approved in the isolated `gsme_rag_test_codex_20261027` database, while the other 30 remained pending. The approved 133-character chunk passed Cohere Pro token counting, Pro `search_document` embedding at 1024 dimensions, and one Qdrant upsert. Cohere Fast `search_query` returned one matching PostgreSQL-validated hit with source file, chunk ID and version ID. A different tenant got zero hits. After a recorded rejection, the same search returned zero hits **before** cleanup; reconciliation removed one stale point and Qdrant retrieval confirmed it was gone. The source document itself is not in the ZIP. This is a one-question plumbing smoke, not a measured retrieval-quality baseline.

## Authorized real-source smoke test (already migrated database)

Use a small FAQ whose exact answer you can verify against the original. The example below uses `D:\Desktop\GSME\faq test files\FAQs QA Format.docx`, which was visible locally. Do not auto-approve all its chunks. Keep your existing private `.env` and `data/`; install this release with `-e ".[storage,test]"`. Put your key only in `.env`, and set `GSME_EXTERNAL_CONTENT_APPROVED=yes` there after confirming this source may be sent. Do not put either value in PowerShell history, code or the ZIP.

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli submit 'D:\Desktop\GSME\faq test files\FAQs QA Format.docx' --tenant cohere-smoke
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli worker-once --ingestion-only --job-id ACTUAL_JOB_UUID
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli inspect ACTUAL_VERSION_UUID --tenant cohere-smoke
```

Compare one pending chunk with the original, including its question, answer and any warnings. Configure `GSME_AUTHORIZED_REVIEWERS` and `GSME_REVIEW_AUTH_TOKEN` locally; the review command prompts for the token. Then:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli review --tenant cohere-smoke --version-id ACTUAL_VERSION_UUID --chunk-id ACTUAL_CHUNK_ID --decision approved --reviewer YOUR_AUTHORIZED_NAME --rationale 'Compared with the original FAQ' --source-verified
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli publish --tenant cohere-smoke --document-id ACTUAL_DOCUMENT_UUID --version-id ACTUAL_VERSION_UUID --provider cohere
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli worker-once --provider cohere --index-only --tenant cohere-smoke --version-id ACTUAL_VERSION_UUID
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli search 'A question answered by the approved FAQ chunk' --tenant cohere-smoke --provider cohere
```

Expected: submit says `queued`; ingestion says `review_needed`; inspect shows the source chunk and pending status; review records one source-verified approval; publish reports at least one candidate; worker reports `indexed >= 1` and `failed = 0`; search returns the approved text, source file, document/version/chunk IDs and page if available. If worker reports `index token_budget failed`, inspect the local job error; do not turn off truncation or approve unrelated content. If the Cohere token-count endpoint rejects Embed 5, the safe status label identifies that stage and indexing stops before a document embedding call. A query embedding is sent only by the explicit search command.

After a live test, reject that same chunk and run the identical search again: it must return no hit for the revoked chunk even before Qdrant reconciliation. Run `reconcile --provider cohere` to remove the stale derived point. A replacement version similarly excludes old hits immediately when published. Use the exact source-reviewed cases format in `evaluation/gsme_reviewed_template.json` for Recall@5/10, wrong-version/revoked leakage, query latency, embedding-call and failure reporting. Compare dense and optional hybrid on the **same** reviewed questions; an empty template cannot prove quality.

The one-chunk Cohere document/query stop condition passed. A representative source-reviewed query set, dense-versus-hybrid comparison, large-document performance and production operations remain unverified. The local administrator CLI, .env credentials and filesystem adapter are not a production authentication, backup or retention design.
