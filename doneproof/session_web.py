"""Before/after console workflow; no external action or executor evidence input."""

SESSION_PANEL = """<section class="tablewrap" style="padding:20px;margin-top:24px" id="session-panel">
<h2>Prepare before your agent acts</h2>
<p>Register the outcome and capture trusted baselines first. Your agent performs the action independently.</p>
<label for="session-task">Outcome</label><br><textarea id="session-task" rows="3" style="width:100%" placeholder="Close issue #123 in owner/repository"></textarea>
<p><label><input type="checkbox" id="session-transition" checked> Require every condition to change from false to true</label></p>
<button id="session-prepare">Prepare assurance</button> <button id="session-new">New outcome</button>
<p><label for="session-id">Resume session ID</label> <input id="session-id" class="key"> <button id="session-load">Load session</button></p>
<p id="session-status" role="status" aria-live="polite"></p>
<button id="session-verify" disabled>Verify after external action</button>
<button id="session-reverify" disabled>Re-verify after external repair</button>
<p><label for="session-pin">Independently trusted issuer public key (base64)</label><br><input class="key" id="session-pin" autocomplete="off">
<button id="session-trust">Verify pinned signature</button></p>
<p id="session-signature">Issuer authenticity has not been checked against a trusted pin.</p>
<div id="session-details"></div><p><a href="/connections">Connect an account or review repository permissions</a></p>
</section>"""

