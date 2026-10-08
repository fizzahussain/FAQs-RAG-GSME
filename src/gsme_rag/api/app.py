from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from gsme_rag.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="GSME RAG Pipeline Demo",
        version="0.7.0",
        description=(
            "Stage 1 normalizes DOCX, born-digital PDF, or CSV input to Markdown. "
            "Stage 2 compares four chunking strategies on that Markdown. "
            "Schedule CSVs also have a typed record preview."
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
    input, button, textarea, select { font: inherit; }
    input[type=file], input[type=number], input[type=text], select { padding: 8px; border: 1px solid #777; }
    button { margin-top: 12px; padding: 10px 14px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
    button:disabled { background: #888; border-color: #888; cursor: wait; }
    textarea { width: 100%; min-height: 260px; box-sizing: border-box; padding: 12px; border: 1px solid #777; }
    pre { padding: 14px; border: 1px solid #777; background: #f7f7f7; white-space: pre-wrap; overflow: auto; }
    .note { color: #444; }
  </style>
</head>
<body>
  <h1>GSME RAG Pipeline Demo</h1>
  <p class="note">Parsing creates Markdown. Chunking receives only Markdown. Schedule CSVs can also be inspected as typed records.</p>

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
    <label for="strategy">Chunking strategy</label>
    <select id="strategy">
      <option value="layout_aware">Layout aware: FAQ pairs, table rows, sections</option>
      <option value="fixed_size">Size based: comparison baseline</option>
      <option value="hybrid">Hybrid: layout aware with overlap for long prose</option>
      <option value="paragraph_aware">Paragraph aware: preserve narrative paragraphs</option>
    </select>
    <label for="max-chars">Maximum characters per chunk</label>
    <input id="max-chars" type="number" min="200" max="20000" value="1800">
    <label for="overlap-chars">Long prose overlap (hybrid only)</label>
    <input id="overlap-chars" type="number" min="0" value="120">
    <br>
    <button id="chunk" disabled>Chunk Markdown</button>
    <pre id="chunk-output">Chunk records will appear here.</pre>
  </section>

  <section>
    <h2>3. Compare all strategies</h2>
    <p class="note">Optional evidence check: paste a short excerpt exactly as it appears in the Markdown. Keyword ranking is only a retrieval proxy.</p>
    <label for="question">Question</label>
    <input id="question" type="text" placeholder="Why was the review step added?" size="55">
    <label for="evidence">Expected evidence excerpt</label>
    <input id="evidence" type="text" placeholder="This step reduced the chance..." size="70">
    <br>
    <button id="compare">Compare four strategies</button>
    <pre id="compare-output">Comparison report will appear here.</pre>
  </section>

  <section>
    <h2>4. Preview structured schedule rows</h2>
    <p class="note">Select a schedule CSV above. This reads typed rows directly from the CSV and filters them in memory; it does not use the Markdown or a database.</p>
    <label for="technology">Technology (nm), optional</label>
    <input id="technology" type="number" min="0" placeholder="28">
    <label for="flavor">Flavor, optional exact match</label>
    <input id="flavor" type="text" placeholder="HPC+ RF">
    <label for="after-date">Tapeout on or after, optional (YYYY-MM-DD)</label>
    <input id="after-date" type="text" placeholder="2026-10-01">
    <br>
    <button id="schedule">Preview schedule records</button>
    <pre id="schedule-output">Typed schedule records will appear here.</pre>
  </section>

  <p><a href="/docs">Open interactive API documentation</a></p>

  <script>
    const fileInput = document.getElementById('file');
    const parseButton = document.getElementById('parse');
    const parseMeta = document.getElementById('parse-meta');
    const markdown = document.getElementById('markdown');
    const chunkButton = document.getElementById('chunk');
    const chunkOutput = document.getElementById('chunk-output');
    const compareButton = document.getElementById('compare');
    const compareOutput = document.getElementById('compare-output');
    const scheduleButton = document.getElementById('schedule');
    const scheduleOutput = document.getElementById('schedule-output');
    let sourceFile = 'normalized.md';

    markdown.addEventListener('input', () => {
      chunkButton.disabled = !markdown.value.trim();
    });

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
            max_chars: Number(document.getElementById('max-chars').value),
            strategy: document.getElementById('strategy').value,
            overlap_chars: Number(document.getElementById('overlap-chars').value)
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

    compareButton.addEventListener('click', async () => {
      if (!markdown.value.trim()) {
        compareOutput.textContent = 'Parse a file or paste Markdown first.';
        return;
      }
      const question = document.getElementById('question').value.trim();
      const evidence = document.getElementById('evidence').value.trim();
      if (Boolean(question) !== Boolean(evidence)) {
        compareOutput.textContent = 'Enter both a question and an evidence excerpt, or leave both blank.';
        return;
      }
      compareButton.disabled = true;
      compareOutput.textContent = 'Comparing...';
      try {
        const response = await fetch('/v1/compare', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            markdown: markdown.value,
            source_file: sourceFile,
            max_chars: Number(document.getElementById('max-chars').value),
            overlap_chars: Number(document.getElementById('overlap-chars').value),
            cases: question ? [{ question, evidence }] : []
          })
        });
        const result = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(result));
        compareOutput.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        compareOutput.textContent = String(error);
      } finally {
        compareButton.disabled = false;
      }
    });

    scheduleButton.addEventListener('click', async () => {
      const file = fileInput.files[0];
      if (!file || !file.name.toLowerCase().endsWith('.csv')) {
        scheduleOutput.textContent = 'Select a schedule CSV in section 1 first.';
        return;
      }
      const params = new URLSearchParams();
      const technology = document.getElementById('technology').value.trim();
      const flavor = document.getElementById('flavor').value.trim();
      const afterDate = document.getElementById('after-date').value.trim();
      if (technology) params.set('technology_nm', technology);
      if (flavor) params.set('flavor', flavor);
      if (afterDate) params.set('tapeout_on_or_after', afterDate);
      const data = new FormData();
      data.append('file', file);
      scheduleButton.disabled = true;
      scheduleOutput.textContent = 'Reading structured records...';
      try {
        const response = await fetch('/v1/schedules/preview?' + params.toString(), {
          method: 'POST', body: data
        });
        const result = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(result));
        scheduleOutput.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        scheduleOutput.textContent = String(error);
      } finally {
        scheduleButton.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


app = create_app()
