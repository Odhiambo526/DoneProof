"""Assurance-console history. All receipt content is rendered as text."""

RECOVERY_PANEL = """<section id="recovery-panel" class="tablewrap" hidden style="margin-top:24px;padding:20px">
<div style="display:flex;justify-content:space-between;gap:12px"><h2 style="margin:0">Verification history</h2>
<button type="button" id="recovery-close">Close</button></div>
<p class="sub">Review the guidance, make any repair in the external system, then request independent verification.</p>
<p id="recovery-state" role="status" aria-live="polite"></p>
<div style="display:flex;gap:12px;flex-wrap:wrap">
<button type="button" id="recovery-reverify">Re-verify latest receipt</button>
<button type="button" id="recovery-refresh">Refresh history</button>
<button type="button" id="recovery-cancel" hidden>Cancel verification</button></div>
<p><label><input type="checkbox" id="recovery-automatic"> Re-verify when new matching webhook evidence arrives</label></p>
<div id="recovery-history"></div></section>"""

RECOVERY_SCRIPT = r"""'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const panel = byId('recovery-panel'), state = byId('recovery-state');
  let selected = null, data = null, generation = 0, controller = null, pendingKey = null;
  const texts = {reverification_in_progress:'A verification is already running.',
    reverification_limit_reached:'This chain has reached its verification limit.',
    receipt_is_not_chain_head:'A newer receipt is available. Refresh the history.',
    automatic_reverification_requires_configured_exact_webhook_selectors:
      'Automatic verification requires an exact webhook source, event type and object in this workspace.'};
  function credentials() { return byId('key').value; }
  function message(error) { state.textContent = error.message; }
  async function request(path, options = {}, signal) {
    const key = credentials();
    const response = await fetch(path, {...options, signal, cache:'no-store',
      headers:{'Content-Type':'application/json', ...(key ? {'X-DoneProof-Key':key} : {}), ...options.headers}});
    const body = await response.json();
    if (!response.ok) throw new Error(response.status === 401 ? 'Enter a valid workspace API key.' :
      (texts[body.detail] || 'The recovery request could not be completed. Refresh and try again.'));
    return body;
  }
  function element(tag, text, parent) {
    const node = document.createElement(tag); node.textContent = text; parent.appendChild(node); return node;
  }
  function render(history) {
    data = history;
    state.textContent = `${history.attempts_used} of ${history.max_attempts} re-verification attempts used. ` +
      (history.active_job_id ? 'Verification is running.' : 'Receipt chain integrity verified.');
    byId('recovery-reverify').disabled = !history.can_reverify;
    byId('recovery-cancel').hidden = !history.active_job_id;
    byId('recovery-automatic').checked = history.automatic;
    const content = byId('recovery-history'); content.replaceChildren();
    for (const receipt of history.receipts) {
      const card = document.createElement('article'); card.style.cssText = 'border-top:1px solid #223649;padding:16px 0';
      element('h3', `${receipt.verdict} · ${new Date(receipt.verified_at).toLocaleString()}`, card);
      element('p', receipt.receipt_id, card).className = 'rid';
      if (receipt.previous_receipt_id) element('p', 'Previous receipt: ' + receipt.previous_receipt_id, card).className = 'rid';
      const warnings = receipt.recovery;
      if (warnings?.oscillating_conditions.length) element('p', 'Oscillating outcomes: ' + warnings.oscillating_conditions.join(', '), card);
      if (warnings?.repeated_failures.length) element('p', 'Repeated failures: ' + warnings.repeated_failures.join(', '), card);
      element('p', receipt.conditions.map(x => `${x.condition}: ${x.status}`).join(' · '), card);
      for (const item of receipt.remediation) {
        const detail = document.createElement('details');
        element('summary', `${item.condition} — ${item.action_hint}`, detail);
        element('p', 'Expected: ' + JSON.stringify(item.expected), detail);
        element('p', 'Observed: ' + JSON.stringify(item.observed), detail);
        element('p', 'Re-verify after: ' + item.reverify_after.replaceAll('_', ' ') +
          (item.retryable ? '' : ' (requires a new contract or registered run)'), detail);
        card.appendChild(detail);
      }
      content.appendChild(card);
    }
    for (const attempt of history.attempts.filter(x => ['EXPIRED','INTERNAL_ERROR'].includes(x.state))) {
      element('p', `Attempt ${attempt.attempt}: ${attempt.state} (${attempt.terminal_reason}). No receipt was issued.`, content);
    }
  }
  async function show(receiptId) {
    controller?.abort(); controller = new AbortController();
    const signal = controller.signal, ticket = ++generation, key = credentials();
    selected = receiptId; data = null; panel.hidden = false; byId('recovery-history').replaceChildren();
    byId('recovery-reverify').disabled = true; byId('recovery-cancel').hidden = true;
    state.textContent = 'Loading verification history…';
    try {
      const history = await request(`/v1/receipts/${encodeURIComponent(receiptId)}/history`, {}, signal);
      if (ticket !== generation || key !== credentials()) return;
      render(history);
      if (history.active_job_id) {
        let revision = -1;
        while (ticket === generation && key === credentials()) {
          const job = await request(`/v1/jobs/${encodeURIComponent(history.active_job_id)}/wait?after_revision=${revision}`, {}, signal);
          if (ticket !== generation || key !== credentials()) return;
          state.textContent = `Verification ${job.state}. ${history.attempts_used} of ${history.max_attempts} attempts used.`;
          if (['COMPLETE','PARTIAL_FAILURE','EXPIRED','INTERNAL_ERROR'].includes(job.state)) { pendingKey = null; await show(receiptId); if (key === credentials()) await load(); break; }
          revision = job.revision;
        }
      }
    } catch (error) { if (ticket === generation && error.name !== 'AbortError') message(error); }
  }
  function close() { ++generation; controller?.abort(); selected = null; data = null; pendingKey = null; panel.hidden = true; byId('recovery-history').replaceChildren(); }
  byId('key').addEventListener('change', close);
  byId('recovery-close').addEventListener('click', close);
  byId('content').addEventListener('click', event => {
    const button = event.target.closest('button[data-receipt]');
    if (button) { pendingKey = null; show(button.dataset.receipt); }
  });
  byId('recovery-refresh').addEventListener('click', () => selected && show(selected));
  byId('recovery-reverify').addEventListener('click', async () => {
    if (!data?.can_reverify) return;
    const head = data.head_id, ticket = generation;
    pendingKey ||= crypto.randomUUID(); byId('recovery-reverify').disabled = true;
    try {
      await request(`/v1/receipts/${encodeURIComponent(head)}/reverify`, {method:'POST', body:'{}', headers:{'Idempotency-Key':pendingKey}});
      if (ticket === generation) await show(head);
    } catch (error) { if (ticket === generation) { message(error); byId('recovery-reverify').disabled = false; } }
  });
  byId('recovery-cancel').addEventListener('click', async () => {
    if (!data?.active_job_id) return;
    const ticket = generation;
    try { await request(`/v1/jobs/${encodeURIComponent(data.active_job_id)}/cancel`, {method:'POST'});
      if (ticket === generation) await show(selected);
    } catch (error) { if (ticket === generation) message(error); }
  });
  byId('recovery-automatic').addEventListener('change', async event => {
    if (!selected || !data) return;
    const ticket = generation, automatic = event.target.checked; event.target.disabled = true;
    try { await request(`/v1/receipts/${encodeURIComponent(selected)}/recovery-policy`, {method:'POST', body:JSON.stringify({automatic})});
      if (ticket === generation) await show(selected);
    } catch (error) { if (ticket === generation) { message(error); event.target.checked = data.automatic; } }
    finally { event.target.disabled = false; }
  });
})();"""
