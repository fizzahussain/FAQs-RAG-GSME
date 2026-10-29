"""Static browser UI for the loopback-only evidence console."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GSME Evidence Console</title>
<style>
body{font:15px system-ui,Arial,sans-serif;max-width:1200px;margin:24px auto;padding:0 18px;color:#17202a;background:#f8fafc}
h1{margin-bottom:4px}h2{font-size:19px;margin-top:0}section{background:white;border:1px solid #dbe1e7;border-radius:10px;margin:18px 0;padding:18px}
label{display:block;margin:9px 0 4px;font-weight:600}input,select,textarea,button{font:inherit;padding:8px;border:1px solid #9ba8b4;border-radius:5px;box-sizing:border-box}
input[type=text],input[type=password],textarea{width:100%}button{cursor:pointer;background:#144b73;color:white;border:0;margin:5px 5px 5px 0}button:disabled{opacity:.5}
pre{white-space:pre-wrap;overflow:auto;background:#eef2f5;padding:12px;border-radius:5px;max-height:320px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}.muted{color:#52616f}.warning{background:#fff1cf;padding:10px;border-radius:5px}
.chunk{border-top:1px solid #dbe1e7;padding:12px 0}.chunk pre{max-height:240px}.state{font-weight:600}#message{position:sticky;top:0;background:#e5f1f8;padding:10px;border-radius:5px;z-index:2}
</style></head><body>
<h1>GSME Evidence Console</h1><p class="muted">Local operator workflow. PostgreSQL controls review; Qdrant holds derived embeddings. This is not a public or end-user search app.</p>
<div id="message">Enter the local token to connect.</div>
<section><h2>1. Connect</h2><div class="grid"><div><label>Local review token</label><input id="token" type="password" autocomplete="off"></div>
<div><label>Tenant</label><input id="tenant" type="text" value="local-test"></div><div><label>Authorized reviewer ID</label><input id="reviewer" type="text" placeholder="Your ID from .env"></div></div>
<button id="connect">Check connection</button><pre id="health"></pre></section>
<section><h2>2. Submit and parse</h2><p class="muted">The uploaded source is copied to private local storage. It begins pending and is never automatically sent to Cohere.</p>
<div class="grid"><div><label>File</label><input id="file" type="file"></div><div><label>Schedule kind (CSV only)</label><select id="schedule"><option value="">Auto from filename</option><option value="full">Full</option><option value="shared">Shared</option></select></div></div>
<label>Existing document ID (only when submitting a new version)</label><input id="existingDoc" type="text">
<button id="submit">Submit</button><button id="ingest">Parse submitted job</button>
<div class="grid"><div><label>Job ID</label><input id="job" type="text"></div><div><label>Version ID</label><input id="version" type="text"></div><div><label>Document ID</label><input id="document" type="text"></div></div>
<button id="jobStatus">Job status</button><button id="versions">List recent versions</button><pre id="jobs"></pre></section>
<section><h2>3. Inspect source and review each chunk</h2><p class="warning">Compare every proposed claim with the original. OCR, inferred tables, charts and formulas need special care. A checkbox is not a substitute for source verification.</p>
<button id="inspect">Load version and index status</button><button id="source">Open original source</button><button id="markdown">View normalized Markdown</button><button id="manifest">View warnings and diagnostics</button>
<pre id="versionInfo"></pre><pre id="artifact"></pre>
<label>Review rationale (required)</label><textarea id="rationale" rows="2" placeholder="Describe the original page/paragraph and what you verified"></textarea>
<label><input id="verified" type="checkbox"> I checked this particular chunk against the original source</label>
<div id="chunks"></div></section>
<section><h2>4. Publish and index approved evidence</h2><p class="muted">Publish makes reviewed evidence current. Index processes one bounded batch each click; repeat until indexed is zero. Fake vectors test plumbing only.</p>
<label>Provider</label><select id="provider"><option value="fake">Fake test vectors (no external transfer)</option><option value="cohere">Cohere Pro/Fast (real embeddings)</option></select>
<label><input id="external" type="checkbox"> I authorize this version's approved text to be sent to Cohere</label>
<button id="publish">Publish approved version</button><button id="index">Index one batch</button><button id="reconcile">Remove stale points</button><pre id="indexInfo"></pre></section>
<section><h2>5. Test retrieval</h2><p class="muted">Returns approved evidence and citations, not a generated answer. Fake-vector rankings do not measure semantic quality.</p>
<label>Question</label><input id="question" type="text" placeholder="Ask a question grounded in approved source content">
<button id="search">Search</button><pre id="results"></pre></section>
<script>
const el=id=>document.getElementById(id), value=id=>el(id).value.trim();
function message(s){el('message').textContent=s}
async function api(path,opts={}){
  const headers={'X-GSME-Token':value('token')};
  if(opts.body && !(opts.body instanceof FormData)) headers['Content-Type']='application/json';
  const response=await fetch('/api/'+path,{...opts,headers});
  if(!response.ok){let err;try{err=await response.json()}catch{err={detail:response.statusText}}throw new Error(err.detail||response.statusText)}
  return response;
}
async function json(path,body){const opts=body?{method:'POST',body:JSON.stringify(body)}:{};return (await api(path,opts)).json()}
async function run(fn){try{message('Working…');await fn();message('Done.')}catch(e){message('Error: '+e.message)}}
function ids(){return {tenant:value('tenant'),version_id:value('version'),document_id:value('document'),provider:value('provider')}}
function show(id,data){el(id).textContent=JSON.stringify(data,null,2)}
el('connect').onclick=()=>run(async()=>show('health',await json('health')));
el('submit').onclick=()=>run(async()=>{
 const file=el('file').files[0];if(!file)throw new Error('Select a file');
 const data=new FormData();data.append('file',file);data.append('tenant',value('tenant'));
 if(value('existingDoc'))data.append('document_id',value('existingDoc'));
 if(value('schedule'))data.append('schedule_kind',value('schedule'));
 const out=await (await api('submit',{method:'POST',body:data})).json();show('jobs',out);
 el('job').value=out.job_id||'';el('version').value=out.version_id;el('document').value=out.document_id;
});
el('ingest').onclick=()=>run(async()=>show('jobs',await json('ingest',{tenant:value('tenant'),job_id:value('job')})));
el('jobStatus').onclick=()=>run(async()=>show('jobs',await json('status/'+encodeURIComponent(value('job'))+'?tenant='+encodeURIComponent(value('tenant')))));
el('versions').onclick=()=>run(async()=>show('jobs',await json('versions?tenant='+encodeURIComponent(value('tenant')))));
el('inspect').onclick=()=>run(async()=>{
 const out=await json('inspect/'+encodeURIComponent(value('version'))+'?tenant='+encodeURIComponent(value('tenant')));
 show('versionInfo',{version:out.version,index_jobs:out.index_jobs,review_manifest_path:out.review_manifest_path});
 el('document').value=out.version.document_id;
 const root=el('chunks');root.replaceChildren();
 for(const chunk of out.chunks){
   const card=document.createElement('div');card.className='chunk';
   const title=document.createElement('div');title.className='state';
   title.textContent=`${chunk.source_chunk_id} | page ${chunk.source_page_start??'?'} | ${chunk.chunk_type} | ${chunk.evidence_status} | review required: ${chunk.review_required} | latest: ${chunk.latest_decision??'none'}`;
   const txt=document.createElement('pre');txt.textContent=chunk.evidence_text;
   const approve=document.createElement('button');approve.textContent='Approve this chunk';
   approve.onclick=()=>review(chunk,'approved');
   const reject=document.createElement('button');reject.textContent='Reject this chunk';
   reject.onclick=()=>review(chunk,'rejected');
   const preview=document.createElement('button');preview.textContent='Show embedding preview';
   preview.onclick=()=>run(async()=>show('indexInfo',await json('embedding/'+encodeURIComponent(chunk.source_chunk_id)+
     '?tenant='+encodeURIComponent(value('tenant'))+'&version_id='+encodeURIComponent(value('version'))+'&provider='+encodeURIComponent(value('provider')))));
   card.append(title,txt,approve,reject,preview);root.append(card);
 }
});
async function review(chunk,decision){await run(async()=>{
 if(decision==='approved'&&!el('verified').checked)throw new Error('Check source verification for this chunk first');
 if(!value('rationale'))throw new Error('Add a source-specific rationale');
 const out=await json('review',{tenant:value('tenant'),version_id:value('version'),chunk_id:chunk.source_chunk_id,
   decision,reviewer:value('reviewer'),rationale:value('rationale'),source_verified:decision==='approved'&&el('verified').checked});
 chunk.evidence_status=decision;chunk.latest_decision=decision;el('verified').checked=false;
 show('indexInfo',out);el('inspect').click();
})}
el('markdown').onclick=()=>run(async()=>{el('artifact').textContent=await(await api('markdown/'+encodeURIComponent(value('version'))+'?tenant='+encodeURIComponent(value('tenant')))).text()});
el('manifest').onclick=()=>run(async()=>show('artifact',await json('manifest/'+encodeURIComponent(value('version'))+'?tenant='+encodeURIComponent(value('tenant')))));
el('source').onclick=()=>run(async()=>{const response=await api('source/'+encodeURIComponent(value('version'))+'?tenant='+encodeURIComponent(value('tenant')));
 const url=URL.createObjectURL(await response.blob());window.open(url,'_blank','noopener');setTimeout(()=>URL.revokeObjectURL(url),120000)});
el('publish').onclick=()=>run(async()=>show('indexInfo',await json('publish',ids())));
el('index').onclick=()=>run(async()=>show('indexInfo',await json('index',{...ids(),confirm_external:el('external').checked})));
el('reconcile').onclick=()=>run(async()=>show('indexInfo',await json('reconcile',ids())));
el('search').onclick=()=>run(async()=>show('results',await json('search',{tenant:value('tenant'),question:value('question'),provider:value('provider')})));
</script></body></html>"""
