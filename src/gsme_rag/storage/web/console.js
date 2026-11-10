"use strict";
const el = id => document.getElementById(id);
const value = id => el(id).value.trim();
let offset = 0, nextOffset = null, inspection = null, dimension = 1024, connected = false;
const pageSize = 10;
// Selections never carry over to another page, version or workspace.
const selectedChunks = new Set();
function selectionChanged() {
  el("selectionInfo").textContent=`${selectedChunks.size} selected`;
  el("batchVerified").checked=false;
  document.querySelectorAll("input[data-select-chunk]").forEach(input => {
    input.checked=selectedChunks.has(input.dataset.selectChunk);
  });
}

function showScreen(name) {
  document.querySelectorAll("[data-screen]").forEach(button => {
    const selected=button.dataset.screen===name;
    button.setAttribute("aria-selected",String(selected)); button.tabIndex=selected ? 0 : -1;
    el("panel-"+button.dataset.screen).hidden=!selected;
  });
}
const tabs=Array.from(document.querySelectorAll("[data-screen]"));
tabs.forEach((button,index)=>{
  button.onclick=()=>showScreen(button.dataset.screen);
  button.onkeydown=event=>{
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
    event.preventDefault();
    const next=event.key==="Home" ? 0 : event.key==="End" ? tabs.length-1 : (index+(event.key==="ArrowRight" ? 1 : -1)+tabs.length)%tabs.length;
    showScreen(tabs[next].dataset.screen); tabs[next].focus();
  };
});

const evidenceText=window.gsmeEvidence;

