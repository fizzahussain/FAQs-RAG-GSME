"use strict";
const $ = id => document.getElementById(id);
let credential = "", config = {}, busy = false, uploadBusy = false, initialized = false;
// Old requests must not repopulate private content after an identity change.
let sessionEpoch = 0;
let previewEpoch = 0, previewUrl = "";
let followConversation = true;
let libraryTimer;
const mobileNavigation = window.matchMedia("(max-width: 680px)");

function syncNavigation() {
  $("sidebar").close();
  if (!mobileNavigation.matches) $("sidebar").show();
}
function closeNavigation() { if (mobileNavigation.matches) $("sidebar").close(); }
$("open-sidebar").onclick = () => { $("sidebar").showModal(); $("open-sidebar").setAttribute("aria-expanded", "true"); };
$("close-sidebar").onclick = closeNavigation;
$("sidebar").addEventListener("close", () => $("open-sidebar").setAttribute("aria-expanded", "false"));
$("sidebar").addEventListener("click", event => {
  const bounds = $("sidebar").getBoundingClientRect();
  if (event.target === $("sidebar") && (event.clientX > bounds.right || event.clientY > bounds.bottom)) closeNavigation();
});
mobileNavigation.addEventListener("change", syncNavigation);
syncNavigation();

function scrollLatest(force = false) {
  if (force) followConversation = true;
  requestAnimationFrame(() => {
    if (followConversation) $("conversation-scroll").scrollTop = $("conversation-scroll").scrollHeight;
  });
}
$("conversation-scroll").addEventListener("scroll", () => {
  const timeline = $("conversation-scroll");
  followConversation = timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop < 100;
  $("jump-latest").hidden = followConversation || !$("messages").children.length;
}, {passive:true});
$("jump-latest").onclick = () => scrollLatest(true);
function resizeComposer() {
  $("question").style.height = "auto";
  $("question").style.height = Math.min($("question").scrollHeight, 180, window.innerHeight * .22) + "px";
}
$("question").addEventListener("input", resizeComposer);
$("new-chat").onclick = () => {
  if (busy || uploadBusy) return;
  $("messages").replaceChildren(); $("welcome").hidden = false; $("question").value = "";
  $("jump-latest").hidden = true; followConversation = true;
  $("notice").textContent = "New chat. Your documents are still available.";
  closePreview(); closeNavigation(); resizeComposer(); $("question").focus();
  requestAnimationFrame(() => { $("conversation-scroll").scrollTop = 0; });
};
for (const button of document.querySelectorAll("[data-question]")) button.onclick = () => {
  $("question").value = button.dataset.question; resizeComposer(); $("question").focus();
};

function filterDocuments() {
  const search = $("document-search").value.trim().toLocaleLowerCase();
  let visible = 0;
  for (const card of $("document-links").children) {
    card.hidden = !card.querySelector(".document-link").textContent.toLocaleLowerCase().includes(search);
    if (!card.hidden) visible++;
  }
  $("file-empty").hidden = visible > 0;
  $("file-empty").textContent = search ? "No documents match your search." : "No files yet. Upload a document to get started.";
}
$("document-search").addEventListener("input", filterDocuments);

function controls() {
  $("send").disabled = !initialized || busy || uploadBusy;
  $("upload-submit").disabled = !initialized || uploadBusy || !config.uploads_enabled;
  $("attach").disabled = !initialized || uploadBusy || !config.uploads_enabled;
  $("sidebar-upload").disabled = $("attach").disabled;
  $("new-chat").disabled = !initialized || busy || uploadBusy;
  $("ask").setAttribute("aria-busy", String(busy));
}

