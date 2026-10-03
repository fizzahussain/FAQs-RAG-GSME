from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from gsme_parser.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="GSME Parsing Demo",
        version="1.0.0",
        description=(
            "Upload an FAQ DOCX, born-digital FAQ PDF, or schedule CSV and "
            "preview the normalized parser output. No database or search layer is used."
        ),
    )
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def demo_page() -> str:
        return DEMO_PAGE

    return app


DEMO_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GSME Parsing Demo</title>
  <style>
    :root { color-scheme: light; font-family: Inter, system-ui, sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #172033; }
    main { max-width: 960px; margin: 48px auto; padding: 0 24px; }
    .card { background: white; border: 1px solid #dfe5ef; border-radius: 16px; padding: 24px; box-shadow: 0 12px 32px #24324a12; }
    h1 { margin-top: 0; font-size: 2rem; }
    p { line-height: 1.55; color: #4b5870; }
    form { display: grid; gap: 16px; margin-top: 24px; }
    label { display: grid; gap: 7px; font-weight: 650; }
    input, select, button { font: inherit; }
    input, select { padding: 10px 12px; border: 1px solid #cbd4e2; border-radius: 9px; }
    button { padding: 12px 16px; border: 0; border-radius: 9px; background: #3157d5; color: white; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: wait; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .scope { margin: 20px 0 0; padding: 12px 14px; background: #eef3ff; border-radius: 10px; color: #29406f; }
    pre { margin-top: 24px; padding: 18px; min-height: 160px; overflow: auto; border-radius: 12px; background: #111827; color: #d8e4ff; white-space: pre-wrap; }
    .links { margin-top: 14px; font-size: .95rem; }
    @media (max-width: 680px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <section class="card">
      <h1>GSME Parsing Demo</h1>
      <p>Upload one DOCX, born-digital PDF, or schedule CSV. The API selects the appropriate parsing branch and returns normalized output. Nothing is stored.</p>
      <div class="scope">Hybrid here means multiple parsing strategies selected by document structure—not hybrid search.</div>
      <form id="parser-form">
        <label>File
          <input id="file" name="file" type="file" accept=".docx,.pdf,.csv" required>
        </label>
        <div class="grid">
          <label>FAQ strategy
            <select id="strategy" name="strategy">
              <option value="auto">Auto-detect (recommended)</option>
              <option value="qa">Question–answer</option>
              <option value="table">Table</option>
            </select>
          </label>
          <label>CSV schedule type
            <select id="schedule_type" name="schedule_type">
              <option value="">Infer from filename</option>
              <option value="shared">Shared block</option>
              <option value="full">Full block</option>
            </select>
          </label>
        </div>
        <button id="submit" type="submit">Parse and preview</button>
      </form>
      <div class="links"><a href="/docs">Open interactive API documentation</a></div>
      <pre id="output">Parser output will appear here.</pre>
    </section>
  </main>
  <script>
    const form = document.getElementById('parser-form');
    const output = document.getElementById('output');
    const button = document.getElementById('submit');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      button.disabled = true;
      output.textContent = 'Parsing…';
      const data = new FormData();
      data.append('file', document.getElementById('file').files[0]);
      data.append('strategy', document.getElementById('strategy').value);
      const scheduleType = document.getElementById('schedule_type').value;
      if (scheduleType) data.append('schedule_type', scheduleType);
      try {
        const response = await fetch('/v1/parse', { method: 'POST', body: data });
        const result = await response.json();
        output.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        output.textContent = JSON.stringify({ error: String(error) }, null, 2);
      } finally {
        button.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


app = create_app()