// The token stays in this page's input; it is never saved to browser storage.
async function api(path, options = {}) {
  const headers = {"X-GSME-Token": value("token")};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {...options, headers});
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail; } catch (_) { /* HTTP text is enough. */ }
    const requestId=response.headers.get("X-GSME-Request-ID");
    throw new Error((typeof detail === "string" ? detail : JSON.stringify(detail)) + (requestId ? ` [request ${requestId}]` : ""));
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
  const buttons = Array.from(document.querySelectorAll("button:not([data-screen]),input,select,textarea"));
  const disabled = buttons.map(button => button.disabled);
  buttons.forEach(button => button.disabled = true);
  try { const message=await action(); el("message").textContent = typeof message==="string" ? message : "Done."; }
  catch (error) { el("message").textContent = "Error: " + error.message; el("message").className = "error"; }
  finally {
    buttons.forEach((button, index) => { if (button.isConnected) button.disabled = disabled[index]; });
    updateControls();
  }
}
function selectedModel() { return (value("provider") === "fake" ? "test-hash-v1-" : "cohere-embed-v5-pro-fast-") + dimension; }
function updateControls() {
  el("uploadPermission").hidden=el("indexingPermission").hidden=el("retrievalPermission").hidden=value("provider")==="fake";
  for (const id of ["submit","versions","retrieve","search","schedules","showLogs"]) el(id).disabled=!connected;
  for (const id of ["inspect","source","markdown","manifest","publish","index","makeSearchable","ingestExisting","reconcile","repair"]) el(id).disabled=!connected || !value("version");
  for (const id of ["selectPending","clearSelection","approveSelected","rejectSelected"]) el(id).disabled=!connected || !inspection?.chunks.length;
  if (!inspection || inspection.version.document_class==="schedule") {
    for (const id of ["publish","index","makeSearchable","ingestExisting","reconcile","repair"]) el(id).disabled=true;
  }
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
  selectedChunks.clear(); selectionChanged(); el("batchNote").value="";
  inspection = await json(versionPath("inspect") + "&limit=" + pageSize + "&offset=" + offset);
  nextOffset = inspection.next_offset;
  el("document").value = inspection.version.document_id;
  el("reviewSummary").textContent=`${inspection.version.source_file} · ${inspection.total_chunks} chunks · ${inspection.version.is_current ? "published version" : "not published"}`;
  const states={};
  for (const job of inspection.index_jobs.filter(job=>job.model_version===selectedModel())) states[job.state]=(states[job.state] || 0)+Number(job.chunks);
  el("indexSummary").textContent=Object.entries(states).map(([state,count])=>`${count} ${state}`).join(" · ") || "Approve checked chunks, then click Make searchable.";
  show("versionInfo", {source: inspection.version.source_file, state: inspection.version.state,
    current: inspection.version.is_current, total_chunks: inspection.total_chunks,
    source_sha256: inspection.version.source_sha256, parser_version: inspection.version.parser_version,
    index_jobs: inspection.index_jobs});
  el("pageInfo").textContent = inspection.total_chunks ? `${offset + 1}–${offset + inspection.chunks.length} of ${inspection.total_chunks}` :
    inspection.version.document_class === "schedule" ? "Schedule rows have their own exact search." : "No parsed chunks yet; run or check the ingestion job.";
  const root = el("chunks"); root.replaceChildren();
  for (const chunk of inspection.chunks) {
    const card = document.createElement("details"); card.className = "chunk";
    card.open=chunk===inspection.chunks.find(item=>item.evidence_status==="pending");
    const title = document.createElement("summary"); title.className = "state";
    title.textContent = `Chunk ${chunk.chunk_index === null ? "order unknown" : chunk.chunk_index+1} · ${chunk.evidence_status}${chunk.source_page_start ? " · page "+chunk.source_page_start : ""}${chunk.review_required ? " · source check needed" : ""}`;
    const context = document.createElement("p"); context.className = "muted";
    context.textContent = (chunk.heading_path || []).join(" → ") + ` | ${chunk.chunk_type} | ${chunk.source_chunk_id}`;
    const text = evidenceText(chunk.evidence_text);
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
      return decision==="approved" ? "Chunk approved. Review the next pending chunk, or make approved chunks searchable." : "Chunk rejected. It will not be used as approved evidence.";
    }
    approve.onclick = () => run(() => review("approved")); reject.onclick = () => run(() => review("rejected"));
    const preview = document.createElement("button"); preview.textContent = "Embedding preview"; preview.dataset.vector = chunk.source_chunk_id;
    preview.onclick = () => run(async () => show("embeddingInfo", await json("embedding/" + encodeURIComponent(chunk.source_chunk_id) +
      "?tenant=" + encodeURIComponent(value("tenant")) + "&version_id=" + encodeURIComponent(value("version")) + "&provider=" + value("provider"))));
    const selectionLabel=document.createElement("label"), selection=document.createElement("input");
    selection.type="checkbox"; selection.dataset.selectChunk=chunk.source_chunk_id;
    selection.disabled=chunk.evidence_status==="excluded";
    selection.onchange=()=>{ if(selection.checked) selectedChunks.add(chunk.source_chunk_id); else selectedChunks.delete(chunk.source_chunk_id); selectionChanged(); };
    selectionLabel.append(selection,document.createTextNode(" Include in selected-chunk review"));
    card.append(title, selectionLabel, context, text);
    if (chunk.warning_codes.length) { const warnings = document.createElement("p"); warnings.className="error";
      warnings.textContent = chunk.warning_codes.includes("link_target_missing") ? "This answer refers to 'here' without a usable destination. Get the original link; do not guess it." : "Source checks: " + chunk.warning_codes.map(code=>code.replaceAll("_"," ")).join(", "); card.append(warnings); }
    if(chunk.approval_basis==="automatic_clean_text_v1" && chunk.evidence_status==="approved") {
      const accepted=document.createElement("p"); accepted.className="muted";
      accepted.textContent="Accepted automatically as clean text. No human source check is claimed; no review note is needed.";
      const override=document.createElement("details"), summary=document.createElement("summary");
      summary.textContent="Optional manual review / revoke"; override.append(summary,note,checkedLabel,approve,reject);
      card.append(accepted,override,preview);
    } else card.append(note, checkedLabel, approve, reject, preview);
    if (chunk.index_jobs.length) { const details=document.createElement("details"), summary=document.createElement("summary"); summary.textContent="Index details";
      const jobs = document.createElement("pre"); jobs.textContent = JSON.stringify(chunk.index_jobs, null, 2); details.append(summary,jobs); card.append(details); }
    root.append(card);
  }
  updateControls();
}
el("selectPending").onclick=()=>{
  for(const chunk of inspection?.chunks || []) {
    if(chunk.evidence_status==="pending" && !chunk.warning_codes.includes("link_target_missing")) selectedChunks.add(chunk.source_chunk_id);
  }
  selectionChanged();
};
el("clearSelection").onclick=()=>{selectedChunks.clear(); selectionChanged();};
async function reviewSelected(decision) {
  if(!selectedChunks.size) throw new Error("Select the chunks you checked first.");
  if(!value("batchNote")) throw new Error("Add a shared note describing the source pages and records you checked.");
  if(decision==="approved" && !el("batchVerified").checked) throw new Error("Confirm you checked every selected chunk against the source.");
  const result=await json("review/batch",{tenant:value("tenant"),version_id:value("version"),chunk_ids:[...selectedChunks],decision,
    reviewer:value("reviewer"),rationale:value("batchNote"),source_verified:decision==="approved" && el("batchVerified").checked});
  await loadInspection();
  return `${result.recorded} chunks ${decision}. Unselected chunks are unchanged. Use Make searchable for approved chunks.`;
}
el("approveSelected").onclick=()=>run(()=>reviewSelected("approved"));
el("rejectSelected").onclick=()=>run(()=>reviewSelected("rejected"));
el("connect").onclick = () => run(async () => { const health = await json("health"); dimension=health.dimension; show("health", health);
  if (!value("reviewer") && health.authorized_reviewers.length === 1) el("reviewer").value = health.authorized_reviewers[0];
  await loadVersions(); connected=true; el("connection").open=false; el("connectionState").textContent="Connected · "+value("tenant");
  return "Connected. Upload a document or open an existing version."; });
