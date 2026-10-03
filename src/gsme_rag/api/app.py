from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from gsme_rag.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="GSME RAG Pipeline Demo",
        version="2.0.0",
        description=(
            "Stage 1 normalizes DOCX, born-digital PDF, or CSV input to Markdown. "
            "Stage 2 creates layout-aware chunks from that Markdown."
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
  <title>GSME RAG Pipeline Demo</title>
  <style>
    body { max-width: 980px; margin: 40px auto; padding: 0 24px; font-family: Arial, sans-serif; color: #111; background: #fff; }
    h1, h2 { color: #111; }
    section { margin: 28px 0; padding-top: 18px; border-top: 1px solid #bbb; }
    label { display: block; margin: 12px 0 6px; font-weight: 700; }
    input, button, textarea { font: inherit; }
    input[type=file], input[type=number] { padding: 8px; border: 1px solid #777; }
    button { margin-top: 12px; padding: 10px 14px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
    button:disabled { background: #888; border-color: #888; cursor: wait; }
    textarea { width: 100%; min-height: 260px; box-sizing: border-box; padding: 12px; border: 1px solid #777; }
    pre { padding: 14px; border: 1px solid #777; background: #f7f7f7; white-space: pre-wrap; overflow: auto; }
    .note { color: #444; }
  </style>
</head>
<body>
  <h1>GSME RAG Pipeline Demo</h1>
  <p class="note">The two stages are independent. Parsing creates Markdown. Chunking receives only Markdown.</p>

  <section>
    <h2>1. Parse file to Markdown</h2>
    <label for="file">DOCX, digital PDF, or CSV</label>
    <input id="file" type="file" accept=".docx,.pdf,.csv">
    <br>
    <button id="parse">Parse to Markdown</button>
    <pre id="parse-meta">Parser result details will appear here.</pre>
    <label for="markdown">Normalized Markdown</label>
    <textarea id="markdown" placeholder="Parsed Markdown will appear here and can be edited before chunking."></textarea>
  </section>

  <section>
    <h2>2. Chunk normalized Markdown</h2>
    <label for="max-chars">Maximum characters per chunk</label>
    <input id="max-chars" type="number" min="200" max="20000" value="1800">
    <br>
    <button id="chunk" disabled>Chunk Markdown</button>
    <pre id="chunk-output">Chunk records will appear here.</pre>
  </section>

  <p><a href="/docs">Open interactive API documentation</a></p>

  <script>
    const fileInput = document.getElementById('file');
    const parseButton = document.getElementById('parse');
    const parseMeta = document.getElementById('parse-meta');
    const markdown = document.getElementById('markdown');
    const chunkButton = document.getElementById('chunk');
    const chunkOutput = document.getElementById('chunk-output');
    let sourceFile = 'normalized.md';

    parseButton.addEventListener('click', async () => {
      const file = fileInput.files[0];
      if (!file) {
        parseMeta.textContent = 'Select a file first.';
        return;
      }
      parseButton.disabled = true;
      chunkButton.disabled = true;
      parseMeta.textContent = 'Parsing file to Markdown...';
      const data = new FormData();
      data.append('file', file);
      try {
        const response = await fetch('/v1/parse', { method: 'POST', body: data });
        const result = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(result));
        sourceFile = result.filename;
        markdown.value = result.markdown;
        const details = { ...result };
        delete details.markdown;
        parseMeta.textContent = JSON.stringify(details, null, 2);
        chunkButton.disabled = !result.markdown.trim();
      } catch (error) {
        parseMeta.textContent = String(error);
      } finally {
        parseButton.disabled = false;
      }
    });

    chunkButton.addEventListener('click', async () => {
      chunkButton.disabled = true;
      chunkOutput.textContent = 'Chunking Markdown...';
      try {
        const response = await fetch('/v1/chunk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            markdown: markdown.value,
            source_file: sourceFile,
            max_chars: Number(document.getElementById('max-chars').value)
          })
        });
        const result = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(result));
        chunkOutput.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        chunkOutput.textContent = String(error);
      } finally {
        chunkButton.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


app = create_app()
