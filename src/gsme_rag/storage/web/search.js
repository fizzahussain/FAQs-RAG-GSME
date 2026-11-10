"use strict";
const byId=id=>document.getElementById(id);
let token="",workspace="",connected=false,busy=false,libraryRows=[],pendingIndex=null;
const provider=()=>byId("searchProvider").value;
function notice(message,error=false){byId("notice").textContent=message;byId("notice").className=error?"error":"";}
function modeChanged(){
  const demo=provider()==="fake";
  byId("uploadConsentLabel").hidden=byId("searchConsentLabel").hidden=byId("answerConsentLabel").hidden=demo;
  byId("answerConsent").checked=false;
  byId("uploadConsent").checked=byId("searchConsent").checked=false;
  pendingIndex=null;byId("continueIndex").hidden=true;
  if(connected) notice(demo?"Local demo: rankings do not measure real relevance.":"Real retrieval: accepted evidence is searched and reranked by Cohere.");
}
function controls(){
  document.querySelectorAll("button,input,select,textarea").forEach(input=>input.disabled=busy);
  for(const id of ["askButton","uploadButton","logsButton"]) byId(id).disabled=busy || !connected;
}
async function run(action){if(busy)return;busy=true;controls();try{await action();}catch(error){notice(error.message,true);}finally{busy=false;controls();}}
async function request(path,options={}){
  const headers={"X-GSME-Token":token};
  if(options.body && !(options.body instanceof FormData)) headers["Content-Type"]="application/json";
  const response=await fetch("/api/"+path,{...options,headers});
  if(!response.ok){let detail=response.statusText;try{detail=(await response.json()).detail;}catch(_){/* No JSON error body. */}
    const id=response.headers.get("X-GSME-Request-ID");throw new Error((typeof detail==="string"?detail:JSON.stringify(detail))+(id?` [request ${id}]`:""));}
  return response;
}
async function json(path,body){return(await request(path,body?{method:"POST",body:JSON.stringify(body)}:{})).json();}
async function finishIndexing(body){
  let result;
  // Continue bounded server batches for this same explicitly submitted version.
  for(let attempt=0;attempt<5;attempt++){
    result=await json("ingest-ready",body);
    byId("uploadStatus").textContent=`${result.accepted} accepted passages; ${result.held} held aside; ${result.remaining || 0} index jobs waiting.`;
    if(result.state!=="index_pending" || !result.indexed)break;
  }
  pendingIndex=["index_pending","index_failed"].includes(result.state)?body:null;
  byId("continueIndex").hidden=!pendingIndex;
  return result;
}
async function refreshLibrary(){
  const rows=await json("library?"+new URLSearchParams({tenant:workspace,provider:provider()}));
  libraryRows=rows;
  const list=byId("documentList"),filter=byId("sourceFilter"),selected=filter.value;list.replaceChildren();filter.replaceChildren(new Option("All documents",""));
  const seen=new Set(),readyByDocument=new Map();
  for(const item of rows)if(item.is_current)readyByDocument.set(item.document_id,item.indexed_chunks);
  for(const item of rows){
    if(item.is_current && item.indexed_chunks>0) filter.append(new Option(item.source_file,item.document_id));
    if(seen.has(item.document_id))continue;seen.add(item.document_id);
    const row=document.createElement("div");row.className="document-row";
    const title=document.createElement("strong");title.textContent=item.source_file;
    const state=document.createElement("span");state.className="quiet";
    state.textContent=item.document_class==="schedule"?"Stored as exact schedule rows · use advanced schedule search":`${readyByDocument.get(item.document_id) || 0} searchable passages · ${item.held_chunks} held aside${item.state==="failed"?" · ingestion failed":""}`;
    row.append(title,state);list.append(row);
  }
  if(!rows.length)list.textContent="No documents yet. Add your first file above.";
  if(Array.from(filter.options).some(option=>option.value===selected))filter.value=selected;
}
byId("loginForm").onsubmit=event=>{event.preventDefault();run(async()=>{
  token=byId("accessToken").value.trim();workspace=byId("workspace").value.trim();
  if(!token || !workspace)throw new Error("Enter your local token and workspace.");
  const health=await json("health");await refreshLibrary();connected=true;
  byId("answerConsentLabel").lastChild.textContent=` Also generate a cited answer using ${health.answer_provider==="gemini"?"Google Gemini":"Cohere Command"}, with local Ollama fallback if unavailable`;
  byId("login").hidden=true;byId("ask").hidden=false;modeChanged();
  if(provider()==="cohere" && health.cohere_rerank_missing_settings?.length)notice("Search setup incomplete: "+health.cohere_rerank_missing_settings.join(", ")+". Update .env and restart.",true);
  byId("askQuestion").focus();
});};
function documents(open){byId("documents").hidden=!open;byId("documentsToggle").setAttribute("aria-expanded",String(open));}
byId("documentsToggle").onclick=()=>documents(byId("documents").hidden);
byId("closeDocuments").onclick=()=>documents(false);
byId("uploadForm").onsubmit=event=>{event.preventDefault();run(async()=>{
  const file=byId("documentFile").files[0];if(!file)throw new Error("Choose a document first.");
  if(file.size>200*1024*1024)throw new Error("The document exceeds the 200 MiB upload limit.");
  const consent=byId("uploadConsent").checked;
  if(provider()==="cohere" && !consent && !file.name.toLowerCase().endsWith(".csv"))throw new Error("Allow this document's accepted text to be embedded by Cohere, or select local demo in Settings.");
  byId("uploadStatus").textContent="Adding document and parsing its contents…";
  const form=new FormData();form.append("file",file);form.append("tenant",workspace);
  // An identical re-upload reuses its logical document instead of duplicating hits.
  const digest=await crypto.subtle.digest("SHA-256",await file.arrayBuffer());
  const hash=Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,"0")).join("");
  const duplicate=libraryRows.find(item=>item.source_file===file.name && item.source_sha256===hash);
  if(duplicate)form.append("document_id",duplicate.document_id);
  if(byId("scheduleKind").value)form.append("schedule_kind",byId("scheduleKind").value);
  const submitted=await(await request("submit",{method:"POST",body:form})).json();
  if(submitted.job_id && (!submitted.no_op || submitted.state==="queued")){
    const parsed=await json("ingest",{tenant:workspace,job_id:submitted.job_id});
    if(parsed.state==="failed")throw new Error("Parsing failed. Open Settings → Recent logs for the failing stage.");
  }
  const details=await json("inspect/"+submitted.version_id+"?"+new URLSearchParams({tenant:workspace,limit:"1"}));
  if(details.version.document_class==="schedule"){
    byId("uploadStatus").textContent="Schedule stored. Exact schedule search is available in advanced tools.";await refreshLibrary();return;
  }
  byId("uploadStatus").textContent="Storing accepted passages and creating embeddings…";
  const result=await finishIndexing({tenant:workspace,version_id:submitted.version_id,document_id:submitted.document_id,provider:provider(),confirm_external:consent});
  await refreshLibrary();
  const summary=`${result.accepted} accepted passages; ${result.held} held aside.`;
  byId("uploadStatus").textContent=summary;
  if(result.state==="searchable"){notice("Document ready to search. "+summary);documents(false);byId("askQuestion").focus();}
  else if(result.state==="held_for_review")notice("Document stored, but no passages are ready for search. Extraction issues are available in advanced tools.",true);
  else notice(summary+` ${result.failed} indexing failures; ${result.remaining} jobs waiting. See Recent logs.`,true);
});};
byId("continueIndex").onclick=()=>run(async()=>{
  if(!pendingIndex || pendingIndex.provider!==provider())throw new Error("Select the original indexing mode before continuing.");
  const result=await finishIndexing(pendingIndex);await refreshLibrary();
  notice(result.state==="searchable"?"Document ready to search.":`${result.remaining || 0} index jobs still waiting; see Recent logs.`,result.state!=="searchable");
});
function answerExtract(part,item){
  // Remove only a known FAQ display wrapper, never technical content or values.
  if(item.faq_question && part.text.includes("\n\nAnswer:\n"))return part.text.split("\n\nAnswer:\n").slice(1).join("\n\nAnswer:\n").trim();
  if(item.faq_question){
    const blocks=part.text.split(/\n\s*\n/),known=new Set([item.faq_question,...(item.heading_path || [])]);
    const plain=value=>value.replace(/^#+\s*/,"").replace(/^(\*\*|__)(.*)\1$/,"$2").trim();
    while(blocks.length>1 && blocks[0].split("\n").every(line=>known.has(plain(line))))blocks.shift();
    return blocks.join("\n\n");
  }
  return part.text;
}
function scheduleTable(rows){
  const table=document.createElement("table"),head=document.createElement("tr"),wrapper=document.createElement("div");wrapper.style.overflowX="auto";table.style.width="100%";
  const columns=[["technology_nm","Technology (nm)"],["flavor","Flavor"],["tapeout_date","Tapeout"],["final_deadline","Final deadline"],["estimated_ship_date","Estimated ship"]];
  for(const [,label] of columns){const cell=document.createElement("th");cell.textContent=label;head.append(cell);}table.append(head);
  for(const row of rows){const tr=document.createElement("tr");for(const [key] of columns){const cell=document.createElement("td");cell.textContent=row[key] ?? "—";tr.append(cell);}table.append(tr);}wrapper.append(table);return wrapper;
}
byId("askForm").onsubmit=event=>{event.preventDefault();run(async()=>{
  const question=byId("askQuestion").value.trim();if(!question)throw new Error("Enter a question.");
  const confirmed=byId("searchConsent").checked;
  if(provider()==="cohere" && !confirmed)throw new Error("Allow this question and matching passages to be reranked by Cohere.");
  byId("matches").replaceChildren();byId("searchStatus").textContent="Finding sources and reranking matches…";
  byId("counts").textContent="";byId("requestDetails").textContent="";
  const body={tenant:workspace,question,provider:provider(),confirm_external:confirmed,strategy:byId("retrievalStrategy").value,query_mode:byId("queryMode").value,route:byId("autoRoute").checked?"auto":"documents",candidate_count:30,top_k:5,schedule_limit:5};
  body.generate_answer=body.confirm_answer_external=provider()==="cohere" && byId("answerConsent").checked;
  if(byId("sourceFilter").value)body.document_id=byId("sourceFilter").value;
  let result;try{result=await json("retrieve",body);}catch(error){byId("searchStatus").textContent="Search failed. No unreranked fallback was returned.";throw error;}
  if(result.status==="clarification_needed"){
    byId("searchStatus").textContent=result.message;notice(result.message);return;
  }
  if(result.route==="schedules"){
    byId("matches").append(scheduleTable(result.results));byId("searchStatus").textContent=`${result.results.length} exact schedule rows${result.has_more?" · more rows available; refine in advanced tools":""}`;return;
  }
  const sent=result.rerank_input_count ?? result.candidate_count;
  byId("searchStatus").textContent=result.results.length?`${sent} passages reranked · ${result.results.length} source matches · ${((result.timings_ms.total_including_answer ?? result.timings_ms.total)/1000).toFixed(1)} s${result.synthetic?" · DEMO ONLY":""}`:
    result.status==="no_eligible_evidence"?"No searchable passages in this workspace/source yet. Add a document, or check its held/indexing status.":"No matching evidence passed the current search checks.";
  byId("counts").textContent=`Requested up to 30 candidates. ${sent} eligible passages were passed to ${result.synthetic?"the local demo reranker":"Cohere reranking"}; ${result.results.length} matches shown. Supporting split parts are added afterward, not reranked again.`;
  byId("requestDetails").textContent=JSON.stringify({request_id:result.request_id,model:result.reranker_model,status:result.status,timings_ms:result.timings_ms,warnings:result.warnings},null,2);
  notice(result.synthetic?"Demo mode tests the workflow, not semantic quality.":"Source matches found. These are extracts, not an AI-generated answer.");
  byId("searchNote").textContent=result.answer?"The answer is model-generated; supporting source extracts remain below. Verify technical claims against the original.":"These are source extracts, not an AI-written answer. A relevant match does not prove a claim.";
  if(result.answer){
    const answer=result.answer,card=document.createElement("article"),heading=document.createElement("h2"),text=document.createElement("p"),meta=document.createElement("p");
    card.className="match";heading.textContent=answer.status==="answered"?"Answer":"Answer status";
    text.textContent=answer.text;meta.className="quiet";
    meta.textContent=`${answer.provider || "No provider call"}${answer.model?" · "+answer.model:""}${answer.fallback_used?" · local fallback":""} · Citation quotes checked; factual support is not independently verified.`;
    card.append(heading,text,meta);
    for(const citation of answer.citations){
      const label=document.createElement("p"),link=document.createElement("a");label.className="citation";
      const match=result.results.find(item=>item.context_parts.some(part=>part.version_id===citation.version_id && part.chunk_id===citation.chunk_id));
      link.href=match?`#source-${match.version_id}-${match.chunk_id}`:"#matches";
      link.textContent=`[${citation.source_id}] ${citation.source_file}${citation.page_start?" · "+(citation.source_file.toLowerCase().endsWith(".pptx")?"slide ":"page ")+citation.page_start:""}${citation.table_row_number?" · row "+citation.table_row_number:""}`;
      link.onclick=()=>{if(match){const target=document.getElementById(`source-${match.version_id}-${match.chunk_id}`);if(target?.parentElement.tagName==="DETAILS")target.parentElement.open=true;}};
      label.append(link);card.append(label);
    }
    byId("matches").append(card);notice(answer.status==="answered"?"Generated answer with supporting source extracts below.":answer.text);
  }
  for(const [index,item] of result.results.entries()){
    const card=document.createElement("article");card.className="match";card.id=`source-${item.version_id}-${item.chunk_id}`;
    const heading=document.createElement("h2");heading.textContent=item.faq_question || item.heading_path?.at(-1) || "Source passage";
    const text=item.context_parts.map(part=>answerExtract(part,item)).join("\n\n");
    const citation=document.createElement("div");citation.className="citation";
    const label=document.createElement("span");label.textContent=item.source_file+(item.page_start?` · ${item.source_file.toLowerCase().endsWith(".pptx")?"slide":"page"} ${item.page_start}${item.page_end && item.page_end!==item.page_start?"–"+item.page_end:""}`:"")+(item.table_row_number?` · row ${item.table_row_number}`:"");
    const original=document.createElement("button");original.className="secondary";original.textContent="Open source";
    original.onclick=()=>run(async()=>{
      const response=await request("source/"+item.version_id+"?"+new URLSearchParams({tenant:workspace}));
      const url=URL.createObjectURL(await response.blob()),link=document.createElement("a");link.href=url;
      if(item.source_file.toLowerCase().endsWith(".pdf")){link.href+=item.page_start?"#page="+item.page_start:"";link.target="_blank";link.rel="noopener noreferrer";}
      else link.download=item.source_file;
      link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
    });
    citation.append(label,original);card.append(heading,window.gsmeEvidence(text),citation);
    if(!item.context_complete){const warning=document.createElement("p");warning.className="quiet";warning.textContent="This split answer is incomplete; check the original before relying on it.";card.append(warning);}
    if(index===0)byId("matches").append(card);
    else {const more=document.createElement("details"),summary=document.createElement("summary");summary.textContent=`Another source: ${item.faq_question || item.source_file}`;more.append(summary,card);byId("matches").append(more);}
  }
  if(result.route==="mixed" && result.schedule_rows?.length){
    const more=document.createElement("details"),summary=document.createElement("summary");summary.textContent=`${result.schedule_rows.length} exact schedule rows${result.schedule_has_more?" · more available in advanced tools":""}`;
    more.append(summary,scheduleTable(result.schedule_rows));byId("matches").append(more);
  }
});};
for(const id of ["askQuestion","sourceFilter","retrievalStrategy","queryMode","autoRoute"]){byId(id).addEventListener(id==="askQuestion"?"input":"change",()=>{byId("searchConsent").checked=byId("answerConsent").checked=false;});}
byId("documentFile").onchange=()=>{byId("uploadConsent").checked=false;byId("uploadStatus").textContent="";};
byId("askQuestion").onkeydown=event=>{if(event.key==="Enter" && !event.shiftKey && !busy){event.preventDefault();byId("askForm").requestSubmit();}};
byId("searchProvider").onchange=()=>run(async()=>{byId("matches").replaceChildren();byId("searchStatus").textContent="";modeChanged();if(connected)await refreshLibrary();});
byId("logsButton").onclick=()=>run(async()=>{const result=await json("logs");byId("logsOutput").textContent=result.entries.join("\n") || result.note || "No recent operations.";});
byId("disconnect").onclick=()=>{
  token="";workspace="";libraryRows=[];connected=false;byId("accessToken").value="";byId("login").hidden=false;byId("ask").hidden=true;documents(false);
  for(const id of ["matches","documentList"])byId(id).replaceChildren();
  for(const id of ["logsOutput","counts","requestDetails","searchStatus","uploadStatus"])byId(id).textContent="";
  byId("askQuestion").value="";byId("sourceFilter").replaceChildren(new Option("All documents",""));modeChanged();notice("Disconnected. Credentials and results were cleared.");controls();
  byId("documentFile").value="";
};
modeChanged();controls();
