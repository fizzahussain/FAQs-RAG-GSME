"use strict";
const consoleBase = new URL(".", document.currentScript.src).pathname.replace(/\/assets\/$/, "");
const el = id => document.getElementById(id);
const value = id => el(id).value.trim();
let offset = 0, nextOffset = null, inspection = null, dimension = 1024, connected = false;
let busy = false, libraryVersions = [], previewUrl = null, previewVersion = null;
let previewPageUrl=null, previewPage=1, previewIsPdf=false;
let lockedControls = null;
const pageSize = 25;
// A source-page view can include an intact table in one bounded review batch.
const reviewLimit = () => value("reviewPage") ? 100 : pageSize;
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
  const screens = {documents:["Document library","Add sources, update existing documents, and choose what to share."],
    review:["Source review","Compare held evidence with the original. Leave uncertain passages out."],
    jobs:["Jobs & logs","Track parsing and indexing, and find the reason when something stops."],
    search:["Test retrieval","Inspect matching passages and compare their ranks before and after reranking."],
    schedules:["Schedule records","Check exact imported dates and source rows without an AI call."]};
  el("screenTitle").textContent=screens[name][0]; el("screenDescription").textContent=screens[name][1];
}
const tabs=Array.from(document.querySelectorAll("[data-screen]"));
tabs.forEach((button,index)=>{
  button.onclick=()=>{showScreen(button.dataset.screen); if(button.dataset.screen==="jobs" && connected) run(loadJobs);};
  button.onkeydown=event=>{
    if (!["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Home","End"].includes(event.key)) return;
    event.preventDefault();
    const next=event.key==="Home" ? 0 : event.key==="End" ? tabs.length-1 : (index+(["ArrowRight","ArrowDown"].includes(event.key) ? 1 : -1)+tabs.length)%tabs.length;
    showScreen(tabs[next].dataset.screen); tabs[next].focus();
  };
});

const evidenceText=window.gsmeEvidence;

