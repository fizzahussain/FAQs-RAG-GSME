"use strict";
const $ = id => document.getElementById(id);
let credential = "", config = {}, busy = false, uploadBusy = false, initialized = false;
// Old requests must not repopulate private content after an identity change.
let sessionEpoch = 0;
let previewEpoch = 0, previewUrl = "";
let followConversation = true;
let libraryTimer;
let progressVersion = "";
let activeChat = null, historyEnabled = false, chats = [];
let chatNavigationEpoch = 0;
let chatController = null;
let searchMode = "chat";
let previousScheduleFilters = null;

function resetScheduleSearch() {
  previousScheduleFilters = null;
  for (const id of ["schedule-kind", "schedule-date-field", "schedule-from", "schedule-to"]) $(id).value = "";
  $("schedule-results").replaceChildren();
}
$("reset-schedules").onclick = () => {
  if (busy || uploadBusy) return;
  resetScheduleSearch();
  $("notice").textContent = "Schedule filters cleared. Start a new Full, Shared or both lookup.";
};

function setSearchMode(mode) {
  if (busy || uploadBusy) return;
  searchMode = mode;
  const schedules = mode === "schedules";
  $("mode-chat").setAttribute("aria-pressed", String(!schedules));
  $("mode-schedules").setAttribute("aria-pressed", String(schedules));
  $("schedule-workspace").hidden = !schedules;
  $("schedule-options").hidden = !schedules;
  $("document-scope").hidden = schedules;
  $("messages").hidden = schedules;
  $("welcome").hidden = schedules || $("messages").children.length > 0;
  $("chat-title").textContent = schedules ? "Tapeout schedules" : chats.find(c => c.id === activeChat)?.title || "Document chat";
  $("question").placeholder = schedules ? 'e.g. Show shared 180nm schedules with flavor MS RF G' : "Ask about your documents…";
  $("send").querySelector("span").textContent = schedules ? "Search" : "Send";
  $("search-hint").textContent = schedules ? "Exact source rows · Asia/Karachi · Lookups are not saved in chat history." : "Follow-ups use recent questions. Answers search current sources.";
  $("notice").textContent = schedules ? "Choose Full, Shared or both in your question or the Schedule menu. Upload a schedule CSV/XLSX if none is available." : "Ask about your available documents.";
  $("notice").classList.remove("error");
  $("jump-latest").hidden = true;
  $("conversation-scroll").scrollTop = 0;
  $("question").value = ""; resizeComposer();
}
$("mode-chat").onclick = () => setSearchMode("chat");
$("mode-schedules").onclick = () => setSearchMode("schedules");
let desktopCollapsed = false;
try { desktopCollapsed = localStorage.getItem("gsme-sidebar-collapsed") === "yes"; } catch {}
const mobileNavigation = window.matchMedia("(max-width: 680px)");

function syncNavigation() {
  $("sidebar").close();
  const collapsed = !mobileNavigation.matches && desktopCollapsed;
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  if (!mobileNavigation.matches && !collapsed) $("sidebar").show();
  $("open-sidebar").setAttribute("aria-expanded", String($("sidebar").open));
}
function closeNavigation() { if (mobileNavigation.matches) $("sidebar").close(); }
function toggleDesktop() {
  desktopCollapsed = !desktopCollapsed;
  try { localStorage.setItem("gsme-sidebar-collapsed", desktopCollapsed ? "yes" : "no"); } catch {}
  syncNavigation();
}
$("open-sidebar").onclick = () => {
  if (!mobileNavigation.matches) { toggleDesktop(); return; }
  if ($("sidebar").open) closeNavigation();
  else { $("sidebar").showModal(); $("open-sidebar").setAttribute("aria-expanded", "true"); }
};
$("close-sidebar").onclick = () => {
  if (mobileNavigation.matches) closeNavigation(); else toggleDesktop();
  $("open-sidebar").focus();
};
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
function resetConversation() {
  resetScheduleSearch();
  chatNavigationEpoch++;
  activeChat = null;
  $("chat-title").textContent = "New chat";
  $("messages").replaceChildren(); $("welcome").hidden = false; $("question").value = "";
  $("jump-latest").hidden = true; followConversation = true;
  closePreview(); resizeComposer();
}
$("new-chat").onclick = () => {
  if (busy || uploadBusy) return;
  setSearchMode("chat");
  resetConversation(); renderChats();
  $("notice").textContent = "New chat. Your documents are still available.";
  closePreview(); closeNavigation(); resizeComposer(); $("question").focus();
  requestAnimationFrame(() => { $("conversation-scroll").scrollTop = 0; });
};

