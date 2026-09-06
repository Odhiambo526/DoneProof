CONNECTIONS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connection Settings · DoneProof</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b111a;color:#e6edf5;font:16px/1.6 system-ui,sans-serif}
main{max-width:840px;margin:auto;padding:32px 20px}a{color:#9ed8ff}h1{line-height:1.2}
section{border:1px solid #334155;border-radius:12px;padding:22px;margin:20px 0;background:#111c2c}
label{display:block}input,button{font:inherit;padding:10px 14px;border-radius:6px;border:1px solid #52627a}
input{width:min(100%,440px);background:#0b111a;color:inherit}button{cursor:pointer;background:#b9f6dc;color:#11231c;margin:10px 12px 0 0}
button:disabled{opacity:.5;cursor:default}.secondary{background:#26384d;color:#fff}p{margin:8px 0}.muted{color:#a6b8cf}
#notice{min-height:26px}h2{text-transform:capitalize}strong{color:#b9f6dc}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #79c8ff;outline-offset:3px}
</style><script src="/connections.js" defer></script></head>
<body><main><a href="/console">← Assurance console</a><h1>Connection Settings</h1>
<p>Connect provider accounts for this workspace. Verification reads provider state independently.</p>
<section><label for="admin-key">Workspace connection administrator key</label>
<input id="admin-key" type="password" autocomplete="off" spellcheck="false">
<button id="load">Load connections</button><p class="muted">Use the administrator key supplied by your DoneProof operator. It is kept in this page only.</p></section>
<p id="notice" role="status" aria-live="polite"></p><div id="connections"></div>
<p class="muted">Disconnect immediately stops verification through the connection. If provider revocation fails, retry disconnect.
Provider revocation may also remove other grants for this app and account. Previously issued receipts remain intact.</p>
</main></body></html>"""

CONNECTIONS_JS = """
'use strict';
const byId = id => document.getElementById(id);
const notice = text => { byId('notice').textContent = text; };
async function api(path, method = 'GET') {
  const response = await fetch('/v1/connections' + path, {method, credentials:'same-origin',
    headers:{'X-DoneProof-Key':byId('admin-key').value}, cache:'no-store', redirect:'error'});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed.');
  return data;
}
function element(tag, text, parent) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.append(node);
  return node;
}
function button(text, parent, action) {
  const node = element('button', text, parent);
  node.addEventListener('click', async () => {
    node.disabled = true;
    try { await action(); } catch (_) { notice('The request could not complete. Reload connections and retry.'); }
    finally { node.disabled = false; }
  });
  return node;
}
async function load() {
  try {
    const [data, metadata] = await Promise.all([api(''), api('/provider-metadata')]);
    const host = byId('connections');
    host.replaceChildren();
    for (const provider of data.providers) {
      const definition = metadata.providers.find(item => item.provider === provider.provider);
      if (!definition) continue;
      const row = data.connections.find(item => item.provider === provider.provider);
      const card = element('section', '', host);
      element('h2', definition.display_name, card);
      element('strong', row ? row.state.replaceAll('_', ' ') : 'Not connected', card);
      if (row && row.account_label) element('p', row.account_label, card);
      if (row && row.expires_at) element('p', 'Access expires: ' + new Date(row.expires_at * 1000).toLocaleString(), card);
      if (row && row.error_code) element('p', 'Action: ' + row.error_code.replaceAll('_', ' '), card);
      if (row && row.revocation_pending) element('p', 'Provider revocation is pending. Verification is disabled. Retry disconnect.', card);
      if (provider.installation_url) {
        const link = element('a', 'Install the provider app on selected resources', card);
        link.href = provider.installation_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      }
      const connect = button(row ? 'Reconnect' : 'Connect', card, async () => {
        const data = await api('/' + provider.provider + '/authorize', 'POST');
        const url = new URL(data.authorization_url);
        const allowed = definition.authorization_origin;
        if (url.origin !== allowed) throw new Error('Invalid authorization destination');
        window.location.assign(url.href);
      });
      connect.disabled = !provider.onboarding_available || Boolean(row && row.revocation_pending);
      if (!provider.onboarding_available) element('p', 'Your operator needs to configure OAuth onboarding for this provider.', card);
      if (row) {
        button('Check health', card, async () => { await api('/' + row.id + '/health', 'POST'); await load(); });
        button(row.revocation_pending ? 'Retry disconnect' : 'Disconnect', card, async () => {
          if (!window.confirm('Disconnect this account and revoke DoneProof access?')) return;
          await api('/' + row.id + '/disconnect', 'POST'); await load();
        }).className = 'secondary';
        if (row.state === 'disabled' && row.revocation_pending) {
          button('Confirm revocation in provider settings', card, async () => {
            if (!window.confirm('Have you already revoked DoneProof access or the legacy token in provider settings? This erases locally retained credentials.')) return;
            await api('/' + row.id + '/confirm-external-revocation', 'POST'); await load();
          }).className = 'secondary';
        }
      }
    }
    notice('Connections loaded.');
  } catch (error) { notice(error.message); }
}
byId('load').addEventListener('click', load);
byId('admin-key').addEventListener('keydown', event => { if (event.key === 'Enter') load(); });
if (location.hash === '#connected') notice('Account connected. Enter your administrator key to view its status.');
else if (location.hash === '#authorization-failed') notice('Authorization did not complete. Load connections and reconnect.');
history.replaceState(null, '', '/connections');
window.addEventListener('pagehide', () => { byId('admin-key').value = ''; });
"""
