# Local run and Render deployment

## Windows local run

Open PowerShell in the extracted project root containing `pyproject.toml`. A prompt prefix `(.venv)` does not prove that `pip` and `python` use the same environment. If a previous `.venv` has no `Scripts\python.exe`, rename or remove that broken folder while the server is stopped. Then:

```powershell
py -3.13 -m venv .venv
Test-Path .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m gsme_rag.api.run
```

`Test-Path` must return True. If your launcher lacks 3.13, use a supported Python 3.11+ through `py -m venv .venv`. Open `http://127.0.0.1:8000/`. The `/docs` route shows the API schema. Ctrl+C stops the process.

## Render

Render is a cloud web host. It runs the FastAPI process on a server, giving the audience a public `https://SERVICE.onrender.com/` URL. A localhost URL points to the speaker's own machine and cannot be opened from another person's computer. This repository includes:

- `render.yaml`: Python web service, build `pip install .`, start `python -m gsme_rag.api.run`, health `/v1/health`.
- `.python-version`: Python 3.12 to keep the deployment runtime consistent with the tested line.
- `api/run.py`: reads Render's `PORT` and binds on `0.0.0.0`.

Steps: commit the project files (do not include `.venv`, build artifacts or example customer data) to a GitHub branch; on Render, create a Blueprint from the repo and choose that branch. Place `render.yaml` at the repository root, or configure its path if this is a subfolder. If a Blueprint is inconvenient, create a Web Service from the same branch with **Root Directory** set to the project folder, build command `pip install .`, start command `python -m gsme_rag.api.run`, and health check `/v1/health`. After the build succeeds, open its assigned URL and upload a synthetic FAQ. Changes pushed to the selected branch can trigger a redeploy.

The free web service may spin down after 15 minutes without traffic and need a short wake-up on the next request. Open it shortly before the presentation. The demo has no authentication, persistent storage or file confidentiality boundary; use synthetic or approved files on the public URL. Uploads are limited to 20 MiB and are removed from temporary disk after processing. Do not promise persistence or a production service-level guarantee.

Official instructions: https://render.com/docs/deploy-fastapi ; https://render.com/docs/your-first-deploy ; https://render.com/docs/python-version