el("submit").onclick = () => run(async () => {
  const file = el("file").files[0]; if (!file) throw new Error("Choose a file first.");
  const parseOnly=el("parseOnly").checked, confirmExternal=el("uploadExternal").checked;
  if (!parseOnly && value("provider")==="cohere" && !confirmExternal && !file.name.toLowerCase().endsWith(".csv"))
    throw new Error("Permit sending accepted text to Cohere, or choose local demo / parse only.");
  el("ingestSummary").textContent="Parsing document...";
  const data = new FormData(); data.append("file", file); data.append("tenant", value("tenant"));
  if (value("existingDoc")) data.append("document_id", value("existingDoc"));
  if (value("schedule")) data.append("schedule_kind", value("schedule"));
  const result = await (await api("submit", {method: "POST", body: data})).json(); show("jobs", result);
  el("job").value = result.job_id || ""; el("version").value = result.version_id; el("document").value = result.document_id;
  el("external").checked = false;
  offset = 0; inspection = null; el("chunks").replaceChildren();
  if (result.job_id && !result.no_op) {
    el("message").textContent="Parsing the uploaded document. Large PDFs may take several minutes...";
    const parsed=await json("ingest",{tenant:value("tenant"),job_id:result.job_id}); show("jobs",parsed);
    if (parsed.state==="failed") throw new Error("Parsing failed. Open parsing tools / job response for details.");
  }
  await loadVersions(); await loadInspection();
  if (inspection.version.document_class==="schedule") {
    if (inspection.version.schedule_kind) el("kind").value=inspection.version.schedule_kind;
    showScreen("schedules"); return "Schedule parsed. Use exact filters to find its rows; semantic indexing is not required.";
  }
  if (!parseOnly) {
    const result=await json("ingest-ready",{...identifiers(),confirm_external:confirmExternal});
    show("jobs",result); await loadInspection();
    const message=ingestionMessage(result); el("ingestSummary").textContent=message;
    if(result.state==="searchable") showScreen("search"); else showScreen("review");
    return message;
  }
  showScreen("review");
  return "Document ready for source review. No chunks have been automatically approved.";
});
el("ingest").onclick = () => run(async () => { show("jobs", await json("ingest", {tenant:value("tenant"), job_id:value("job")})); offset = 0; await loadInspection(); });
el("retry").onclick = () => run(async () => show("jobs", await json("retry", {tenant:value("tenant"), job_id:value("job")})));
el("jobStatus").onclick = () => run(async () => show("jobs", await json("status/" + encodeURIComponent(value("job")) + "?tenant=" + encodeURIComponent(value("tenant")))));
el("versions").onclick = () => run(loadVersions);
el("versionPicker").onchange = () => run(async () => { const option = el("versionPicker").selectedOptions[0]; if (!option.value) return;
  el("version").value = option.value; el("document").value = option.dataset.document; el("job").value = option.dataset.job;
  el("external").checked = false; offset = 0; await loadInspection(); showScreen(inspection.version.document_class==="schedule" ? "schedules" : "review"); });