SESSION_JS = r"""'use strict';
(() => {
  const el = id => document.getElementById(id);
  let session = null, busy = false, preparation = null, generation = 0;
  function text(tag, value, parent) { const n=document.createElement(tag); n.textContent=value; parent.append(n); return n; }
  function status(value) { el('session-status').textContent=value; }
  async function request(path, method='GET', body, idempotency) {
    const response=await fetch(path,{method,cache:'no-store',redirect:'error',headers:{
      'X-DoneProof-Key':el('key').value,'Content-Type':'application/json',...(idempotency?{'Idempotency-Key':idempotency}:{})},
      ...(body ? {body:JSON.stringify(body)} : {})});
    if(!response.ok) throw new Error(response.status===401?'Enter the workspace API key.':
      response.status===409?'Session or connection changed. Reload before retrying.':'Request unavailable. Resume using the same session ID.');
    return await response.json();
  }
  function render(value) {
    session=value; el('session-id').value=value.id;
    status(value.state === 'READY_FOR_EXECUTION' ? 'Trusted boundary and baselines registered. Your external agent may now act.' : value.state);
    el('session-verify').disabled=busy || value.state!=='READY_FOR_EXECUTION';
    el('session-reverify').disabled=busy || !value.can_reverify || ['VERIFYING','REVERIFYING'].includes(value.state);
    el('session-signature').textContent='Issuer authenticity has not been checked against a trusted pin.';
    const host=el('session-details');host.replaceChildren();
    if(value.compiler?.clarification_requirements?.length) for(const item of value.compiler.clarification_requirements) text('p',item.message,host);
    if(value.trusted_task_started_at) text('p','Trusted task boundary: '+value.trusted_task_started_at,host);
    for(const baseline of value.baselines || []) text('p',`Baseline ${baseline.id}: ${baseline.status} · ${baseline.reason}`,host);
    if(value.assurance) {
      text('h3',value.assurance.level.replaceAll('_',' '),host);
      text('p',value.assurance.explanation,host);
      text('p',`${value.assurance.transitions_proven}/${value.assurance.transition_required} required transitions proven`,host);
      if(value.assurance.lower_assurance_browser) text('strong','Browser UI evidence: lower assurance than authoritative APIs.',host);
    }
    for(const condition of value.receipt?.results || []) {
      const detail=document.createElement('details');host.append(detail);
      text('summary',`${condition.id}: ${condition.status} · ${condition.reason}`,detail);
      text('p','Provider: '+condition.evidence.provider,detail);
      text('p','Observed: '+JSON.stringify(condition.evidence.observed),detail);
      text('p','Fetched: '+condition.evidence.fetched_at,detail);
      if(condition.evidence.source_url) text('p','Source: '+condition.evidence.source_url,detail);
    }
    for(const link of value.lineage || []) text('p',`${link.verdict} · ${link.receipt_id} · previous ${link.previous_receipt_id || 'none'}`,host);
  }
  async function action(operation) {
    if(busy) return;busy=true;const ticket=generation;
    for(const id of ['session-prepare','session-verify','session-reverify','session-load']) el(id).disabled=true;
    try { await operation(ticket); } catch(error) { if(ticket===generation) status(error.message); }
    finally {busy=false;el('session-prepare').disabled=false;el('session-load').disabled=false;
      if(session) {el('session-verify').disabled=session.state!=='READY_FOR_EXECUTION';
        el('session-reverify').disabled=!session.can_reverify || ['VERIFYING','REVERIFYING'].includes(session.state);}}
  }
  async function loadSession(id,ticket) { if(!/^as_[a-f0-9]{32}$/.test(id)) throw new Error('Enter a valid session ID.');
    const value=await request('/v1/assurance/sessions/'+id);if(ticket===generation) render(value);return value; }
  async function follow(value,ticket) {
    const until=Date.now()+120000;let revision=-1;
    while(ticket===generation && ['VERIFYING','REVERIFYING'].includes(value.state) && Date.now()<until) {
      const job=await request(`/v1/jobs/${value.current_job_id}/wait?after_revision=${revision}&timeout=20`);
      revision=job.revision;value=await loadSession(value.id,ticket);
    }
    if(ticket===generation && ['VERIFYING','REVERIFYING'].includes(value.state)) status('Still running. Resume this session to check completion.');
  }
  el('session-prepare').onclick=()=>action(async ticket=>{
    const body={task:el('session-task').value,require_transition:el('session-transition').checked,context:{}};
    const fingerprint=JSON.stringify(body);
    if(preparation && preparation.fingerprint!==fingerprint) throw new Error('Choose New outcome before changing a prepared task.');
    preparation ||= {fingerprint,key:'console:'+crypto.randomUUID()};
    const value=await request('/v1/assurance/sessions','POST',body,preparation.key);if(ticket===generation) render(value);
  });
  el('session-load').onclick=()=>action(async ticket=>follow(await loadSession(el('session-id').value.trim(),ticket),ticket));
  for(const kind of ['verify','reverify']) el('session-'+kind).onclick=()=>action(async ticket=>{
    if(!session) return;const id=session.id, prior=session.receipt?.receipt_id;
    const body=kind==='reverify'?{previous_receipt_id:prior}:{};
    const value=await request(`/v1/assurance/sessions/${id}/${kind}`,'POST',body,`console:${id}:${kind}:${prior || 'initial'}`);
    if(ticket===generation) {render(value);await follow(value,ticket);}
  });
  function reset() {generation++;session=null;preparation=null;el('session-details').replaceChildren();
    el('session-id').value='';status('Prepare before external execution.');el('session-verify').disabled=true;el('session-reverify').disabled=true;}
  el('session-new').onclick=reset;el('key').addEventListener('change',reset);
  const bytes=value=>Uint8Array.from(atob(value),c=>c.charCodeAt(0));
  const hex=value=>Array.from(new Uint8Array(value),x=>x.toString(16).padStart(2,'0')).join('');
  function ordered(value) {return Array.isArray(value)?value.map(ordered):value && typeof value==='object'?
    Object.fromEntries(Object.keys(value).sort().map(k=>[k,ordered(value[k])])):value;}
  el('session-trust').onclick=async()=>{
    const current=session, ticket=generation;
    try {
      if(!current?.receipt || !current.signed_payload_b64) throw new Error();
      const pin=el('session-pin').value.trim(), receipt=current.receipt, payload=bytes(current.signed_payload_b64);
      const unsigned={...receipt};delete unsigned.signature;delete unsigned.receipt_hash;
      if(pin!==receipt.public_key || JSON.stringify(ordered(unsigned))!==JSON.stringify(ordered(JSON.parse(new TextDecoder().decode(payload))))) throw new Error();
      if(hex(await crypto.subtle.digest('SHA-256',payload))!==receipt.receipt_hash ||
         hex(await crypto.subtle.digest('SHA-256',bytes(pin))).slice(0,16)!==receipt.key_id) throw new Error();
      const key=await crypto.subtle.importKey('raw',bytes(pin),{name:'Ed25519'},false,['verify']);
      if(!await crypto.subtle.verify('Ed25519',key,bytes(receipt.signature),payload)) throw new Error();
      if(ticket===generation && current===session) el('session-signature').textContent='Signature verified against the supplied issuer pin.';
    } catch {if(ticket===generation) el('session-signature').textContent='Pinned signature verification failed or is unsupported in this browser.';}
  };
})();"""
