# Company chatbot deployment

## Access policy

| Content | Anonymous visitor | Signed-in owner | Other signed-in user |
| --- | --- | --- | --- |
| Explicitly published current FAQ version and original | Read | Read | Read |
| Private upload, filename, extracted text and original | No | Read | No |
| Explicitly published schedule records | Read records only | Read; original if owner | Read records only |
| Operator review, publication and administration | No | No | No |

Public FAQ access is enabled by default, as requested. A database policy must explicitly publish the exact approved current version. Uploads never choose their owner, workspace or public visibility. Missing policies deny access. A replacement version is not automatically public. Operators with database/filesystem access remain trusted; this is not end-to-end encryption.

Authorization filters evidence before vector search/reranking and rechecks source access before returning context and generated answers. Private text must never be sent to the model merely to filter it later. Organization-approved LLM permissions still apply; owner-only app access does not mean local-only model processing.

## Local setup

Keep the real `.env` private. Install `.[storage,test,hosted]`, start PostgreSQL/Qdrant, then run:

```powershell
.\.venv\Scripts\python.exe -m gsme_rag.storage.cli migrate
.\.venv\Scripts\python.exe -m gsme_rag.hosted.app
```

Migrations 006/007 add policies and shared request counters. They do not publish existing documents. Set `GSME_HOSTED_TENANT` to the fixed workspace holding your evidence; the default `company` is separate from `local-test`. There is no browser workspace switch. Configure Cohere keys/content permissions for real retrieval and answer generation. Private uploads cannot work until sign-in is configured.

For a reviewed FAQ, use the local-only operator tool with the exact IDs:

```powershell
.\.venv\Scripts\python.exe tools/publish_hosted_source.py --tenant company --document-id DOCUMENT_UUID --version-id VERSION_UUID --kind faq --confirm-public
```

The tool prompts for the existing local operator token. Inspect the original first: publication exposes both FAQ text and the original file. Use `--kind schedule` to publish typed schedule records without publishing the spreadsheet. This is a local administrative action, not an upload option.

## Google Workspace and HTTPS

Provide a Google web OAuth client ID, allowed Workspace hosted domain and final HTTPS origin. Configure the exact JavaScript origin in Google Cloud; a Gemini API key is unrelated to user sign-in. Set `GSME_GOOGLE_CLIENT_ID`, `GSME_GOOGLE_WORKSPACE_DOMAINS` and `GSME_HOSTED_ORIGIN` accordingly. Identity uses verified Google subject IDs, audience/issuer checks, verified email and Workspace domain checks, not browser-supplied email addresses. Tokens stay in browser memory; logout clears private UI state and late responses are discarded.

The server binds to loopback port 8020. Put a TLS reverse proxy in front of it, restrict `GSME_TRUSTED_PROXY_IPS` to that proxy, and block direct access to PostgreSQL, Qdrant and operator port 8010. Never expose the parsing demo or operator console as the company product. Existing Dockerfile/render.yaml still deploy only the parsing demo; they are not a hosted-chat production deployment.

## Limits and operating boundaries

Defaults: 6 chats/user/minute, 8 chats/company/minute, 50 chats/company/day, 120 incoming requests/IP/minute, two concurrent chat operations and four upload tasks per process. Chat limits use shared PostgreSQL counters; concurrency limits are process-local. Configure budgets for actual traffic before public launch. These are abuse/cost guardrails, not a DDoS defense or a precise provider billing ceiling. Reverse-proxy rate limits and monitoring are still required.

Uploads are limited to 20 MiB. Clean evidence is automatically accepted; uncertain formulas, table/OCR/chart claims remain held. Upload/index work runs in bounded in-process background tasks, with persisted ingestion state and an owner resume action. It is not an external durable worker or automatic restart-recovery service. Large documents may need resume after an index batch limit. The API supports owner version replacement; the simple upload UI currently creates a new logical document. Chat questions are independent, not conversational memory. Structured schedule results have API pagination; the UI asks users to refine broad results.

## Release gates

Synthetic API/browser checks do not establish real Workspace SSO, hosted TLS operation, penetration resistance or company load capacity. Before external launch: configure and test real sign-in/logout, publish only approved originals, verify proxy isolation/backups/restore, choose a durable worker if required, test expected load, and complete the held-out answer-quality review. No existing source has been made public by this increment.

## Render or Vercel

For this implementation, prefer **Render for the FastAPI backend and its built-in chatbot assets**. This keeps source downloads, Google sign-in and API calls on one origin. Render documents a normal [FastAPI web service](https://render.com/docs/deploy-fastapi). This is an architectural recommendation, not a deployment or a guarantee that a free service is sufficient.

Provision PostgreSQL, private Qdrant access, secrets and durable original/normalized storage separately. Render's default filesystem is ephemeral; its [persistent disks](https://render.com/docs/disks) require a paid service and are accessible to only one instance. A disk-backed first deployment therefore does not provide horizontal scaling. A separate ingestion worker cannot simply mount the web service's disk; shared private object storage and a durable worker design are needed for that architecture. Do not set GSME_DATA_DIR to an ephemeral directory and expect private originals to survive redeploys.

The current Ollama fallback targets loopback on the machine running the backend. A Render service cannot use the developer laptop's Ollama simply because it works locally. Cloud fallback needs a deliberately provisioned local model service and suitable resources; it is not included in the existing parsing-demo deployment. Until provisioned, a Command outage may return provider-unavailable. It must never fabricate a response or reinterpret missing source evidence as a provider outage.

The current loopback development launcher is not the cloud start command. A configured web service can launch the application factory using:

```sh
uvicorn gsme_rag.hosted.app:create_hosted_app --factory --host 0.0.0.0 --port "$PORT"
```

Bind public HTTP only inside the managed web service, configure the actual HTTPS origin, and verify the platform proxy/trusted-header settings. Do not run the operator console alongside it or reuse the existing parsing-demo Blueprint unchanged. Test mounted storage, backup restoration and Google origin configuration before exposing real uploads. Google sign-in remains disabled until a web client ID and Workspace domain exist.

Vercel supports [Python/FastAPI functions](https://vercel.com/docs/functions/runtimes/python), but the current persistent-file and in-process ingestion design is not a drop-in serverless deployment. Its documented [4.5 MB function payload limit](https://vercel.com/docs/functions/limitations#request-body-size) is also below this application's 20 MiB upload limit. A Vercel frontend is an option later, using a carefully configured same-origin proxy to a durable backend; the current API intentionally does not enable open cross-origin access. No Vercel or Render service has been created by this increment.