el("inspect").onclick = () => run(async () => { offset = 0; await loadInspection(); });
el("previous").onclick = () => run(async () => { offset = Math.max(0,offset-pageSize); await loadInspection(); });
el("next").onclick = () => run(async () => { if (nextOffset !== null) { offset=nextOffset; await loadInspection(); } });
el("provider").onchange = () => { el("uploadExternal").checked=false; el("external").checked = false; el("retrievalExternal").checked = false; updateControls(); };
el("showLogs").onclick=()=>run(async()=>{
  const result=await json("logs"); show("operationLogs",result.entries.join("\n") || result.note || "No recent operations.");
});
el("file").onchange=()=>{el("uploadExternal").checked=false; el("ingestSummary").textContent="";};
for (const input of ["question","tenant","retrievalDocument","retrievalCategory","retrievalSource"]) {
  el(input).addEventListener("input",()=>{ el("retrievalExternal").checked=false; });
}
for (const input of ["tenant","token"]) el(input).addEventListener("input",()=>{
  selectedChunks.clear(); selectionChanged(); el("batchNote").value="";
  el("uploadExternal").checked=false; el("ingestSummary").textContent="";
  show("operationLogs", "");
  connected=false; inspection=null; offset=0; nextOffset=null;
  for (const id of ["job","version","document"]) el(id).value="";
  for (const id of ["chunks","retrievalResults","rankingComparison"]) el(id).replaceChildren();
  for (const id of ["retrievalTrace","artifact","versionInfo","health","jobs","results","scheduleResults","indexInfo","embeddingInfo"]) show(id,"");
  el("external").checked=false; el("retrievalExternal").checked=false;
  el("versionPicker").replaceChildren(); el("connectionState").textContent="Reconnect required";
  el("reviewSummary").textContent="Connect and choose a document first."; el("indexSummary").textContent=""; el("searchSummary").textContent="";
  updateControls();
});
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
function ingestionMessage(result) {
  const summary=`${result.accepted} accepted for search; ${result.held} extraction issues held aside; ${result.excluded} rejected/excluded.`;
  if(result.state==="held_for_review") return `Document stored. ${summary} Open extraction issues; no text was embedded.`;
  if(result.state==="index_failed" || result.state==="index_pending") return `${summary} ${result.failed} indexing failures; ${result.remaining} jobs still waiting. See index details.`;
  return `Document ready to search. ${summary}`;
}
el("ingestExisting").onclick=()=>run(async()=>{
  const result=await json("ingest-ready",{...identifiers(),confirm_external:el("external").checked});
  show("indexInfo",result); await loadInspection();
  const message=ingestionMessage(result); el("ingestSummary").textContent=message;
  if(result.state==="searchable") showScreen("search");
  return message;
});
el("makeSearchable").onclick=()=>run(async()=>{
  const ids=identifiers(), confirm=el("external").checked;
  if (ids.provider==="cohere" && !confirm) throw new Error("Confirm sending this version's approved text to Cohere first.");
  await json("publish",ids);
  let processed=0,failed=0;
  // Bound each click to twenty batches. Future-due retries and failures stay
  // visible rather than spinning indefinitely or silently reapproving content.
  for (let batch=0;batch<20;batch++) {
    el("message").textContent=`Indexing approved chunks: batch ${batch+1} (up to 20)...`;
    const result=await json("index",{...ids,confirm_external:confirm}); show("indexInfo",result);
    processed+=result.index.indexed; failed+=result.index.failed;
    if (!result.index.indexed || result.index.failed) break;
  }
  await loadInspection();
  if (failed) el("indexDetails").open=true;
  const pending=inspection.index_jobs.filter(job=>job.model_version===selectedModel() && ["queued","indexing","failed"].includes(job.state)).reduce((sum,job)=>sum+Number(job.chunks),0);
  return failed ? `Some chunks failed. Open index details before continuing. ${el("indexSummary").textContent}` : pending ? `${processed} processed; ${pending} still waiting. Click Make searchable to continue due work.` : "Approved chunks are searchable. Open Ask sources to test them.";
});
el("search").onclick = () => run(async () => { const hits = await json("search",{tenant:value("tenant"),question:value("question"),provider:value("provider")});
  show("results", hits.length ? hits : "No approved indexed evidence matched in this workspace and provider."); });