function renderChats() {
  $("chat-list").replaceChildren();
  $("history-empty").hidden = chats.length > 0;
  $("history-empty").textContent = historyEnabled ? "Start a chat to save it here." : "Chat history requires a browser workspace or sign-in.";
  for (const chat of chats) {
    const row = document.createElement("div"), open = document.createElement("button");
    row.className = "chat-history-row";
    open.type = "button"; open.className = "chat-history-open"; open.textContent = chat.title;
    open.title = chat.title; open.disabled = !initialized || busy || uploadBusy;
    open.setAttribute("aria-current", String(chat.id === activeChat));
    open.onclick = () => openChat(chat.id).catch(showError);
    row.append(open);
    for (const [label, symbol, action] of [
      ["Rename", "✎", async epoch => {
        const title = prompt("Chat name", chat.title); if (!title?.trim()) return;
        if (title.trim().length > 120) throw new Error("Chat names must be 120 characters or fewer.");
        await api(`chats/${chat.id}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:title.trim()})});
        if (epoch === sessionEpoch && activeChat === chat.id) $("chat-title").textContent = title.trim();
      }],
      ["Delete", "×", async epoch => {
        if (!confirm("Permanently delete this chat history? Documents will not be deleted.")) return;
        await api(`chats/${chat.id}`, {method:"DELETE"});
        if (epoch === sessionEpoch && activeChat === chat.id) resetConversation();
      }]
    ]) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = symbol;
      button.setAttribute("aria-label", label + " chat " + chat.title); button.title = label + " chat";
      button.disabled = !initialized || busy || uploadBusy;
      button.onclick = async () => {
        const epoch = sessionEpoch;
        try { await action(epoch); if (epoch === sessionEpoch) await loadChats(); }
        catch (error) { if (epoch === sessionEpoch) showError(error); }
      };
      row.append(button);
    }
    $("chat-list").append(row);
  }
}
async function loadChats() {
  const epoch = sessionEpoch, data = await (await api("chats")).json();
  if (epoch !== sessionEpoch) return;
  chats = data.chats; historyEnabled = data.history_enabled; renderChats();
}
async function openChat(id, restoring = false) {
  if (busy || uploadBusy) return;
  const epoch = sessionEpoch, navigation = ++chatNavigationEpoch;
  const data = await (await api(`chats/${id}`)).json();
  if (epoch !== sessionEpoch || navigation !== chatNavigationEpoch || busy || uploadBusy) return;
  setSearchMode("chat"); resetConversation(); activeChat = data.id; $("chat-title").textContent = data.title;
  for (const turn of data.turns) { message("user", turn.question); answerMessage(turn.response); }
  renderChats(); if (!restoring) closeNavigation(); scrollLatest(true);
  $("notice").textContent = "Saved conversation. Follow-ups use recent questions and search current documents.";
}
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
  $("reset-schedules").disabled = !initialized || busy || uploadBusy;
  $("ask").setAttribute("aria-busy", String(busy));
  $("stop-reply").hidden = !busy;
  $("mode-chat").disabled = $("mode-schedules").disabled = !initialized || busy || uploadBusy;
  renderChats();
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
    if (response.status === 404 && path === "chat/stream")
      detail = "Restart the chatbot server and refresh this page to enable streaming replies.";
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
  if (result.query_context) {
    const details = document.createElement("details"), title = document.createElement("summary"), query = document.createElement("p");
    title.textContent = "Question used for search";
    query.textContent = result.query_context.search_question;
    details.append(title, query); box.append(details);
  }
  if (result.retrieval_fallback_used) {
    const notice = document.createElement("p");
    notice.className = "muted";
    notice.textContent = "Using local recovery while the search provider is unavailable. " +
      (result.local_candidate_limit ? `Up to ${result.local_candidate_limit} candidates are locally reranked. ` : "") +
      "Results may differ from the normal service.";
    box.append(notice);
  }
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
    const strategy = document.createElement("p");
    strategy.textContent = result.retrieval_strategy === "hybrid" ? "Retrieval: hybrid (semantic + BM25 keyword search)" :
      result.retrieval_strategy ? "Retrieval: " + result.retrieval_strategy : "Retrieval strategy not recorded for this saved reply.";
    diagnostics.append(strategy);
    p.textContent = `Request: ${result.request_id}\nSearch: ${result.status}\nAnswer: ${result.answer?.status || "not requested"}\nReason: ${result.answer?.reason || result.answer?.failure_reason || "see stage logs"}\nPassages reranked: ${result.rerank_input_count || 0}\nProvider: ${result.answer?.provider || "not used"}\nModel: ${result.answer?.model || "not used"}\nFallback used: ${result.answer?.fallback_used ? "yes" : "no"}\nValidated answer reused: ${result.answer?.cache_hit ? "yes" : "no"}\nEvidence fingerprint: ${result.answer?.context_fingerprint || "not available"}\nAnswer fingerprint: ${result.answer?.answer_fingerprint || "not available"}\nLocal backend logs: data/logs/console.log`;
    diagnostics.append(title, p); box.append(diagnostics);
  }
  return box;
}

function clearPrivateState() {
  resetScheduleSearch();
  chatController?.abort();
  clearTimeout(libraryTimer);
  progressVersion = "";
  activeChat = null; chats = []; historyEnabled = false; renderChats();
  chatNavigationEpoch++;
  $("chat-title").textContent = "Document chat";
  closePreview();
  $("messages").replaceChildren(); $("documents").replaceChildren();
  $("schedule-results").replaceChildren();
  $("welcome").hidden = false; $("document-search").value = "";
  $("question").value = ""; resizeComposer(); $("conversation-scroll").scrollTop = 0;
  $("document-count").textContent = "0"; $("jump-latest").hidden = true; followConversation = true;
  $("scope").replaceChildren(new Option("All files", ""));
  $("document-links").replaceChildren();
  $("signout").hidden = true;
  $("signin").hidden = config.guest_uploads_enabled || !config.private_uploads_enabled;
  $("googleButton").replaceChildren(); $("file").value = "";
  setSearchMode("chat");
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
    if (doc.document_class !== "schedule") $("scope").append(new Option(doc.source_file, doc.document_id));
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
      doc.document_class === "schedule" && doc.state === "approved" ? "Ready for schedule search" : doc.indexed ? "Ready to chat" :
      doc.accepted ? "Text accepted · Not indexed" : stateLabel;
    if (!doc.owned) {
      progress.textContent = "Shared library · " + progress.textContent;
      progress.title = "Shared library files are managed by the operator. Retry and Delete apply to your own uploads only.";
    }
    if (doc.deletion_protected) progress.title = "Required document: protected from deletion.";
    if (doc.automatic_processing && doc.retry_after_seconds > 0)
      progress.textContent = "Waiting for provider · resumes automatically";
    else if (doc.processing_error && !doc.automatic_processing && doc.state !== "indexed")
      progress.textContent = doc.processing_error === "source_review_required" ? "Source review needed" :
        doc.processing_error === "quota_exhausted" ? "Cohere API-call quota exhausted" : "Needs operator attention";
    if (doc.processing_message) progress.title = doc.processing_message;
    if (doc.processing) progress.classList.add("processing");
    card.append(fileLink, progress);
    if (doc.owned) {
      const actions = document.createElement("div"); actions.className = "file-actions";
      if (doc.state !== "indexed" && !(doc.document_class === "schedule" && doc.state === "approved")) {
        const retry = document.createElement("button"); retry.type = "button";
        retry.textContent = doc.processing ? "Processing…" : "Retry";
        retry.disabled = doc.processing || doc.automatic_processing || uploadBusy;
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
  const watched = result.documents.find(doc => doc.version_id === progressVersion);
  if (watched?.state === "indexed") {
    $("notice").textContent = "Ready to chat. Ask a question below.";
    if (!uploadBusy) progressVersion = "";
  } else if (watched?.automatic_processing && watched.retry_after_seconds > 0) {
    $("notice").textContent = `Waiting for the provider; processing resumes automatically in about ${watched.retry_after_seconds} seconds. You can keep chatting.`;
  } else if (watched?.processing_error && !watched.automatic_processing) {
    $("notice").textContent = watched.processing_message || (watched.processing_error === "quota_exhausted" ?
      "Provider quota exhausted. Processing needs the operator to check the API account; repeated retries will not help." :
      watched.processing_error === "source_review_required" ? "The extracted content needs source review before it can be searched." :
      "Automatic processing stopped. The operator should check provider credentials, quota and server logs.");
    progressVersion = "";
  }
  if (!uploadBusy && result.documents.some(doc => doc.processing))
    libraryTimer = setTimeout(() => library().catch(showError), 4000);
}

async function watch(version, epoch) {
  progressVersion = version;
  // Large documents use bounded indexing batches. Held evidence is never
  // auto-approved just to make the progress indicator finish.
  for (let attempt = 0; attempt < 300 && epoch === sessionEpoch; attempt++) {
    const status = await (await api(`documents/${encodeURIComponent(version)}/status`)).json();
    if (epoch !== sessionEpoch) return;
    await library();
    if (status.state === "indexed" || (status.document_class === "schedule" && status.state === "approved")) {
      $("notice").textContent = status.document_class === "schedule" ? "Ready for schedule search. Open Tapeout schedules above." : "Ready to chat. Ask a question below.";
      return;
    }
    if (status.state === "failed") throw new Error("The file could not be parsed. Check the server log or upload another file.");
    if (status.automatic_processing) {
      $("notice").textContent = status.retry_after_seconds > 0 ?
        `Waiting for the provider; processing resumes automatically in about ${status.retry_after_seconds} seconds. You can keep chatting.` :
        "Processing continues automatically in the background. You can keep chatting.";
      return;
    }
    if (!status.automatic_processing && status.processing_error && status.processing_error !== "source_review_required")
      throw new Error(status.processing_message || (status.processing_error === "quota_exhausted" ?
        "Processing is paused because the provider quota is exhausted. The operator must check the API account; repeated retries will not fix this." :
        "Automatic processing could not continue. The operator should check provider credentials, quota and server logs. Previously indexed content is preserved."));
    if (!status.processing && status.state === "review_needed" && status.accepted === 0) {
      const reasons = (status.held_reasons || []).map(item => item.code.replaceAll("_", " ")).join(", ");
      $("notice").textContent = `Parsed ${status.chunks} passages; ${status.held} need source review${reasons ? " (" + reasons + ")" : ""}. No trusted text is searchable yet. Use Retry to apply processing fixes; uncertain content is never auto-approved.`; return;
    }
    if (!status.processing && status.index_jobs.failed) throw new Error(status.retry_after_seconds ?
      `Provider cooldown: ${status.retry_after_seconds} seconds. Indexed content remains searchable. Restart the updated server to restore automatic processing.` :
      "Embedding did not finish. Check provider settings or connection, then use Retry beside the file.");
    if (!status.processing && status.accepted > 0 && !Object.keys(status.index_jobs).length) {
      throw new Error("Text was parsed, but indexing could not start. Check provider settings/server logs, then use Retry beside the file.");
    }
    $("notice").textContent = ["queued", "parsing"].includes(status.state) ? "Processing: reading your document… Large PDFs may take several minutes." : "Processing: preparing your document for search…";
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  if (epoch === sessionEpoch) $("notice").textContent = "Processing continues automatically on the server. You can return later; keep the server running.";
}

async function resumeAndWatch(version) {
  if (uploadBusy) return;
  const epoch = sessionEpoch; uploadBusy = true; controls();
  try { $("notice").textContent = "Retrying document processing…"; await api(`documents/${encodeURIComponent(version)}/resume`, {method: "POST"}); await watch(version, epoch); }
  finally { uploadBusy = false; controls(); if (epoch === sessionEpoch) library().catch(showError); }
}

function schedule(box, result) {
  if (result.status === "clarification_needed") { box.querySelector("p").textContent = result.message; return; }
  if (result.filters) {
    const f = result.filters, active = document.createElement("p"); active.className = "citation";
    const labels = [f.schedule_kind, f.technology_nm ? `${f.technology_nm} nm` : "all technologies",
      f.flavor ? `flavor ${f.flavor}` : "all flavors", f.date_field.replaceAll("_", " "),
      f.date_from ? `from ${f.date_from}` : "", f.date_to ? `through ${f.date_to}` : "",
      f.next_dates ? `next ${f.next_dates} distinct dates` : "", f.deadline_status !== "all" ? f.deadline_status : ""];
    active.textContent = `${result.context_applied ? "Refined search" : "Active filters"}: ${labels.filter(Boolean).join(" · ")}`;
    box.append(active);
  }
  if (!result.results.length) {
    box.querySelector("p").textContent = result.sources?.length ? "No matching schedule records. Try a different date, technology or flavor." : "No schedule source is available in this workspace. Upload your Full/Shared CSV or schedule workbook using +. Existing private or unpublished schedules are not exposed automatically.";
    return;
  }
  const wrapper = document.createElement("div"), table = document.createElement("table"), head = document.createElement("tr");
  wrapper.className = "table-wrap";
  const columns = ["schedule_kind", "technology_nm", "flavor", "trial_deadline", "final_deadline", "tapeout_date", "estimated_ship_date", "source"];
  for (const key of columns) { const th = document.createElement("th"); th.textContent = key.replaceAll("_", " "); head.append(th); }
  table.append(head);
  for (const row of result.results) {
    const tr = document.createElement("tr");
    for (const key of columns) {
      const td = document.createElement("td");
      if (key === "source") {
        td.textContent = `${row.source_file} · ${row.source_sheet || "CSV"} · row ${row.source_row_number}`;
        // A published schedule row does not grant access to the whole workbook.
        if (row.source_url) {
          const open = document.createElement("button"); open.type = "button"; open.textContent = "Open source";
          open.onclick = () => original(row.version_id).catch(showError); td.append(document.createElement("br"), open);
        }
      } else td.textContent = row[key] ?? "—";
      tr.append(td);
    }
    table.append(tr);
  }
  wrapper.append(table); box.append(wrapper);
  const note = document.createElement("p"); note.className = "citation";
  note.textContent = `${result.total} imported records · ${result.timezone} · as of ${result.as_of}${result.has_more ? " · Refine your question for more records" : ""}. Not a live capacity confirmation.`;
  box.append(note);
}

async function runScheduleSearch(question, epoch) {
  const body = {question, limit:100};
  if (previousScheduleFilters) body.previous_filters = previousScheduleFilters;
  for (const [id, key] of [["schedule-kind", "schedule_kind"], ["schedule-date-field", "date_field"],
    ["schedule-from", "date_from"], ["schedule-to", "date_to"]]) {
    if ($(id).value) body[key] = $(id).value;
  }
  const box = document.createElement("article"), title = document.createElement("p");
  box.className = "message assistant"; title.textContent = "Looking up schedule records…"; box.append(title);
  $("schedule-results").replaceChildren(box);
  $("notice").textContent = "Searching imported schedule records…";
  const result = await (await api("schedules/search", {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body), signal:chatController.signal})).json();
  if (epoch !== sessionEpoch) return;
  if (["results", "no_matching_rows"].includes(result.status)) {
    // Carry filters only. Every follow-up rechecks current rows and permissions.
    previousScheduleFilters = Object.fromEntries(
      ["schedule_kind", "technology_nm", "flavor", "date_field", "date_from", "date_to", "deadline_status", "next_dates"]
        .map(key => [key, result.filters[key]]));
  }
  title.textContent = question; schedule(box, result);
  $("notice").textContent = result.status === "clarification_needed" ? "Please clarify your schedule filters. The previous lookup is unchanged." :
    "Schedule lookup complete. You can refine it with ‘Only 180nm’ or ‘What about Shared?’";
}

$("refresh").onclick = () => library().catch(showError);
$("attach").onclick = () => $("file").click();
$("sidebar-upload").onclick = () => $("file").click();
$("file").onchange = () => { if ($("file").files.length) $("upload").requestSubmit(); };
$("stop-reply").onclick = () => chatController?.abort();

async function streamedChat(body, signal, onEvent) {
  const response = await api("chat/stream", {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body), signal});
  if (!response.headers.get("Content-Type")?.includes("text/event-stream") || !response.body)
    throw new Error("The server did not return an answer stream. Restart the updated chatbot.");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const {value, done} = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, {stream:true});
      if (buffer.length > 2_000_000) throw new Error("The reply stream exceeded its size limit.");
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
        const lines = frame.split(/\r?\n/), kind = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).replace(/^ /, "")).join("\n");
        if (!data) continue; // Comments are transport heartbeats, not reply text.
        const payload = JSON.parse(data);
        if (kind === "error") throw new Error((payload.detail || "The answer could not complete.") +
          (payload.request_id ? ` (Request ${payload.request_id})` : ""));
        if (kind === "done") {
          if (!payload.result || typeof payload.result !== "object") throw new Error("Incomplete final answer.");
          payload.result.request_id ||= response.headers.get("X-GSME-Request-ID");
          await reader.cancel(); return payload.result;
        }
        onEvent(kind, payload);
      }
      if (done) throw new Error("The connection ended before the reply was finalized. The incomplete draft was discarded; try again.");
    }
  } finally { reader.releaseLock(); }
}
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
  $("question").value = ""; resizeComposer();
  if (searchMode === "schedules") {
    busy = true; controls(); chatController = new AbortController();
    try { await runScheduleSearch(question, epoch); }
    catch (error) {
      if (epoch === sessionEpoch) {
        $("schedule-results").replaceChildren();
        if (error.name === "AbortError") $("notice").textContent = "Schedule lookup stopped.";
        else showError(error);
      }
    } finally { chatController = null; busy = false; controls(); }
    return;
  }
  message("user", question); busy = true; controls();
  $("notice").classList.remove("error");
  const pending = message("assistant", "Searching your documents…"); pending.classList.add("thinking");
  const draftClaims = []; let interpretation = "";
  chatController = new AbortController();
  $("notice").textContent = "Searching your accessible evidence…";
  try {
    if (historyEnabled && !activeChat) {
      const created = await (await api("chats", {method:"POST"})).json();
      if (epoch !== sessionEpoch) return;
      activeChat = created.id;
    }
    const body = {question}; if ($("scope").value) body.document_id = $("scope").value;
    if (activeChat) body.chat_id = activeChat;
    const result = await streamedChat(body, chatController.signal, (kind, data) => {
      if (epoch !== sessionEpoch) return;
      if (kind === "status") {
        if (data.notice) { interpretation = data.notice; $("notice").textContent = interpretation; }
        if (!draftClaims.length) pending.querySelector("p").textContent = data.stage === "generating" ?
          (data.fallback_used ? "Using the local answer service…" : "Writing an evidence-checked reply…") :
          data.stage === "local_retrieval" ? "Cohere is unavailable. Searching locally with Ollama…" : "Searching your documents…";
      } else if (kind === "claim") {
        if (data.index !== draftClaims.length || typeof data.text !== "string") throw new Error("Reply fragments arrived out of order.");
        draftClaims.push(data.text); pending.classList.remove("thinking"); pending.classList.add("streaming-draft");
        pending.querySelector("p").textContent = (interpretation ? interpretation + "\n\n" : "") + draftClaims.join("\n\n");
        $("notice").textContent = "Reply arriving · checking the complete answer and citations…";
        scrollLatest();
      } else if (kind === "reset") {
        draftClaims.length = 0; pending.classList.remove("streaming-draft"); pending.classList.add("thinking");
        pending.querySelector("p").textContent = "Restarting the reply with the local answer service…";
      }
    });
    if (epoch !== sessionEpoch) return;
    pending.remove();
    if (result.route === "schedules") schedule(message("assistant", result.status === "no_matching_rows" ? "No matching schedule records." : "Schedule results"), result);
    else {
      const box = answerMessage(result);
      if (result.schedule_search) schedule(box, result.schedule_search);
    }
    $("notice").textContent = "Answers use accessible, accepted sources. Check the citations.";
    if (activeChat) { await loadChats(); $("chat-title").textContent = chats.find(c => c.id === activeChat)?.title || "Document chat"; }
  } catch (error) {
    if (epoch === sessionEpoch) {
      if (error.name === "AbortError") { message("assistant", "Reply stopped. No incomplete draft was saved."); $("notice").textContent = "Reply stopped. You can send another question."; }
      else { message("assistant", error.message); showError(error); }
    }
  } finally { pending.remove(); chatController = null; busy = false; controls(); }
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
    if (doc.document_class !== "schedule") $("scope").value = doc.document_id;
    await watch(result.version_id, epoch);
  } catch (error) { if (epoch === sessionEpoch) showError(error); }
  finally { uploadBusy = false; controls(); if (epoch === sessionEpoch) library().catch(showError); }
};

$("signout").onclick = () => {
  credential = ""; sessionEpoch++; clearPrivateState();
  $("notice").textContent = "Signed out. Private conversation and document lists were cleared.";
  Promise.all([library(), loadChats()]).catch(showError);
};
$("signin").onclick = () => {
  if (!config.private_uploads_enabled) return;
  const initialize = () => google.accounts.id.initialize({client_id: config.google_client_id, callback: async response => {
    credential = response.credential; sessionEpoch++; clearPrivateState();
    try {
      await Promise.all([library(), loadChats()]); $("signout").hidden = false; $("signin").hidden = true;
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
      $("workspace-note").textContent = "No sign-in needed. Your uploads are private to this browser workspace. Access expires after seven days or when cookies are cleared; files are not automatically deleted. Follow-ups can use recent questions in the same chat.";
    }
    clearPrivateState(); await Promise.all([library(), loadChats()]);
    if (chats.length) await openChat(chats[0].id, true);
    initialized = true; controls();
    $("notice").textContent = "Upload a file or ask a question about available FAQs.";
  } catch (error) { showError(error); }
})();
