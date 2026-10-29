"use strict";
const el = id => document.getElementById(id);
const value = id => el(id).value.trim();
let offset = 0, nextOffset = null, inspection = null, dimension = 1024;

// The token stays in this page's input; it is never saved to browser storage.
async function api(path, options = {}) {
  const headers = {"X-GSME-Token": value("token")};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {...options, headers});
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail; } catch (_) { /* HTTP text is enough. */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response;
}
async function json(path, body) {
  return (await api(path, body ? {method: "POST", body: JSON.stringify(body)} : {})).json();
}
function show(id, data) { el(id).textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2); }
function identifiers() { return {tenant: value("tenant"), version_id: value("version"), document_id: value("document"), provider: value("provider")}; }
function versionPath(action) { return action + "/" + encodeURIComponent(value("version")) + "?tenant=" + encodeURIComponent(value("tenant")); }
async function run(action) {
  el("message").className = "";
  el("message").textContent = "Working...";
  const buttons = Array.from(document.querySelectorAll("button"));
  const disabled = buttons.map(button => button.disabled);
  buttons.forEach(button => button.disabled = true);
  try { await action(); el("message").textContent = "Done. Review the result below."; }
  catch (error) { el("message").textContent = "Error: " + error.message; el("message").className = "error"; }
  finally {
    buttons.forEach((button, index) => { if (button.isConnected) button.disabled = disabled[index]; });
    updateControls();
  }
}
function selectedModel() { return (value("provider") === "fake" ? "test-hash-v1-" : "cohere-embed-v5-pro-fast-") + dimension; }
function updateControls() {
  el("previous").disabled = offset === 0;
  el("next").disabled = nextOffset === null;
  document.querySelectorAll("button[data-vector]").forEach(button => {
    const chunk = inspection?.chunks.find(item => item.source_chunk_id === button.dataset.vector);
    button.disabled = !(inspection?.version.is_current && chunk?.evidence_status === "approved" &&
      chunk.index_jobs.some(job => job.model_version === selectedModel() && job.state === "indexed"));
    button.title = button.disabled ? "Approve, publish and index this chunk with the selected provider first" : "Read the stored vector";
  });
}
async function loadVersions() {
  const versions = await json("versions?tenant=" + encodeURIComponent(value("tenant")));
  const picker = el("versionPicker"); picker.replaceChildren();
  const initial = document.createElement("option"); initial.value = ""; initial.textContent = "Choose a version"; picker.append(initial);
  for (const item of versions) {
    const option = document.createElement("option"); option.value = item.id; option.dataset.document = item.document_id;
    option.dataset.job = item.job_id || "";
    option.textContent = `${item.source_file} | ${item.state} | ${item.approved_chunks}/${item.chunks} approved | ${item.id.slice(0,8)}`;
    picker.append(option);
  }
  picker.value = value("version");
}
async function loadInspection() {
  inspection = await json(versionPath("inspect") + "&limit=50&offset=" + offset);
  nextOffset = inspection.next_offset;
  el("document").value = inspection.version.document_id;
  show("versionInfo", {source: inspection.version.source_file, state: inspection.version.state,
    current: inspection.version.is_current, total_chunks: inspection.total_chunks,
    source_sha256: inspection.version.source_sha256, parser_version: inspection.version.parser_version,
    index_jobs: inspection.index_jobs});
  el("pageInfo").textContent = inspection.total_chunks ? `${offset + 1}–${offset + inspection.chunks.length} of ${inspection.total_chunks}` :
    inspection.version.document_class === "schedule" ? "Schedule rows have their own exact search." : "No parsed chunks yet; run or check the ingestion job.";
  const root = el("chunks"); root.replaceChildren();
  for (const chunk of inspection.chunks) {
    const card = document.createElement("div"); card.className = "chunk";
    const title = document.createElement("div"); title.className = "state";
    title.textContent = `Chunk ${chunk.chunk_index ?? "legacy order unknown"} | page ${chunk.source_page_start ?? "n/a"} | ${chunk.evidence_status} | source review: ${chunk.review_required ? "required" : "standard"}`;
    const context = document.createElement("p"); context.className = "muted";
    context.textContent = (chunk.heading_path || []).join(" → ") + ` | ${chunk.chunk_type} | ${chunk.source_chunk_id}`;
    const text = document.createElement("pre"); text.textContent = chunk.evidence_text;
    const note = document.createElement("textarea"); note.rows = 2; note.placeholder = "Review note: e.g. Compared page 3, question 05; question and answer match.";
    const checkedLabel = document.createElement("label"); const checked = document.createElement("input"); checked.type = "checkbox";
    checkedLabel.append(checked, document.createTextNode(" I checked this chunk against the original source"));
    const approve = document.createElement("button"); approve.textContent = "Approve chunk";
    const reject = document.createElement("button"); reject.textContent = "Reject chunk";
    approve.disabled = reject.disabled = chunk.evidence_status === "excluded";
    async function review(decision) {
      if (!note.value.trim()) throw new Error("Add a review note describing what you checked.");
      if (decision === "approved" && !checked.checked) throw new Error("Compare this chunk with its source and tick its checkbox first.");
      await json("review", {tenant: value("tenant"), version_id: value("version"), chunk_id: chunk.source_chunk_id,
        decision, reviewer: value("reviewer"), rationale: note.value.trim(), source_verified: decision === "approved" && checked.checked});
      await loadInspection();
    }
    approve.onclick = () => run(() => review("approved")); reject.onclick = () => run(() => review("rejected"));
    const preview = document.createElement("button"); preview.textContent = "Embedding preview"; preview.dataset.vector = chunk.source_chunk_id;
    preview.onclick = () => run(async () => show("embeddingInfo", await json("embedding/" + encodeURIComponent(chunk.source_chunk_id) +
      "?tenant=" + encodeURIComponent(value("tenant")) + "&version_id=" + encodeURIComponent(value("version")) + "&provider=" + value("provider"))));
    card.append(title, context, text);
    if (chunk.warning_codes.length) { const warnings = document.createElement("p"); warnings.textContent = "Warnings: " + chunk.warning_codes.join(", "); card.append(warnings); }
    card.append(note, checkedLabel, approve, reject, preview);
    if (chunk.index_jobs.length) { const jobs = document.createElement("pre"); jobs.textContent = JSON.stringify(chunk.index_jobs, null, 2); card.append(jobs); }
    root.append(card);
  }
  updateControls();
}
el("connect").onclick = () => run(async () => { const health = await json("health"); dimension=health.dimension; show("health", health);
  if (!value("reviewer") && health.authorized_reviewers.length === 1) el("reviewer").value = health.authorized_reviewers[0];
  await loadVersions(); });