el("retrieve").onclick = () => run(async () => {
  el("retrievalResults").replaceChildren(); el("rankingComparison").replaceChildren(); el("searchSummary").textContent="Searching and reranking..."; show("retrievalInfo", ""); show("retrievalTrace", "");
  const body = {tenant:value("tenant"), question:value("question"), provider:value("provider"),
    confirm_external:el("retrievalExternal").checked, strategy:value("retrievalStrategy"),
    candidate_count:Number(value("candidateCount")), top_k:Number(value("topK"))};
  for (const [input, field] of [["retrievalDocument","document_id"],["retrievalCategory","category"],["retrievalSource","source_file"]])
    if (value(input)) body[field] = value(input);
  let response;
  try { response = await json("retrieve",body); }
  catch (error) { el("searchSummary").textContent=`Search failed: ${error.message}. No unreranked fallback was returned.`; throw error; }
  el("searchSummary").textContent=response.results.length ? `${response.results.length} evidence results · ${Math.round(response.timings_ms.total)} ms${response.synthetic ? " · local demo, not a quality benchmark" : ""}` : `No evidence returned (${response.status.replaceAll("_"," ")}).`;
  show("retrievalInfo", {status:response.status, synthetic:response.synthetic, strategy:response.strategy,
    reranking:response.reranking_status, model:response.reranker_model, candidates:response.candidate_count,
    timings_ms:response.timings_ms, warnings:response.warnings, note:response.note});
  show("retrievalTrace",response);
  if (response.reranked_candidates.length) {
    const table=document.createElement("table"), head=document.createElement("tr");
    for (const label of ["Source / chunk", "Initial rank", "Rerank position", "Relevance (not confidence)"]) {
      const cell=document.createElement("th"); cell.textContent=label; head.append(cell);
    }
    table.append(head);
    for (const item of response.reranked_candidates) {
      const row=document.createElement("tr");
      for (const content of [`${item.source_file} | ${item.chunk_id}`, item.retrieval_rank, item.rerank_rank, item.rerank_score.toFixed(4)]) {
        const cell=document.createElement("td"); cell.textContent=String(content); row.append(cell);
      }
      table.append(row);
    }
    el("rankingComparison").append(table);
  }
  for (const [index, item] of response.results.entries()) {
    const card = document.createElement("div"); card.className="chunk";
    const title = document.createElement("h3"); title.textContent=`${index+1}. ${item.faq_question || item.source_file}`;
    const meta = document.createElement("p"); meta.textContent=`Candidate rank ${item.retrieval_rank} → rerank ${item.rerank_rank} | relevance ${item.rerank_score.toFixed(4)} (not confidence) | page ${item.page_start ?? "n/a"} | ${(item.heading_path || []).join(" / ")}`;
    const text = evidenceText(item.context_parts.map(part=>
      item.part_count > 1 ? `Part ${part.part_index+1}/${part.part_count} | page ${part.page_start ?? "n/a"} | ${part.chunk_id}\n${part.text}` : part.text).join("\n\n"));
    const provenance = document.createElement("p"); provenance.className="muted";
    provenance.textContent=`Version ${item.version_id} | chunk ${item.chunk_id} | source SHA256 ${item.source_sha256}`;
    const source = document.createElement("button"); source.className="secondary"; source.textContent="Download original";
    source.onclick=()=>run(async()=>{
      const path="source/"+encodeURIComponent(item.version_id)+"?tenant="+encodeURIComponent(body.tenant);
      const response=await api(path), url=URL.createObjectURL(await response.blob());
      const link=document.createElement("a"); link.href=url; link.download=item.source_file; link.click();
      setTimeout(()=>URL.revokeObjectURL(url),60000);
    });
    const location=document.createElement("p"); location.className="muted"; location.textContent=`${item.source_file}${item.page_start ? " · page "+item.page_start : ""}`;
    const details=document.createElement("details"), summary=document.createElement("summary"); summary.textContent="Source details & ranking"; details.append(summary,meta,provenance);
    card.append(title,location,text,source,details);
    if (item.part_count > 1) { const expanded=document.createElement("p"); expanded.className="muted"; expanded.textContent="Approved unit parts are shown in source order. The rerank score applies to the matched passage; expanded parts are supporting context."; card.append(expanded); }
    if (!item.context_complete) { const warning=document.createElement("p"); warning.className="error"; warning.textContent="Split answer context is incomplete: unavailable, unapproved, or over budget. Do not treat as a complete answer."; card.append(warning); }
    el("retrievalResults").append(card);
  }
});
el("schedules").onclick = () => run(async () => {
  const params = new URLSearchParams({tenant:value("tenant"),kind:value("kind")});
  for (const [input,parameter] of [["technology","technology_nm"],["flavor","flavor"],["tapeout","tapeout_on_or_after"]]) if(value(input)) params.set(parameter,value(input));
  show("scheduleResults",await json("schedules?"+params));
});
updateControls();