async function api(path, options = {}) {
  const requestCredential = credential, headers = new Headers(options.headers || {});
  if (requestCredential) headers.set("Authorization", "Bearer " + requestCredential);
  if (!["GET", "HEAD"].includes(options.method || "GET")) headers.set("X-GSME-Session", "1");
  const response = await fetch("/api/" + path, {...options, headers, credentials: "same-origin"});
  if (!response.ok) {
    if (response.status === 401 && credential && credential === requestCredential) {
      credential = ""; sessionEpoch++; clearPrivateState();
    }
    let body; try { body = await response.json(); } catch {}
    const reference = response.headers.get("X-GSME-Request-ID");
    let detail = typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`;
    if (response.status === 404 && detail === "Not Found" && options.method === "DELETE")
      detail = "The running server does not have this delete route. Stop and restart the chatbot, then refresh this page.";
    throw new Error(detail +
      (reference ? ` (Request ${reference})` : ""));
  }
  return response;
}

function message(kind, text) {
  const box = document.createElement("article"), p = document.createElement("p");
  box.className = "message " + kind; p.textContent = text;
  box.setAttribute("aria-label", kind === "user" ? "Your message" : "Muse reply");
  box.append(p); $("messages").append(box); $("welcome").hidden = true;
  if (kind === "user") scrollLatest(true);
  else requestAnimationFrame(() => {
    if (!box.isConnected || !followConversation) return;
    const timeline = $("conversation-scroll");
    // Complete long replies arrive at once. Reveal their beginning instead of
    // jumping past the first paragraphs to the bottom of the answer.
    if (box.offsetHeight > timeline.clientHeight * .75) {
      timeline.scrollTop += box.getBoundingClientRect().top - timeline.getBoundingClientRect().top - 16;
    } else scrollLatest();
  });
  return box;
}

function answerMessage(result) {
  const box = message("assistant", ""), text = box.querySelector("p"), targets = new Map();
  let groups = result.answer_sources || [];
  // Search matches are not citations. Keep them explicitly separate on abstention.
  if (result.answer?.status !== "answered" && result.results?.length) {
    const matches = document.createElement("details"), title = document.createElement("summary");
    title.textContent = "Search matches (not used in an answer)"; matches.append(title);
    const seen = new Set();
    for (const item of result.results) {
      const key = JSON.stringify([item.source_sha256 || item.version_id, item.text]);
      if (seen.has(key)) continue; seen.add(key);
      const p = document.createElement("p"); p.textContent = `${item.source_file}\n${item.text}`; matches.append(p);
    }
    box.append(matches);
  }
  for (const group of groups) {
    const more = document.createElement("details"), summary = document.createElement("summary");
    more.className = "cited-source"; summary.textContent = group.source_file; more.append(summary);
    for (const passage of group.passages) {
      const section = document.createElement("section"), label = document.createElement("p"), quote = document.createElement("p"), open = document.createElement("button");
      label.className = "citation";
      label.textContent = passage.references.map(r => `[${r.source_id}]${r.page_start ? " · page " + r.page_start : ""}`).join(" / ");
      quote.textContent = passage.quote; open.type = "button"; open.textContent = "Open document";
      open.onclick = () => original(passage.references[0].version_id).catch(showError);
      section.append(label, quote, open); more.append(section);
      for (const reference of passage.references) targets.set(reference.source_id, {more, section, summary, filename: group.source_file});
    }
    box.append(more);
  }
  const answer = result.answer?.text || "No supported answer is available.";
  let cursor = 0;
  for (const match of answer.matchAll(/\[(E\d+)\]/g)) {
    text.append(document.createTextNode(answer.slice(cursor, match.index)));
    const target = targets.get(match[1]);
    if (target) {
      const button = document.createElement("button"); button.type = "button"; button.className = "evidence-reference";
      button.textContent = match[0]; button.title = "Show supporting passage in " + target.filename;
      button.onclick = () => { target.more.open = true; target.section.scrollIntoView({block:"nearest"}); target.summary.focus(); };
      text.append(button);
    } else text.append(document.createTextNode(match[0]));
    cursor = match.index + match[0].length;
  }
  text.append(document.createTextNode(answer.slice(cursor)));
  if (result.request_id) {
    const diagnostics = document.createElement("details"), title = document.createElement("summary"), p = document.createElement("p");
    diagnostics.className = "request-details"; title.textContent = "Request details";
    p.textContent = `Request: ${result.request_id}\nSearch: ${result.status}\nAnswer: ${result.answer?.status || "not requested"}\nReason: ${result.answer?.reason || result.answer?.failure_reason || "see stage logs"}\nPassages reranked: ${result.rerank_input_count || 0}\nModel: ${result.answer?.model || "not used"}\nLocal backend logs: data/logs/console.log`;
    diagnostics.append(title, p); box.append(diagnostics);
  }
  return box;
}

function clearPrivateState() {
  clearTimeout(libraryTimer);
  closePreview();
  $("messages").replaceChildren(); $("documents").replaceChildren();
  $("welcome").hidden = false; $("document-search").value = "";
  $("question").value = ""; resizeComposer(); $("conversation-scroll").scrollTop = 0;
  $("document-count").textContent = "0"; $("jump-latest").hidden = true; followConversation = true;
  $("scope").replaceChildren(new Option("All files", ""));
  $("document-links").replaceChildren();
  $("signout").hidden = true;
  $("signin").hidden = config.guest_uploads_enabled || !config.private_uploads_enabled;
  $("googleButton").replaceChildren(); $("file").value = "";
}
function showError(error) { $("notice").textContent = error.message; $("notice").classList.add("error"); }

async function original(version) {
  closePreview();
  const epoch = sessionEpoch, request = previewEpoch;
  const data = await (await api("preview/" + encodeURIComponent(version))).json();
  if (epoch !== sessionEpoch || request !== previewEpoch) return;
  $("viewer-title").textContent = data.filename;
  $("viewer-note").textContent = data.notice || "Original document. Close this preview to return to the chat.";
  if (data.kind === "text") {
    $("viewer-text").textContent = data.text; $("viewer-text").hidden = false;
  } else {
    const response = await api("source/" + encodeURIComponent(version));
    const blob = await response.blob();
    if (epoch !== sessionEpoch || request !== previewEpoch) return;
    previewUrl = URL.createObjectURL(blob);
    const target = data.kind === "pdf" ? $("viewer-pdf") : $("viewer-image");
    target.src = previewUrl; target.hidden = false;
  }
  $("document-viewer").showModal();
}

function closePreview() {
  previewEpoch++;
  $("document-viewer").close();
  $("viewer-pdf").removeAttribute("src"); $("viewer-image").removeAttribute("src");
  for (const id of ["viewer-pdf", "viewer-image", "viewer-text"]) $(id).hidden = true;
  $("viewer-text").textContent = ""; $("viewer-title").textContent = "Document"; $("viewer-note").textContent = "";
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = "";
}
$("viewer-close").onclick = closePreview;
$("document-viewer").addEventListener("cancel", event => { event.preventDefault(); closePreview(); });

async function library() {
  clearTimeout(libraryTimer);
  const epoch = sessionEpoch, result = await (await api("documents")).json();
  if (epoch !== sessionEpoch) return;
  const selected = $("scope").value;
  $("documents").replaceChildren(); $("document-links").replaceChildren();
  const names = result.documents.map(doc => doc.source_file);
  $("document-count").textContent = names.length;
  $("scope").replaceChildren(new Option("All files", ""));
  for (const doc of result.documents) {
    $("scope").append(new Option(doc.source_file, doc.document_id));
    // Use the authorized source endpoint, not a filesystem URL or public link.
    const fileLink = document.createElement("button");
    fileLink.type = "button"; fileLink.className = "document-link";
    fileLink.textContent = doc.source_file;
    fileLink.setAttribute("aria-label", doc.source_file);
    fileLink.title = "Open original: " + doc.source_file;
    fileLink.onclick = () => { closeNavigation(); original(doc.version_id).catch(showError); };
    const card = document.createElement("div"), progress = document.createElement("span");
    card.className = "document-card";
    const stateLabel = {indexed:"Ready to search", approved:"Preparing search", indexing:"Creating embeddings", queued:"Waiting to parse", parsing:"Reading document", review_needed:"Extraction needs attention", failed:"Processing failed"}[doc.state] || doc.state;
    progress.className = "file-progress";
    progress.textContent = doc.processing ? `Processing · ${doc.state === "review_needed" ? "Checking extracted text" : stateLabel}` :
      doc.indexed ? "Ready to chat" :
      doc.accepted ? "Ready to index · Use Retry" : stateLabel;
    if (!doc.owned) {
      progress.textContent = "Shared library · " + progress.textContent;
      progress.title = "Shared library files are managed by the operator. Retry and Delete apply to your own uploads only.";
    }
    if (doc.deletion_protected) progress.title = "Required document: protected from deletion.";
    if (doc.processing) progress.classList.add("processing");
    card.append(fileLink, progress);
    if (doc.owned) {
      const actions = document.createElement("div"); actions.className = "file-actions";
      if (doc.state !== "indexed") {
        const retry = document.createElement("button"); retry.type = "button";
        retry.textContent = doc.processing ? "Processing…" : "Retry";
        retry.disabled = doc.processing || uploadBusy;
        retry.setAttribute("aria-label", "Retry " + doc.source_file);
        retry.onclick = () => resumeAndWatch(doc.version_id).catch(showError); actions.append(retry);
      }
      if (doc.visibility === "private" && !doc.deletion_protected) {
        const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Delete";
        remove.disabled = !config.document_actions?.includes("delete_own_upload");
        if (remove.disabled) remove.title = "Restart the chatbot server to enable upload deletion.";
        remove.setAttribute("aria-label", "Delete " + doc.source_file);
        remove.onclick = async () => {
          if (!confirm(`Remove ${doc.source_file} from chat and search? Original and audit evidence are retained for recovery.`)) return;
          remove.disabled = true;
          try {
            const epoch = sessionEpoch;
            const result = await (await api(`documents/${encodeURIComponent(doc.version_id)}`, {method:"DELETE"})).json();
            if (epoch !== sessionEpoch) return;
            sessionEpoch++; // Discard in-flight replies/previews of removed evidence.
            closePreview(); $("messages").replaceChildren(); $("welcome").hidden = false;
            $("notice").textContent = result.message; await library();
          } catch (error) { showError(error); remove.disabled = false; }
        };
        actions.append(remove);
      }
      card.append(actions);
    }
    $("document-links").append(card);
    const row = document.createElement("div"), label = document.createElement("span"), button = document.createElement("button");
    const state = {indexed:"Indexed", approved:"Text accepted", queued:"Waiting", parsing:"Reading file", review_needed:"Text extracted", failed:"Needs attention"}[doc.state] || doc.state;
    label.textContent = `${doc.source_file} · ${state} · ${doc.owned ? "Your upload" : "Shared document"} `;
    button.type = "button"; button.textContent = "Open original";
    button.onclick = () => original(doc.version_id).catch(showError); row.append(label, button);
    $("documents").append(row);
  }
  if (!result.documents.length) $("documents").textContent = "No documents are visible in this server workspace. Use + to upload, or restart the chatbot if existing files were just shared.";
  if ([...$("scope").options].some(option => option.value === selected)) $("scope").value = selected;
  filterDocuments();
  if (!uploadBusy && result.documents.some(doc => doc.processing))
    libraryTimer = setTimeout(() => library().catch(showError), 4000);
}

async function watch(version, epoch) {
  let automaticResumes = 0;
  // Large documents use bounded indexing batches. Held evidence is never
  // auto-approved just to make the progress indicator finish.
  for (let attempt = 0; attempt < 300 && epoch === sessionEpoch; attempt++) {
    const status = await (await api(`documents/${encodeURIComponent(version)}/status`)).json();
    if (epoch !== sessionEpoch) return;
    await library();
    if (status.state === "indexed" || (status.document_class === "schedule" && status.state === "approved")) {
      $("notice").textContent = "Ready to chat. Ask a question below.";
      return;
    }
    if (status.state === "failed") throw new Error("The file could not be parsed. Check the server log or upload another file.");
    if (!status.processing && status.state === "review_needed" && status.accepted === 0) {
      const reasons = (status.held_reasons || []).map(item => item.code.replaceAll("_", " ")).join(", ");
      $("notice").textContent = `Parsed ${status.chunks} passages; ${status.held} need source review${reasons ? " (" + reasons + ")" : ""}. No trusted text is searchable yet. Use Retry to apply processing fixes; uncertain content is never auto-approved.`; return;
    }
    if (!status.processing && status.index_jobs.failed) throw new Error(status.retry_after_seconds ?
      `Cohere rate limit: wait ${status.retry_after_seconds} seconds, then Retry. ${status.index_jobs.indexed || 0} already indexed passages remain searchable.` :
      "Embedding did not finish. Check provider settings or connection, then use Retry beside the file.");
    if (!status.processing && status.index_jobs.queued && automaticResumes < 3) {
      await api(`documents/${encodeURIComponent(version)}/resume`, {method: "POST"}); automaticResumes++;
    } else if (!status.processing && status.accepted > 0 && !Object.keys(status.index_jobs).length) {
      throw new Error("Text was parsed, but indexing could not start. Check provider settings/server logs, then use Retry beside the file.");
    }
    $("notice").textContent = ["queued", "parsing"].includes(status.state) ? "Processing: reading your document… Large PDFs may take several minutes." : "Processing: preparing your document for search…";
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  if (epoch === sessionEpoch) $("notice").textContent = "Still processing. Check the file status in the sidebar; use Retry if processing stops.";
}

async function resumeAndWatch(version) {
  if (uploadBusy) return;
  const epoch = sessionEpoch; uploadBusy = true; controls();
  try { $("notice").textContent = "Retrying document processing…"; await api(`documents/${encodeURIComponent(version)}/resume`, {method: "POST"}); await watch(version, epoch); }
  finally { uploadBusy = false; controls(); }
}

function schedule(box, result) {
  if (result.status === "clarification_needed") { message("assistant", result.message); return; }
  const wrapper = document.createElement("div"), table = document.createElement("table"), head = document.createElement("tr");
  wrapper.className = "table-wrap";
  const columns = ["schedule_kind", "technology_nm", "flavor", "trial_deadline", "final_deadline", "tapeout_date", "estimated_ship_date"];
  for (const key of columns) { const th = document.createElement("th"); th.textContent = key.replaceAll("_", " "); head.append(th); }
  table.append(head);
  for (const row of result.results) {
    const tr = document.createElement("tr");
    for (const key of columns) { const td = document.createElement("td"); td.textContent = row[key]; tr.append(td); }
    table.append(tr);
  }
  wrapper.append(table); box.append(wrapper);
  const note = document.createElement("p"); note.className = "citation";
  note.textContent = `${result.total} imported records · ${result.timezone} · as of ${result.as_of}${result.has_more ? " · Refine your question for more records" : ""}. Not a live capacity confirmation.`;
  box.append(note);
}

$("refresh").onclick = () => library().catch(showError);
$("attach").onclick = () => $("file").click();
$("sidebar-upload").onclick = () => $("file").click();
$("file").onchange = () => { if ($("file").files.length) $("upload").requestSubmit(); };
$("question").addEventListener("keydown", event => {
  // IME composition and Shift+Enter must keep their normal editing behavior.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (initialized && !busy && !uploadBusy) $("ask").requestSubmit();
  }
});
$("ask").onsubmit = async event => {
  event.preventDefault(); if (!initialized || busy || uploadBusy) return;
  const epoch = sessionEpoch, question = $("question").value.trim(); if (!question) return;
  message("user", question); $("question").value = ""; resizeComposer(); busy = true; controls();
  $("notice").classList.remove("error");
  const pending = message("assistant", "Searching your documents…"); pending.classList.add("thinking");
  $("notice").textContent = "Searching your accessible evidence…";
  try {
    const body = {question}; if ($("scope").value) body.document_id = $("scope").value;
    const response = await api("chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const result = await response.json(); result.request_id ||= response.headers.get("X-GSME-Request-ID");
    if (epoch !== sessionEpoch) return;
    pending.remove();
    if (result.route === "schedules") schedule(message("assistant", result.status === "no_matching_rows" ? "No matching schedule records." : "Schedule results"), result);
    else {
      const box = answerMessage(result);
      if (result.schedule_search) schedule(box, result.schedule_search);
    }
    $("notice").textContent = "Answers use accessible, accepted sources. Check the citations.";
  } catch (error) { if (epoch === sessionEpoch) { message("assistant", error.message); showError(error); } }
  finally { pending.remove(); busy = false; controls(); }
};

$("upload").onsubmit = async event => {
  event.preventDefault(); if (!initialized || uploadBusy) return;
  if (!credential && !config.guest_uploads_enabled) { showError(new Error("Sign in to enable document uploads.")); return; }
  const file = $("file").files[0]; if (!file) return;
  if (file.size > 20 * 1024 * 1024) { showError(new Error("Choose a file no larger than 20 MiB.")); return; }
  const epoch = sessionEpoch, form = new FormData(); form.append("file", file);
  uploadBusy = true; controls(); $("notice").textContent = "Uploading to your workspace…";
  $("notice").classList.remove("error"); closeNavigation();
  try {
    const result = await (await api("upload", {method: "POST", body: form})).json(); if (epoch !== sessionEpoch) return;
    $("file").value = ""; await library();
    const doc = await (await api(`documents/${encodeURIComponent(result.version_id)}/status`)).json(); if (epoch !== sessionEpoch) return;
    $("scope").value = doc.document_id; await watch(result.version_id, epoch);
  } catch (error) { if (epoch === sessionEpoch) showError(error); }
  finally { uploadBusy = false; controls(); }
};

$("signout").onclick = () => {
  credential = ""; sessionEpoch++; clearPrivateState();
  $("notice").textContent = "Signed out. Private conversation and document lists were cleared.";
  library().catch(showError);
};
$("signin").onclick = () => {
  if (!config.private_uploads_enabled) return;
  const initialize = () => google.accounts.id.initialize({client_id: config.google_client_id, callback: async response => {
    credential = response.credential; sessionEpoch++; clearPrivateState();
    try {
      await library(); $("signout").hidden = false; $("signin").hidden = true;
      $("notice").textContent = "Signed in. Uploaded documents are visible only to you.";
    } catch (error) { credential = ""; sessionEpoch++; clearPrivateState(); showError(error); }
  }});
  if (window.google) { initialize(); google.accounts.id.renderButton($("googleButton"), {type: "standard", theme: "outline"}); }
  else {
    const script = document.createElement("script"); script.src = "https://accounts.google.com/gsi/client";
    script.onload = () => { initialize(); google.accounts.id.renderButton($("googleButton"), {type: "standard", theme: "outline"}); };
    script.onerror = () => showError(new Error("Google sign-in could not load.")); document.head.append(script);
  }
};

(async () => {
  try {
    config = await (await api("config")).json();
    if (config.guest_uploads_enabled) {
      await api("session", {method: "POST"});
      $("workspace-note").textContent = "No sign-in needed. Your uploads are private to this browser workspace. Access expires after seven days or when cookies are cleared; files are not automatically deleted. Questions search documents independently.";
    }
    clearPrivateState(); await library(); initialized = true; controls();
    $("notice").textContent = "Upload a file or ask a question about available FAQs.";
  } catch (error) { showError(error); }
})();