el("submit").onclick = () => run(async () => {
  const file = el("file").files[0]; if (!file) throw new Error("Choose a file first.");
  const data = new FormData(); data.append("file", file); data.append("tenant", value("tenant"));
  if (value("existingDoc")) data.append("document_id", value("existingDoc"));
  if (value("schedule")) data.append("schedule_kind", value("schedule"));
  const result = await (await api("submit", {method: "POST", body: data})).json(); show("jobs", result);
  el("job").value = result.job_id || ""; el("version").value = result.version_id; el("document").value = result.document_id;
  el("external").checked = false;
  offset = 0; inspection = null; el("chunks").replaceChildren(); await loadVersions();
});
el("ingest").onclick = () => run(async () => { show("jobs", await json("ingest", {tenant:value("tenant"), job_id:value("job")})); offset = 0; await loadInspection(); });
el("retry").onclick = () => run(async () => show("jobs", await json("retry", {tenant:value("tenant"), job_id:value("job")})));
el("jobStatus").onclick = () => run(async () => show("jobs", await json("status/" + encodeURIComponent(value("job")) + "?tenant=" + encodeURIComponent(value("tenant")))));
el("versions").onclick = () => run(loadVersions);
el("versionPicker").onchange = () => run(async () => { const option = el("versionPicker").selectedOptions[0]; if (!option.value) return;
  el("version").value = option.value; el("document").value = option.dataset.document; el("job").value = option.dataset.job;
  el("external").checked = false; offset = 0; await loadInspection(); });
el("inspect").onclick = () => run(async () => { offset = 0; await loadInspection(); });
el("previous").onclick = () => run(async () => { offset = Math.max(0,offset-50); await loadInspection(); });
el("next").onclick = () => run(async () => { if (nextOffset !== null) { offset=nextOffset; await loadInspection(); } });
el("provider").onchange = () => { el("external").checked = false; updateControls(); };
el("markdown").onclick = () => run(async () => show("artifact", await (await api(versionPath("markdown"))).text()));
el("manifest").onclick = () => run(async () => show("artifact", await json(versionPath("manifest"))));
el("source").onclick = () => run(async () => {
  const response = await api(versionPath("source")), url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href=url; link.download=inspection?.version.source_file || "source"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
});
el("publish").onclick = () => run(async () => { show("indexInfo", await json("publish", identifiers())); await loadInspection(); });
el("index").onclick = () => run(async () => { show("indexInfo", await json("index", {...identifiers(),confirm_external:el("external").checked})); await loadInspection(); });
el("reconcile").onclick = () => run(async () => { show("indexInfo", await json("reconcile", identifiers())); await loadInspection(); });
el("repair").onclick = () => run(async () => { show("indexInfo", await json("repair", identifiers())); await loadInspection(); });
el("search").onclick = () => run(async () => { const hits = await json("search",{tenant:value("tenant"),question:value("question"),provider:value("provider")});
  show("results", hits.length ? hits : "No approved indexed evidence matched in this workspace and provider."); });
el("schedules").onclick = () => run(async () => {
  const params = new URLSearchParams({tenant:value("tenant"),kind:value("kind")});
  for (const [input,parameter] of [["technology","technology_nm"],["flavor","flavor"],["tapeout","tapeout_on_or_after"]]) if(value(input)) params.set(parameter,value(input));
  show("scheduleResults",await json("schedules?"+params));
});
updateControls();