// The token stays in this page's input; it is never saved to browser storage.
async function api(path, options = {}) {
  const headers = {"X-GSME-Token": value("token")};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(consoleBase + "/api/" + path, {...options, headers});
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
  if (busy) return;
  busy=true;
  el("message").className = "";
  el("message").textContent = "Working...";
  lockedControls=new Map();lockControls();
  try { const message=await action(); el("message").textContent = typeof message==="string" ? message : "Done."; }
  catch (error) { el("message").textContent = "Error: " + error.message; el("message").className = "error"; }
  finally {
    busy=false;
    for(const [control,disabled] of lockedControls) if(control.isConnected) control.disabled=disabled;
    lockedControls=null;
    updateControls();
  }
}
function lockControls() {
  if(!lockedControls) return;
  document.querySelectorAll("button:not([data-screen]),input,select,textarea").forEach(control=>{
    if(!lockedControls.has(control)) lockedControls.set(control,control.disabled);
    control.disabled=true;
  });
}
function selectedModel() { return (value("provider") === "fake" ? "test-hash-v1-" : "cohere-embed-v5-pro-fast-") + dimension; }
function updateControls() {
  el("modeState").textContent=value("provider")==="fake" ? "Local demo · synthetic vectors" : "Cohere · live provider";
  el("modeState").className=value("provider")==="fake" ? "badge warning" : "badge";
  for(const id of ["newUpload","refreshJobs","documentSearch"]) el(id).disabled=!connected;
  for(const id of ["openReview","reviewStatus","reviewPage","clearReviewFilters","coverage"]) el(id).disabled=!connected || !inspection;
  for(const id of ["ingest","retry","jobStatus"]) el(id).disabled=!connected || !value("job");
  el("closePreview").disabled=!previewUrl;
  el("replaceSelected").disabled=!connected || !inspection;
  el("shareVersion").disabled=!connected || !inspection?.version.is_current ||
    !["approved","indexing","indexed"].includes(inspection?.version.state) || !el("confirmShare").checked;
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
  document.querySelectorAll("button[data-operator-action]").forEach(button=>button.disabled=!connected);
  if(busy) lockControls();
}
function friendlyState(state) {
  return ({queued:"Waiting to parse",processing:"Parsing",parsing:"Parsing",parsed:"Parsed",
    review_needed:"Ready for source review",approved:"Accepted · indexing may be needed",indexing:"Indexing accepted text",
    indexed:"Index processed",failed:"Needs attention",superseded:"Older version"})[state] || String(state || "Unknown").replaceAll("_"," ");
}
async function openVersion(item, screen="review") {
  clearPreview(); offset=0;
  el("version").value=item.id || item.version_id; el("document").value=item.document_id; el("job").value=item.job_id || "";
  el("versionPicker").value=value("version"); el("external").checked=false;
  el("reviewPage").value=""; el("reviewStatus").value="pending";
  await loadInspection(); showScreen(inspection.version.document_class==="schedule" ? "schedules" : screen);
}
function renderDocuments() {
  const root=el("documentList"); root.replaceChildren();
  const seen=new Set(), query=value("documentSearch").toLowerCase();
  for(const item of libraryVersions) {
    if(seen.has(item.document_id)) continue;
    seen.add(item.document_id);
    if(!item.source_file.toLowerCase().includes(query)) continue;
    const card=document.createElement("article"); card.className="document-card"+(item.id===value("version") ? " selected" : "");
    const name=document.createElement("button"); name.className="document-title"; name.textContent=item.source_file; name.dataset.operatorAction="open";
    name.onclick=()=>run(()=>openVersion(item,"documents"));
    const state=document.createElement("span"); state.className="badge"; state.textContent=friendlyState(item.state);
    const meta=document.createElement("p"); meta.className="document-meta";
    meta.textContent=`${item.approved_chunks} accepted · ${item.held_chunks} held · ${item.is_current ? "current version" : "latest attempt; check version history for current"}`;
    const review=document.createElement("button"); review.className="secondary"; review.textContent=item.document_class==="schedule" ? "Check rows" : "Review";
    review.dataset.operatorAction="review"; review.onclick=()=>run(()=>openVersion(item));
    const update=document.createElement("button"); update.className="secondary"; update.textContent="Update"; update.dataset.operatorAction="update";
    update.onclick=()=>run(async()=>{await openVersion(item,"documents"); beginReplacement();});
    card.append(name,state,meta,review,update); root.append(card);
  }
  if(!root.children.length) {const empty=document.createElement("p"); empty.className="empty-state"; empty.textContent=query ? "No document names match." : "No documents yet. Choose Add document to get started.";root.append(empty);}
  const note=document.createElement("p"); note.className="muted"; note.textContent="Latest 100 versions · One card per document · Use Version history for earlier versions."; root.append(note);
}
function renderReviewCounts() {
  const counts=inspection.status_counts || {}, root=el("reviewCounts"); root.replaceChildren();
  for(const [status,label] of [["approved","Accepted"],["pending","Needs review"],["rejected","Rejected"],["excluded","Excluded · not indexed"]]) {
    const card=document.createElement("div");card.className="stat";
    const number=document.createElement("strong"),text=document.createElement("span");number.textContent=counts[status] || 0;text.textContent=label;card.append(number,text);root.append(card);
  }
}
function selectDisplayedPending(page=null) {
  for(const chunk of inspection?.chunks || [])
    if(chunk.evidence_status==="pending" && !chunk.warning_codes.includes("link_target_missing") &&
      (page===null || (chunk.source_page_start ?? "unpaged")===page)) selectedChunks.add(chunk.source_chunk_id);
  selectionChanged();
}
function clearReplacement() {
  el("existingDoc").value="";el("updateBanner").hidden=true;el("cancelReplacement").hidden=true;
}
function beginReplacement() {
  if(!inspection) return;
  el("existingDoc").value=inspection.version.document_id; el("uploadPanel").open=true;
  el("updateBanner").textContent=`Updating ${inspection.version.source_file}. The document identity is preserved; the new shared version needs explicit publication.`;
  el("updateBanner").hidden=false; el("cancelReplacement").hidden=false;
  el("file").value="";el("uploadExternal").checked=false;
  if(inspection.version.schedule_kind) el("schedule").value=inspection.version.schedule_kind; else el("schedule").value="";
  showScreen("documents");el("file").focus();
  el("message").textContent="Choose the replacement file, then Ingest document. Older originals stay in version history.";
}
function clearPreview() {
  el("originalImage").removeAttribute("src");el("originalImage").hidden=true;
  el("originalDownload").removeAttribute("href");el("originalDownload").hidden=true;
  el("openSourceTab").hidden=true;
  if(previewUrl) URL.revokeObjectURL(previewUrl);
  if(previewPageUrl) URL.revokeObjectURL(previewPageUrl);
  previewPageUrl=null;previewPage=1;previewIsPdf=false;
  previewUrl=null;previewVersion=null;
  el("previewHint").textContent="View the original to compare it beside the extracted text. PDF and image previews stay local.";
}
async function renderPreviewPage(page) {
  el("originalImage").hidden=true;el("originalImage").removeAttribute("src");
  if(previewPageUrl) URL.revokeObjectURL(previewPageUrl);
  previewPageUrl=null;
  const response=await api(versionPath("preview")+"&page="+encodeURIComponent(page));
  const blob=await response.blob();previewPageUrl=URL.createObjectURL(blob);previewPage=page;
  el("originalImage").src=previewPageUrl;el("originalImage").hidden=false;
  el("previewHint").textContent=`${inspection.version.source_file} · page ${page} rendered from the original PDF, not OCR. Open the original for zoom.`;
}
async function previewOriginal() {
  const response=await api(versionPath("source")),blob=await response.blob();clearPreview();
  previewUrl=URL.createObjectURL(blob); previewVersion=value("version");
  const name=inspection.version.source_file, page=value("reviewPage") || inspection.chunks[0]?.source_page_start || 1;
  const download=el("originalDownload"); download.href=previewUrl;download.download=name;download.hidden=false;
  // Only native PDF/raster viewers are embedded. Office/HTML/SVG source bytes
  // never become executable markup in the privileged operator page.
  if(/\.pdf$/i.test(name) && blob.type==="application/pdf") {
    previewIsPdf=true;el("openSourceTab").hidden=false;
    await renderPreviewPage(Number(page));
  } else if(/\.(png|jpe?g|bmp)$/i.test(name) && /^image\/(png|jpeg|bmp)$/.test(blob.type)) {
    el("originalImage").src=previewUrl;el("originalImage").hidden=false;el("openSourceTab").hidden=false;el("previewHint").textContent=name;
  } else el("previewHint").textContent="This format has no native browser preview. Download the original and compare it in its own application.";
}
async function loadJobs() {
  const jobs=await json("jobs?limit=50&tenant="+encodeURIComponent(value("tenant"))),root=el("jobList");root.replaceChildren();
  for(const job of jobs) {
    const card=document.createElement("article");card.className="job-card";
    const title=document.createElement("h3");title.textContent=job.source_file;
    const state=document.createElement("span");state.className="badge";
    const autoWaiting=job.hosted_auto_process && job.hosted_error_code;
    state.textContent=autoWaiting ? "Waiting · automatic processing enrolled" : friendlyState(job.version_state);
    const summary=document.createElement("p");summary.className="muted";
    summary.textContent=`Parse: ${friendlyState(job.state)} · ${(job.index_states || []).map(s=>`${s.count} ${s.state}`).join(" · ") || "No index jobs"} (all model versions)`;
    const reason=document.createElement("p");reason.className="muted";
    reason.textContent=job.hosted_error_code ? `Reason: ${job.hosted_error_code.replaceAll("_"," ")}. ${job.hosted_auto_process ? "Next attempt is scheduled; " : "Operator action needed; "}open the document for details.` : "Open the document to review evidence, check its job, or index accepted text.";
    if(autoWaiting && job.hosted_next_attempt_at) reason.textContent+=` Next due: ${new Date(job.hosted_next_attempt_at).toLocaleString()}.`;
    const open=document.createElement("button");open.className="secondary";open.dataset.operatorAction="open-job";open.textContent="Open document";
    open.onclick=()=>run(()=>openVersion({...job,id:job.version_id,job_id:job.id}));
    const details=document.createElement("details"),caption=document.createElement("summary"),raw=document.createElement("pre");caption.textContent="Technical job details";raw.textContent=JSON.stringify(job,null,2);details.append(caption,raw);
    card.append(title,state,summary,reason,open);
    if(job.state==="failed") {
      const retry=document.createElement("button");retry.className="secondary";retry.textContent="Retry local parsing";retry.dataset.operatorAction="retry-job";
      retry.onclick=()=>run(async()=>{
        const body={tenant:value("tenant"),job_id:job.id};
        await json("retry",body);const result=await json("ingest",body);await loadJobs();
        return result.state==="failed" ? "Parsing failed again. Open technical job details for its stored error." : "Parsing retried. Open the document to check evidence before indexing; no external embedding was requested.";
      });card.append(retry);
    }
    card.append(details);root.append(card);
  }
  if(!jobs.length) {const empty=document.createElement("p");empty.className="empty-state";empty.textContent="No jobs in this workspace.";root.append(empty);}
  updateControls();return `Showing ${jobs.length} most recent processing jobs. No processing was started.`;
}
async function loadVersions() {
  const versions = await json("versions?limit=100&tenant=" + encodeURIComponent(value("tenant")));
  libraryVersions=versions;
  const picker = el("versionPicker"); picker.replaceChildren();
  const initial = document.createElement("option"); initial.value = ""; initial.textContent = "Choose a version"; picker.append(initial);
  for (const item of versions) {
    const option = document.createElement("option"); option.value = item.id; option.dataset.document = item.document_id;
    option.dataset.job = item.job_id || "";
    option.textContent = `${item.source_file} | ${item.state} | ${item.approved_chunks}/${item.chunks} approved | ${item.id.slice(0,8)}`;
    picker.append(option);
  }
  picker.value = value("version");
  renderDocuments();
}
async function loadInspection() {
  el("coverageReport").replaceChildren();
  if(previewVersion && previewVersion!==value("version")) clearPreview();
  el("confirmShare").checked=false; el("shareSummary").textContent="";
  selectedChunks.clear(); selectionChanged(); el("batchNote").value="";
  const filters = new URLSearchParams();
  if(value("reviewStatus")) filters.set("status",value("reviewStatus"));
  if(value("reviewPage")) {
    const page=Number(value("reviewPage"));
    if(!Number.isSafeInteger(page) || page<1) throw new Error("Source page must be a positive whole number.");
    filters.set("page",page);
  }
  inspection = await json(versionPath("inspect") + "&limit=" + reviewLimit() + "&offset=" + offset + "&" + filters);
  if(offset && !inspection.chunks.length) {offset=0; return loadInspection();}
  nextOffset = inspection.next_offset;
  el("document").value = inspection.version.document_id;
  const listed=libraryVersions.find(item=>item.id===value("version"));
  if(listed) Object.assign(listed,{state:inspection.version.state,is_current:inspection.version.is_current,
    chunks:inspection.total_chunks,approved_chunks:inspection.status_counts?.approved || 0,
    held_chunks:inspection.status_counts?.pending || 0});
  el("selectedName").textContent=inspection.version.source_file;
  el("selectedStatus").textContent=`${friendlyState(inspection.version.state)} · ${inspection.version.is_current ? "current version" : "not the current version"}. Sharing is a separate action.`;
  renderDocuments(); renderReviewCounts();
  el("reviewSummary").textContent=`${inspection.version.source_file} · ${inspection.total_chunks} total passages · ${inspection.version.is_current ? "current source version" : "not the current source version"}`;
  const states={};
  for (const job of inspection.index_jobs.filter(job=>job.model_version===selectedModel())) states[job.state]=(states[job.state] || 0)+Number(job.chunks);
  el("indexSummary").textContent=Object.entries(states).map(([state,count])=>`${count} ${state}`).join(" · ") || "No index jobs for this processing mode yet. Accept evidence first, then index it with the intended provider.";
  show("versionInfo", {source: inspection.version.source_file, state: inspection.version.state,
    current: inspection.version.is_current, total_chunks: inspection.total_chunks,
    source_sha256: inspection.version.source_sha256, parser_version: inspection.version.parser_version,
    index_jobs: inspection.index_jobs});
  const filtered=inspection.filtered_chunks ?? inspection.total_chunks;
  el("pageInfo").textContent = filtered ? `${offset + 1}–${offset + inspection.chunks.length} of ${filtered} matching passages` :
    inspection.version.document_class === "schedule" ? "Schedule rows have their own exact search." : inspection.total_chunks ? "0 matching passages" : "No parsed passages yet; check the processing job.";
  const root = el("chunks"); root.replaceChildren();
  if(!inspection.chunks.length) {
    const empty=document.createElement("p"); empty.className="empty-state";
    empty.textContent=inspection.version.document_class==="schedule" ? "Schedules use exact rows, not passage approvals. Open Schedules to check them." :
      "No passages match these filters. Choose All passages to inspect accepted or excluded evidence.";
    root.append(empty);
  }
  const seenPages=new Set();
  for (const chunk of inspection.chunks) {
    const page=chunk.source_page_start ?? "unpaged";
    if(!seenPages.has(page)) {
      seenPages.add(page);
      const group=document.createElement("div"); group.className="page-group";
      const title=document.createElement("h3"); title.textContent=page==="unpaged" ? "Document passages" : `Page ${page}`;
      const select=document.createElement("button"); select.className="secondary"; select.textContent="Select displayed held passages";
      select.dataset.operatorAction="select-group";
      select.onclick=()=>{selectDisplayedPending(page); el("batchReview").open=true;};
      group.append(title,select); root.append(group);
    }
    const card = document.createElement("details"); card.className = "chunk";
    card.open=chunk===inspection.chunks.find(item=>item.evidence_status==="pending");
    const title = document.createElement("summary"); title.className = "state";
    const location=chunk.source_page_start ? ` · page ${chunk.source_page_start}${chunk.source_page_end && chunk.source_page_end!==chunk.source_page_start ? "–"+chunk.source_page_end : ""}` : "";
    title.textContent = `Chunk ${chunk.chunk_index === null ? "order unknown" : chunk.chunk_index+1} · ${chunk.evidence_status}${location}${chunk.review_required ? " · source check needed" : ""}`;
    const context = document.createElement("p"); context.className = "muted";
    context.textContent = (chunk.heading_path || []).join(" → ") + ` · ${chunk.chunk_type.replaceAll("_"," ")}`;
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
    selectionLabel.append(selection,document.createTextNode(" Select for group review"));
    card.append(title, selectionLabel, context, text);
    if (chunk.warning_codes.length) { const warnings = document.createElement("p"); warnings.className="error";
      warnings.textContent = chunk.warning_codes.includes("link_target_missing") ? "This answer refers to 'here' without a usable destination. Get the original link; do not guess it." : "Source checks: " + chunk.warning_codes.map(code=>code.replaceAll("_"," ")).join(", "); card.append(warnings); }
    if(chunk.approval_basis==="automatic_clean_text_v1" && chunk.evidence_status==="approved") {
      const accepted=document.createElement("p"); accepted.className="muted";
      accepted.textContent="Accepted automatically as clean text. No human source check is claimed; no review note is needed.";
      const override=document.createElement("details"), summary=document.createElement("summary");
      summary.textContent="Optional manual review / revoke"; override.append(summary,note,checkedLabel,approve,reject);
      card.append(accepted,override,preview);
    } else {
      const individual=document.createElement("details"),summary=document.createElement("summary");
      summary.textContent="Review this passage individually";individual.append(summary,note,checkedLabel,approve,reject,preview);card.append(individual);
    }
    if (chunk.index_jobs.length) { const details=document.createElement("details"), summary=document.createElement("summary"); summary.textContent="Index details";
      const jobs = document.createElement("pre"); jobs.textContent = JSON.stringify(chunk.index_jobs, null, 2); details.append(summary,jobs); card.append(details); }
    root.append(card);
  }
  updateControls();
}
el("selectPending").onclick=()=>{
  selectDisplayedPending();
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
el("coverage").onclick = () => run(async () => {
  const report=await json(versionPath("coverage")), root=el("coverageReport");root.replaceChildren();
  const summary=document.createElement("p");
  summary.textContent=`${report.source_page_count ?? "Unknown number of"} source pages · ${report.pages_summarized} summarized · ${report.unattributed_chunks} passages without page attribution. Source ${report.source_available ? (report.source_readable ? "present" : "present but unreadable") : "missing"}; normalized text ${report.normalized_available ? "present" : "missing"}; warnings manifest ${report.manifest_available ? "present" : "missing"}.`;
  root.append(summary);
  const note=document.createElement("p");note.className="muted";note.textContent=report.note;root.append(note);
  if(report.page_display_truncated) {const warning=document.createElement("p");warning.textContent="Display limited to the first 5,000 pages.";root.append(warning);}
  const table=document.createElement("table"), head=document.createElement("tr");
  for(const label of ["Page","Accepted","Held","Excluded / rejected","Source-verified passages","Recorded warnings / diagnostics"]) {const th=document.createElement("th");th.textContent=label;head.append(th);}table.append(head);
  for(const p of report.pages) {
    const row=document.createElement("tr"), cell=document.createElement("td"), open=document.createElement("button");
    open.type="button";open.textContent=String(p.page);open.onclick=()=>run(async()=>{el("reviewPage").value=p.page;el("reviewStatus").value="";offset=0;await loadInspection();});cell.append(open);row.append(cell);
    for(const text of [p.approved,p.pending,`${p.excluded} / ${p.rejected}`,p.verified_chunks,
      `${p.warning_codes.join(", ") || (p.has_attributed_evidence ? "None recorded" : "No attributed evidence — inspect source")} · ${p.diagnostic_regions} regions`]) {const td=document.createElement("td");td.textContent=String(text);row.append(td);}table.append(row);
  }
  const scroll=document.createElement("div");scroll.className="table-scroll";scroll.append(table);root.append(scroll);
  if(report.document_warnings.length) {const warnings=document.createElement("pre");warnings.textContent=JSON.stringify(report.document_warnings,null,2);root.append(warnings);}
  return "Coverage inventory loaded. Open a page number to inspect its evidence.";
});
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
  clearReplacement();clearPreview();el("reviewStatus").value="pending";el("reviewPage").value="";
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
el("confirmShare").onchange=updateControls;
el("replaceSelected").onclick=()=>{
  beginReplacement();
};
el("shareVersion").onclick=()=>run(async()=>{
  const result=await json("share",{tenant:value("tenant"),document_id:value("document"),
    version_id:value("version"),confirm_public:el("confirmShare").checked});
  el("confirmShare").checked=false; el("shareSummary").textContent=result.message;
  return result.message;
});
el("versionPicker").onchange = () => run(async () => { const option = el("versionPicker").selectedOptions[0]; if (!option.value) return;
  await openVersion({id:option.value,document_id:option.dataset.document,job_id:option.dataset.job}); });
el("inspect").onclick = () => run(async () => { offset = 0; await loadInspection(); });
el("previous").onclick = () => run(async () => { offset = Math.max(0,offset-reviewLimit()); await loadInspection(); });
el("next").onclick = () => run(async () => { if (nextOffset !== null) { offset=nextOffset; await loadInspection(); } });
el("provider").onchange = () => { el("uploadExternal").checked=false; el("external").checked = false; el("retrievalExternal").checked = false; updateControls(); if(inspection) run(loadInspection); };
el("showLogs").onclick=()=>run(async()=>{
  const requestId=el("logRequestId").value.trim();
  const result=await json("logs?limit=200"+(requestId?"&request_id="+encodeURIComponent(requestId):"")); show("operationLogs",result.entries.join("\n") || result.note || "No matching operations in retained logs.");
});
el("file").onchange=()=>{el("uploadExternal").checked=false; el("ingestSummary").textContent="";};
for (const input of ["question","tenant","retrievalDocument","retrievalCategory","retrievalSource"]) {
  el(input).addEventListener("input",()=>{ el("retrievalExternal").checked=false; });
}
for (const input of ["tenant","token"]) el(input).addEventListener("input",()=>{
  clearPreview();clearReplacement();libraryVersions=[];
  el("documentList").replaceChildren();el("jobList").replaceChildren();el("reviewCounts").replaceChildren();
  el("selectedName").textContent="No document selected";el("selectedStatus").textContent="Reconnect to load documents.";
  el("reviewPage").value="";el("reviewStatus").value="pending";
  selectedChunks.clear(); selectionChanged(); el("batchNote").value="";
  el("uploadExternal").checked=false; el("ingestSummary").textContent="";
  show("operationLogs", "");
  connected=false; inspection=null; offset=0; nextOffset=null;
  el("existingDoc").value=""; el("confirmShare").checked=false; el("shareSummary").textContent="";
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
el("source").onclick = () => run(previewOriginal);
el("closePreview").onclick=()=>{clearPreview();updateControls();};
el("openSourceTab").onclick=()=>{if(previewUrl) window.open(previewUrl+(previewIsPdf ? "#page="+previewPage : ""),"_blank","noopener,noreferrer");};
window.addEventListener("beforeunload",clearPreview);
el("documentSearch").oninput=()=>{renderDocuments();updateControls();};
el("newUpload").onclick=()=>{clearReplacement();el("file").value="";el("uploadExternal").checked=false;el("uploadPanel").open=true;el("file").focus();};
el("cancelReplacement").onclick=clearReplacement;
el("openReview").onclick=()=>run(async()=>{el("reviewStatus").value="pending";el("reviewPage").value="";offset=0;await loadInspection();showScreen("review");});
for(const id of ["reviewStatus","reviewPage"]) el(id).onchange=()=>run(async()=>{
  offset=0;await loadInspection();
  if(previewUrl && previewIsPdf) await renderPreviewPage(Number(value("reviewPage") || inspection.chunks[0]?.source_page_start || 1));
});
el("clearReviewFilters").onclick=()=>run(async()=>{el("reviewPage").value="";el("reviewStatus").value="pending";offset=0;await loadInspection();});
el("refreshJobs").onclick=()=>run(loadJobs);
el("question").onkeydown=event=>{if(event.key==="Enter" && !event.isComposing){event.preventDefault();el("retrieve").click();}};
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
  const rows=await json("schedules?"+params);show("scheduleResults",rows);
  const root=el("scheduleTable");root.replaceChildren();
  if(!rows.length) {root.textContent="No schedule rows match these filters.";return;}
  const wrap=document.createElement("div");wrap.className="table-scroll";
  const table=document.createElement("table"),head=document.createElement("tr");
  const columns=[["technology_nm","Technology (nm)"],["flavor","Flavor"],["trial_deadline","Trial deadline"],["final_deadline","Final deadline"],["tapeout_date","Tapeout"],["estimated_ship_date","Estimated ship"]];
  for(const [,label] of columns){const cell=document.createElement("th");cell.textContent=label;head.append(cell);}table.append(head);
  for(const row of rows){const tr=document.createElement("tr");for(const [key] of columns){const td=document.createElement("td");td.textContent=row[key] ?? "—";tr.append(td);}table.append(tr);}
  const count=document.createElement("p");count.className="muted";count.textContent=`${rows.length} rows shown. Raw response below retains record IDs and provenance.`;
  wrap.append(table);root.append(count,wrap);
});
updateControls();
