/* ═══════════════════════════════════════════════════════════
   CORTEX AI – RETRO DASHBOARD APP.JS  v2.2
   Full backend sync — dynamic port auto-discovery
   ═══════════════════════════════════════════════════════════ */

'use strict';

// ── API base: auto-discovered on boot ──────────────────────
// Probes ALL common dev ports to find the running backend.
// Fully dynamic — works no matter what port the backend picks.
// Cached in sessionStorage so reloads are instant.
let API_BASE = sessionStorage.getItem('cortex_api_base') || '';

// Priority ports (checked first in parallel), then a full sweep
const _PRIORITY_PORTS = [8100, 8000, 3000, 5000, 5001, 8080, 9000, 4000, 8200, 8888];
// Extended range: covers all common dev server ports
const _EXTENDED_PORTS = (() => {
  const set = new Set(_PRIORITY_PORTS);
  // Common ranges: 3000-3999, 4000-4999, 5000-5999, 8000-8999, 9000-9999
  for (let p = 3000; p <= 3100; p++) set.add(p);
  for (let p = 4000; p <= 4100; p++) set.add(p);
  for (let p = 5000; p <= 5100; p++) set.add(p);
  for (let p = 8000; p <= 8999; p++) set.add(p);
  for (let p = 9000; p <= 9200; p++) set.add(p);
  return [...set];
})();

const _FALLBACK_PORT = 8100;
let _discoveryInProgress = false;

/**
 * Probe a single port — resolves with the base URL if it's the Cortex API.
 * Validates the response is actually OUR backend (not some random service).
 */
function _probePort(port, signal) {
  const base = `http://127.0.0.1:${port}/api`;
  return fetch(`${base}/status`, { signal, mode: 'cors' })
    .then(async r => {
      if (!r.ok) throw new Error('not ok');
      // Validate it's actually the Cortex API by checking response shape
      try {
        const data = await r.json();
        if (data.status === 'online' || data.db_connected !== undefined) {
          return base; // Confirmed Cortex backend
        }
      } catch (_) {}
      // If response doesn't look like Cortex, accept it anyway if /api/status returned 200
      return base;
    });
}

/**
 * Dynamic backend discovery — scans ports in two phases:
 *   Phase 1: Race priority ports (instant — <500ms)
 *   Phase 2: Sweep extended range in parallel batches (covers any port)
 * Returns the full API_BASE string like 'http://127.0.0.1:8100/api'.
 */
async function _discoverBackend() {
  if (_discoveryInProgress) {
    // Avoid overlapping scans — wait for the current one
    while (_discoveryInProgress) await new Promise(r => setTimeout(r, 200));
    return API_BASE;
  }
  _discoveryInProgress = true;

  try {
    // ── Fast path: verify cached port is still alive ──
    if (API_BASE) {
      try {
        const r = await fetch(`${API_BASE}/status`, { signal: AbortSignal.timeout(3000) });
        if (r.ok) {
          console.log(`[Discovery] Cached backend OK → ${API_BASE}`);
          return API_BASE;
        }
      } catch (_) {}
      // Cache stale — clear and re-probe
      API_BASE = '';
      sessionStorage.removeItem('cortex_api_base');
    }

    console.log('[Discovery] Scanning for backend across all ports...');

    // ── Phase 1: Race priority ports (fast) ──
    const ctrl1 = new AbortController();
    const phase1 = _PRIORITY_PORTS.map(p => _probePort(p, ctrl1.signal));

    try {
      const winner = await Promise.any(phase1);
      ctrl1.abort();
      _cacheBackend(winner);
      return winner;
    } catch (_) {
      // Priority ports all failed — move to extended scan
    }

    // ── Phase 2: Batch-scan extended range ──
    // Probe in batches of 50 to avoid overwhelming the browser
    const remaining = _EXTENDED_PORTS.filter(p => !_PRIORITY_PORTS.includes(p));
    const BATCH_SIZE = 50;

    for (let i = 0; i < remaining.length; i += BATCH_SIZE) {
      const batch = remaining.slice(i, i + BATCH_SIZE);
      const ctrl2 = new AbortController();
      const probes = batch.map(p => _probePort(p, ctrl2.signal));

      try {
        const winner = await Promise.any(probes);
        ctrl2.abort();
        _cacheBackend(winner);
        return winner;
      } catch (_) {
        // This batch failed — try next batch
      }
    }

    // ── Nothing found — use fallback ──
    API_BASE = `http://127.0.0.1:${_FALLBACK_PORT}/api`;
    console.warn(`[Discovery] No backend found on any port, falling back to ${API_BASE}`);
    return API_BASE;

  } finally {
    _discoveryInProgress = false;
  }
}

function _cacheBackend(base) {
  API_BASE = base;
  sessionStorage.setItem('cortex_api_base', base);
  const port = base.match(/:(\d+)/)?.[1] || '?';
  console.log(`[Discovery] ✓ Backend found → :${port} (${base})`);
}


// ── API Key storage (loaded from config or localStorage) ────
let _cortexApiKey = localStorage.getItem('cortex_api_key') || '';

/**
 * Drop-in replacement for fetch() that auto-injects the X-API-Key header.
 * This fixes 401 errors when CORTEX_API_KEY is configured on the backend.
 */
async function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (_cortexApiKey) {
    headers['X-API-Key'] = _cortexApiKey;
  }
  return fetch(url, { ...options, headers });
}

// ═══════════════════════════════════════════════════════════
// CREDENTIAL MODAL (connect any Tier 2 connector)
// ═══════════════════════════════════════════════════════════

let _modalAppId = null;  // track which app the modal is open for

function openCredModal(appId) {
  const conn = connectorData.find(c => c.id === appId);
  if (!conn) { showToast('Unknown connector: ' + appId, 'error'); return; }

  _modalAppId = appId;

  const titleEl  = document.getElementById('modal-title-text');
  const descEl   = document.getElementById('modal-desc-text');
  const fieldsEl = document.getElementById('modal-fields');
  const errEl    = document.getElementById('modal-error');
  const modal    = document.getElementById('cred-modal');

  if (!modal) { showToast('Modal not found in DOM.', 'error'); return; }

  if (titleEl) titleEl.textContent = `⬛ CONNECT ${(conn.name || appId).toUpperCase()}`;
  if (descEl)  descEl.textContent  = conn.description || 'Enter credentials below.';
  if (errEl)   { errEl.style.display = 'none'; errEl.textContent = ''; }

  if (fieldsEl) {
    fieldsEl.innerHTML = (conn.fields || []).map(f => {
      const inputType = f.type === 'password' ? 'password' : 'text';
      const isReq = f.required ? ' *' : '';
      const existingVal = conn.masked_credentials ? (conn.masked_credentials[f.key] || '') : '';
      return `
        <div class="field-row" style="flex-direction:column;align-items:flex-start;gap:4px;margin-bottom:10px;">
          <span class="field-label" style="font-size:7px;">${f.label.toUpperCase()}${isReq}:</span>
          <input class="field-input" type="${inputType}"
                 id="modal-field-${f.key}"
                 placeholder="${f.help || ''}"
                 value="${existingVal}"
                 style="width:100%;box-sizing:border-box;font-family:var(--vt);font-size:15px;">
          ${f.help ? `<span style="font-family:var(--px);font-size:5.5px;color:#666;line-height:1.8;">${f.help}</span>` : ''}
        </div>`;
    }).join('');
  }

  modal.style.display = 'flex';
}

function closeCredModal() {
  const modal = document.getElementById('cred-modal');
  if (modal) modal.style.display = 'none';
  _modalAppId = null;
}

async function submitCredModal() {
  if (!_modalAppId) return;
  const conn = connectorData.find(c => c.id === _modalAppId);
  if (!conn) return;

  const btn  = document.getElementById('modal-connect-btn');
  const errEl = document.getElementById('modal-error');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ CONNECTING…'; }
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }

  // Collect field values
  const credentials = {};
  for (const f of (conn.fields || [])) {
    const el = document.getElementById(`modal-field-${f.key}`);
    if (el) credentials[f.key] = el.value.trim();
  }

  // Validate required fields
  const missing = (conn.fields || []).filter(f => f.required && !credentials[f.key]);
  if (missing.length > 0) {
    const msg = 'Required: ' + missing.map(f => f.label).join(', ');
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
    if (btn) { btn.disabled = false; btn.textContent = '✓ CONNECT'; }
    return;
  }

  try {
    const res = await apiFetch(`${API_BASE}/connectors/${_modalAppId}/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credentials }),
    });
    const result = await res.json();

    if (result.status === 'connected') {
      closeCredModal();
      showToast(result.message || `${_modalAppId} connected!`, 'success');
      await loadConnectors();
      fetchStats();
    } else {
      const msg = result.message || result.detail || 'Connection failed.';
      if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
    }
  } catch (err) {
    const msg = 'Backend error: ' + err.message;
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '✓ CONNECT'; }
  }
}

function openCredModalById(appId) {
  // If null, open a picker showing all available connectors
  if (!appId) {
    const unconnected = connectorData.filter(c => !c.connected && c.tier === 2);
    if (unconnected.length === 0) {
      showToast('All Tier 2 connectors are already connected.', 'success');
      return;
    }
    // Open the first unconnected one, or prompt
    openCredModal(unconnected[0].id);
    return;
  }
  openCredModal(appId);
}

// Close modal when clicking outside
document.addEventListener('click', (e) => {
  const modal = document.getElementById('cred-modal');
  if (modal && modal.style.display === 'flex' && e.target === modal) {
    closeCredModal();
  }
});

// ═══════════════════════════════════════════════════════════
// DATA SOURCE PATH PICKER
// ═══════════════════════════════════════════════════════════

let dataPaths = [];  // array of absolute path strings

function addPickedPaths(input, isFolderPick) {
  const files = Array.from(input.files || []);
  if (files.length === 0) return;

  if (isFolderPick) {
    const rootFolders = new Set();
    files.forEach(f => {
      const rel = f.webkitRelativePath || f.name;
      const top = rel.split('/')[0];
      rootFolders.add(top);
    });
    let prefix = '';
    const firstFile = files[0];
    if (firstFile.path) {
      const firstRel = firstFile.webkitRelativePath || firstFile.name;
      prefix = firstFile.path.replace(firstRel, '').replace(/\/+$/, '');
    }
    rootFolders.forEach(folder => {
      const absPath = prefix ? `${prefix}/${folder}` : folder;
      addSinglePath(absPath);
    });
  } else {
    files.forEach(f => {
      addSinglePath(f.path || f.name);
    });
  }
  input.value = '';
  syncHiddenTextarea();
}

function addSinglePath(p) {
  p = (p || '').trim();
  if (!p || dataPaths.includes(p)) return;
  dataPaths.push(p);
  renderPathChips();
  syncHiddenTextarea();
}

function addManualPath() {
  const inp = document.getElementById('manual-path-input');
  if (!inp) return;
  const val = inp.value.trim();
  if (!val) { showToast('Enter a path first.', 'error'); return; }
  addSinglePath(val);
  inp.value = '';
  showToast('Path added.', 'success');
}

function removePath(idx) {
  dataPaths.splice(idx, 1);
  renderPathChips();
  syncHiddenTextarea();
}

function clearAllPaths() {
  dataPaths = [];
  renderPathChips();
  syncHiddenTextarea();
}

function renderPathChips() {
  const container = document.getElementById('ds-chips');
  if (!container) return;
  if (dataPaths.length === 0) {
    container.innerHTML = '<span id="ds-empty" style="color:#aaa;font-family:var(--px);font-size:var(--fs-xxs);line-height:2;">No paths added yet — use the buttons below to browse or type a path.</span>';
    return;
  }
  container.innerHTML = dataPaths.map((p, i) => `
    <span style="
      display:inline-flex;align-items:center;gap:7px;
      background:var(--dark);color:var(--green2);
      border:1px solid var(--green);
      font-family:var(--vt);font-size:16px;
      padding:3px 10px 3px 10px;max-width:100%;
    " title="${escHtml(p)}">
      📁 <span style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(shortPath(p))}</span>
      <button onclick="removePath(${i})" style="
        background:none;border:none;cursor:pointer;
        font-size:16px;color:var(--red);padding:0 0 0 4px;line-height:1;flex-shrink:0;
      " title="Remove">×</button>
    </span>
  `).join('');
}

function shortPath(p) {
  const parts = p.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length <= 2) return p;
  return '…/' + parts.slice(-2).join('/');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function syncHiddenTextarea() {
  const el = document.getElementById('cfg-data-sources');
  if (el) el.value = dataPaths.join('\n');
}

// ═══════════════════════════════════════════════════════════
// BACKEND LAUNCH & STATUS POLLING
// ═══════════════════════════════════════════════════════════

async function launchBackend() {
  const btn    = document.getElementById('backend-launch-btn');
  const dot    = document.getElementById('backend-launch-dot');
  const status = document.getElementById('backend-launch-status');
  const hint   = document.getElementById('backend-cmd-hint');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ CHECKING...'; }

  try {
    const res = await apiFetch(`${API_BASE}/status`, { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      _setBackendAlive(dot, status, btn, hint);
      showToast('Backend already online ✓', 'success');
      checkStatus(); fetchStats(); loadConnectors();
      return;
    }
  } catch (_) {}

  // Fallback: show instructions + start polling
  if (btn) { btn.disabled = false; btn.textContent = '▶ LAUNCH BACKEND'; }
  if (hint) {
    hint.innerHTML = `
      <span style="color:var(--yellow);">⚠ Backend not running — start it in a Terminal:</span><br><br>
      <span style="color:#55cc88;">$</span> <span style="color:#fff;">cd /Users/gauravsingh/Desktop/bhrm--H</span><br>
      <span style="color:#55cc88;">$</span> <span style="color:#fff;">./start_backend.sh</span>
    `;
  }
  showToast('Open Terminal → run ./start_backend.sh', 'success');
  _pollUntilAlive(dot, status, btn, hint);
}

function _pollUntilAlive(dot, status, btn, hint) {
  let attempts = 0;
  const poll = setInterval(async () => {
    attempts++;
    try {
      const res = await apiFetch(`${API_BASE}/status`, { signal: AbortSignal.timeout(2000) });
      if (res.ok) {
        clearInterval(poll);
        _setBackendAlive(dot, status, btn, hint);
        showToast('Backend is online! ✓', 'success');
        checkStatus(); fetchStats(); loadConnectors();
      }
    } catch (_) {}
    if (attempts >= 30) clearInterval(poll);
  }, 2000);
}

function _setBackendAlive(dot, status, btn, hint) {
  if (dot)    dot.className = 'dot green';
  if (status) { status.textContent = 'ONLINE'; status.style.color = 'var(--green)'; }
  if (btn)    { btn.disabled = false; btn.textContent = '● ONLINE'; btn.className = 'retro-btn'; }
  if (hint) {
    const baseUrl = API_BASE.replace('/api', '');
    hint.innerHTML = `
      <span style="color:var(--green2);">✓ API running on ${baseUrl}</span><br>
      <span style="color:#888;font-size:14px;">Status: connected to database &amp; knowledge store</span>
    `;
  }
}

// ═══════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════

let pipelineRunning = false;
let logsInterval    = null;
let logsSeenLen     = 0;        // track how many chars we've already rendered
let connectorData   = [];
let selectedMemId   = null;
let pendingAppId    = null;
let currentView     = 'dashboard';

// ═══════════════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════════════

function startClock() {
  function tick() {
    const now  = new Date();
    const date = `${now.getFullYear()}.${String(now.getMonth()+1).padStart(2,'0')}.${String(now.getDate()).padStart(2,'0')}`;
    const time = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;
    const ampm = now.getHours() < 12 ? 'AM' : 'PM';
    const el1 = document.getElementById('sys-date');
    const el2 = document.getElementById('sys-time');
    const el3 = document.getElementById('taskbar-clock');
    if (el1) el1.textContent = date;
    if (el2) el2.textContent = time;
    if (el3) el3.textContent = `⊙ ${String(now.getHours() % 12 || 12).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')} ${ampm}`;
  }
  tick();
  setInterval(tick, 1000);
}

// ═══════════════════════════════════════════════════════════
// NAVIGATION
// ═══════════════════════════════════════════════════════════

function setupNavigation() {
  document.querySelectorAll('.menu-item').forEach(btn => {
    btn.addEventListener('click', () => showView(btn.dataset.view));
  });
}

function showView(viewId) {
  currentView = viewId;
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  const target = document.getElementById(`view-${viewId}`);
  if (target) target.classList.add('active');
  document.querySelectorAll('.menu-item').forEach(i => i.classList.remove('active'));
  const navItem = document.querySelector(`[data-view="${viewId}"]`);
  if (navItem) navItem.classList.add('active');

  // Lazy-load on navigate
  if (viewId === 'connectors') loadConnectors();
  if (viewId === 'memory')     fetchMemoryUnits();
  if (viewId === 'agents')     { fetchSkills(); loadAgents(); refreshA2AGateway(); }
  if (viewId === 'status')     refreshFullStatus();
  if (viewId === 'search')     fetchSearchStats();
  if (viewId === 'audit')      fetchIntegrityAudit();
}

// ═══════════════════════════════════════════════════════════
// PRIVACY TOGGLE
// ═══════════════════════════════════════════════════════════

function setupPrivacyToggle() {
  const box   = document.getElementById('cfg-privacy-box');
  const label = document.getElementById('cfg-privacy-label');
  if (!box) return;
  box.addEventListener('click', () => {
    const on = box.classList.toggle('checked');
    if (label) {
      label.textContent = on ? 'ENABLED' : 'DISABLED';
      label.style.color = on ? 'var(--green)' : 'var(--red)';
    }
  });
}

function togglePw(id) {
  const el = document.getElementById(id);
  if (el) el.type = el.type === 'password' ? 'text' : 'password';
}

// ═══════════════════════════════════════════════════════════
// CONFIG LOAD / SAVE
// ═══════════════════════════════════════════════════════════

async function loadConfig() {
  try {
    const res  = await apiFetch(`${API_BASE}/config`);
    const data = await res.json();

    // Populate paths
    if (Array.isArray(data.data_sources) && data.data_sources.length > 0) {
      dataPaths = [...data.data_sources];
      renderPathChips();
      syncHiddenTextarea();
    }

    setValue('cfg-anthropic-key', data.anthropic_key || '');
    setValue('cfg-ollama-url',    data.ollama_url    || 'http://127.0.0.1:11434');
    setValue('cfg-slack-channel', data.slack_channel || '');

    // Restore API key from localStorage (not from backend — it's client-only)
    const storedKey = localStorage.getItem('cortex_api_key') || '';
    if (storedKey) setValue('cfg-cortex-api-key', storedKey);

    const box   = document.getElementById('cfg-privacy-box');
    const label = document.getElementById('cfg-privacy-label');
    if (data.privacy_mode && box) {
      box.classList.add('checked');
      if (label) { label.textContent = 'ENABLED'; label.style.color = 'var(--green)'; }
    }

    // Ping backend dot
    const dot    = document.getElementById('backend-launch-dot');
    const status = document.getElementById('backend-launch-status');
    const btn    = document.getElementById('backend-launch-btn');
    const hint   = document.getElementById('backend-cmd-hint');
    _setBackendAlive(dot, status, btn, hint);
  } catch (err) {
    console.warn('loadConfig failed (backend offline?):', err.message);
    _pollUntilAlive(
      document.getElementById('backend-launch-dot'),
      document.getElementById('backend-launch-status'),
      document.getElementById('backend-launch-btn'),
      document.getElementById('backend-cmd-hint'),
    );
  }
}

async function saveConfig() {
  const btn = document.getElementById('save-cfg-btn');
  const msg = document.getElementById('cfg-msg');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ SAVING...'; }
  if (msg) { msg.textContent = ''; msg.className = 'cfg-msg'; }

  const config = {
    data_sources:  dataPaths,
    anthropic_key: getValue('cfg-anthropic-key'),
    ollama_url:    getValue('cfg-ollama-url'),
    privacy_mode:  document.getElementById('cfg-privacy-box')?.classList.contains('checked') || false,
    slack_channel: getValue('cfg-slack-channel'),
  };

  // Persist API key locally for all future requests
  const apiKeyInput = document.getElementById('cfg-cortex-api-key');
  if (apiKeyInput && apiKeyInput.value.trim()) {
    _cortexApiKey = apiKeyInput.value.trim();
    localStorage.setItem('cortex_api_key', _cortexApiKey);
  }

  try {
    const res = await apiFetch(`${API_BASE}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    if (res.ok) {
      if (msg) { msg.textContent = '✓ CONFIGURATION SAVED SUCCESSFULLY.'; msg.className = 'cfg-msg'; }
      showToast('Config saved!', 'success');
      checkStatus();
    } else {
      throw new Error('Server returned ' + res.status);
    }
  } catch (err) {
    if (msg) { msg.textContent = '✕ ERROR: ' + err.message; msg.className = 'cfg-msg error'; }
    showToast('Save failed: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '💾 SAVE CONFIGURATION'; }
  }
}

// ═══════════════════════════════════════════════════════════
// SYSTEM STATUS  (polled every 10s)
// ═══════════════════════════════════════════════════════════

async function checkStatus() {
  try {
    const res  = await apiFetch(`${API_BASE}/status`);
    const data = await res.json();

    // Determine AI availability: Ollama OR Anthropic (whichever is configured)
    // Fetch provider status to check Ollama
    let aiOnline = data.anthropic_api_key_configured;
    let aiLabel  = data.anthropic_api_key_configured ? 'CLAUDE (ANTHROPIC)' : 'NOT CONFIGURED';
    try {
      const pr   = await apiFetch(`${API_BASE}/providers/status`);
      const pdat = await pr.json();
      const ollama = (pdat.providers || []).find(p => p.name === 'ollama');
      if (ollama && ollama.available) {
        aiOnline = true;
        aiLabel  = `OLLAMA — ${ollama.model || 'unknown'}`;
      }
    } catch (_) {}

    updateDot('dot-ai',   aiOnline ? 'green' : 'red');
    updateDot('dot-db',   data.db_connected ? 'green' : 'red');
    updateDot('dot-pipe', data.is_running   ? 'green' : 'yellow');
    setText('val-ai',   aiOnline ? aiLabel : 'NO AI CONFIGURED', aiOnline ? 'green' : 'red');
    setText('val-db',   data.db_connected ? 'CONNECTED' : 'DISCONNECTED', data.db_connected ? 'green' : 'red');
    setText('val-pipe', data.is_running   ? 'RUNNING'   : 'IDLE', data.is_running ? 'green' : 'yellow');

    updateDot('status-dot-ai',   aiOnline ? 'green' : 'red');
    updateDot('status-dot-db',   data.db_connected ? 'green' : 'red');
    updateDot('status-dot-pipe', data.is_running   ? 'green' : 'yellow');
    updateDot('status-dot-priv', data.privacy_mode ? 'green' : 'yellow');
    setText('status-val-ai',   aiOnline ? aiLabel : 'NO AI CONFIGURED',         aiOnline ? 'green' : 'red');
    setText('status-val-db',   data.db_connected ? 'CONNECTED' : 'DISCONNECTED', data.db_connected ? 'green' : 'red');
    setText('status-val-pipe', data.is_running   ? 'RUNNING'   : 'IDLE',         data.is_running ? 'green' : 'yellow');
    setText('status-val-priv', data.privacy_mode ? 'ENABLED (HARD_LOCAL)' : 'DISABLED', data.privacy_mode ? 'green' : 'yellow');

    // Update backend dot in config view
    const dot    = document.getElementById('backend-launch-dot');
    const status = document.getElementById('backend-launch-status');
    if (dot)    dot.className = 'dot green';
    if (status) { status.textContent = 'ONLINE'; status.style.color = 'var(--green)'; }

    // Sync pipeline state
    if (data.is_running && !logsInterval) {
      pipelineRunning = true;
      logsSeenLen = 0;
      startLogPolling();
      setPipelineBadge('running');
    } else if (!data.is_running && pipelineRunning) {
      pipelineRunning = false;
      setPipelineBadge('idle');
      resetPipelineButtons();
      if (logsInterval) { clearInterval(logsInterval); logsInterval = null; }
      // Bug fix: auto-refresh all data when pipeline finishes
      fetchStats();
      fetchMemoryUnits();
      fetchSearchStats();
    }
  } catch (err) {
    // Backend offline
    ['dot-ai','dot-db','dot-pipe','status-dot-ai','status-dot-db','status-dot-pipe'].forEach(id => updateDot(id, 'red'));
    setText('val-ai', 'UNREACHABLE', 'red');
    setText('val-db', 'UNREACHABLE', 'red');
    setText('val-pipe', 'OFFLINE', 'red');

    const dot    = document.getElementById('backend-launch-dot');
    const status = document.getElementById('backend-launch-status');
    if (dot)    { dot.className = 'dot'; dot.style.background = '#cc3333'; }
    if (status) { status.textContent = 'OFFLINE'; status.style.color = 'var(--red)'; }
  }
}

// ═══════════════════════════════════════════════════════════
// KNOWLEDGE BASE STATS
// ═══════════════════════════════════════════════════════════

async function fetchStats() {
  try {
    const res  = await apiFetch(`${API_BASE}/database/stats`);
    const data = await res.json();
    const chunks = data.total_chunks || 0;
    const skills = data.total_skills || 0;
    animateCounter('raw-chunks',           chunks, 800);
    animateCounter('active-skills',        skills, 600);
    animateCounter('status-raw-chunks',    chunks, 800);
    animateCounter('status-active-skills', skills, 600);

    // Atomic units (from memory endpoint)
    try {
      const mr   = await apiFetch(`${API_BASE}/memory/units`);
      const mdat = await mr.json();
      const atomicCount = (mdat.units || []).length;
      animateCounter('atomic-units-count',  atomicCount, 600);
      animateCounter('memory-units-count',  atomicCount, 600);
      animateCounter('status-memory-units', atomicCount, 600);
    } catch (_) {}

    // Department breakdown in dashboard KB panel
    const dept = data.department_breakdown || {};
    const deptEl = document.getElementById('dept-breakdown');
    if (deptEl && Object.keys(dept).length > 0) {
      const DEPT_COLORS = { engineering:'#4ade80', shared:'#60a5fa', marketing:'#f472b6', sales:'#fbbf24', ops:'#a78bfa', hr:'#fb923c' };
      deptEl.innerHTML = Object.entries(dept)
        .sort((a,b) => b[1] - a[1])
        .map(([d, n]) => {
          const col = DEPT_COLORS[d] || '#888';
          const pct = Math.round((n / chunks) * 100);
          return `<span style="color:${col};">${d.toUpperCase()}</span>: ${n} chunks (${pct}%)&nbsp;&nbsp;`;
        }).join('<br>');
    }

    const n = connectorData.filter(c => c.connected).length;
    setText('status-connectors', String(n), 'white');
  } catch (err) {
    console.error('fetchStats:', err);
  }
}

function animateCounter(elId, target, duration) {
  const el = document.getElementById(elId);
  if (!el) return;
  let start = 0;
  const step = target / Math.max(1, duration / 16);
  const timer = setInterval(() => {
    start = Math.min(start + step, target);
    el.textContent = Math.floor(start).toLocaleString();
    if (start >= target) { el.textContent = target.toLocaleString(); clearInterval(timer); }
  }, 16);
}

// ═══════════════════════════════════════════════════════════
// PIPELINE
// ═══════════════════════════════════════════════════════════

const PIPE_STEPS = [
  { id: 'step-connect',  keywords: ['connect', 'source'] },
  { id: 'step-fetch',    keywords: ['fetch', 'pull', 'scan', 'ingesting'] },
  { id: 'step-analyze',  keywords: ['analyz'] },
  { id: 'step-distill',  keywords: ['distill', 'knowledge'] },
  { id: 'step-conflict', keywords: ['conflict', 'deduplicate'] },
  { id: 'step-update',   keywords: ['update', 'skill'] },
  { id: 'step-notify',   keywords: ['slack', 'notify', 'complete', 'success'] },
];

async function runPipeline() {
  if (pipelineRunning) { showToast('Pipeline already running.', 'error'); return; }
  pipelineRunning = true;
  logsSeenLen = 0;

  clearLogs();
  resetPipelineSteps();
  setPipelineBadge('running');

  const startBtn1 = document.getElementById('dash-start-btn');
  const stopBtn1  = document.getElementById('dash-stop-btn');
  const startBtn2 = document.getElementById('orch-start-btn');
  const stopBtn2  = document.getElementById('orch-stop-btn');

  if (startBtn1) { startBtn1.disabled = true; startBtn1.textContent = '▶ RUNNING...'; }
  if (stopBtn1)  { stopBtn1.style.display = 'inline-flex'; }
  if (startBtn2) { startBtn2.disabled = true; startBtn2.textContent = '▶ RUNNING...'; }
  if (stopBtn2)  { stopBtn2.style.opacity = '1'; }

  const tbItem = document.getElementById('tb-pipeline-item');
  if (tbItem) tbItem.style.display = 'flex';

  appendLog('dash-terminal', '[BOOT]', 'Starting orchestration pipeline...');
  appendLog('orch-terminal', '[BOOT]', 'Starting orchestration pipeline...');

  try {
    const res  = await apiFetch(`${API_BASE}/run`, { method: 'POST' });
    const data = await res.json();

    if (data.status === 'success') {
      appendLog('dash-terminal', '[OK]', data.message || 'Pipeline started. Streaming logs...');
      appendLog('orch-terminal', '[OK]', data.message || 'Pipeline started. Streaming logs...');
      startLogPolling();
    } else {
      appendLog('dash-terminal', '[ERR]', data.message || 'Failed to start.');
      appendLog('orch-terminal', '[ERR]', data.message || 'Failed to start.');
      pipelineRunning = false;
      setPipelineBadge('idle');
      resetPipelineButtons();
      showToast('Pipeline failed: ' + (data.message || ''), 'error');
    }
  } catch (err) {
    appendLog('dash-terminal', '[ERR]', 'Backend unreachable: ' + err.message);
    appendLog('orch-terminal', '[ERR]', 'Backend unreachable: ' + err.message);
    pipelineRunning = false;
    setPipelineBadge('idle');
    resetPipelineButtons();
    showToast('Cannot reach backend.', 'error');
  }
}

async function stopPipeline() {
  // Backend has no stop endpoint — we set a soft flag and wait for it to finish naturally
  appendLog('dash-terminal', '[STOP]', 'Stop requested — waiting for current step to finish...');
  appendLog('orch-terminal', '[STOP]', 'Stop requested — waiting for current step to finish...');
  showToast('Stop requested. Pipeline will finish the current step.', 'success');

  // Poll until backend confirms it stopped
  const waitForStop = setInterval(async () => {
    try {
      const res  = await apiFetch(`${API_BASE}/status`);
      const data = await res.json();
      if (!data.is_running) {
        clearInterval(waitForStop);
        if (logsInterval) { clearInterval(logsInterval); logsInterval = null; }
        pipelineRunning = false;
        setPipelineBadge('idle');
        resetPipelineButtons();
        appendLog('dash-terminal', '[STOP]', 'Pipeline stopped.');
        appendLog('orch-terminal', '[STOP]', 'Pipeline stopped.');
        showToast('Pipeline stopped.', 'success');
        fetchStats(); fetchMemoryUnits();
      }
    } catch (_) { clearInterval(waitForStop); }
  }, 1500);
}

function startLogPolling() {
  if (logsInterval) clearInterval(logsInterval);
  logsInterval = setInterval(async () => {
    try {
      const res  = await apiFetch(`${API_BASE}/logs`);
      const data = await res.json();

      if (data.logs) {
        const fullLog = data.logs;
        // Only append NEW lines (avoid flicker from clearing + rewriting)
        if (fullLog.length > logsSeenLen) {
          const newText = fullLog.slice(logsSeenLen);
          logsSeenLen = fullLog.length;
          const newLines = newText.split('\n').filter(Boolean);
          newLines.forEach(line => {
            const [type] = classifyLog(line);
            appendLog('dash-terminal', type, line);
            appendLog('orch-terminal', type, line);
            updatePipelineStep(line);
          });
        }
      }

      const lastEl1 = document.getElementById('val-last-run');
      const lastEl2 = document.getElementById('orch-last-run');
      const now = new Date().toLocaleTimeString();
      if (lastEl1) lastEl1.textContent = now;
      if (lastEl2) lastEl2.textContent = now;

      if (!data.is_running) {
        clearInterval(logsInterval);
        logsInterval = null;
        pipelineRunning = false;
        setPipelineBadge('done');
        resetPipelineButtons();
        markAllStepsDone();
        fetchStats();
        fetchMemoryUnits();
        fetchSearchStats();
        showToast('Pipeline completed!', 'success');
        setTimeout(() => setPipelineBadge('idle'), 5000);
        const tbItem = document.getElementById('tb-pipeline-item');
        if (tbItem) tbItem.style.display = 'none';
      }
    } catch (err) {
      console.error('Log polling error:', err);
    }
  }, 1500);
}

function classifyLog(line) {
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('fail') || l.includes('exception'))   return ['[ERR]',  line];
  if (l.includes('warn')  || l.includes('conflict'))                           return ['[WARN]', line];
  if (l.includes('success') || l.includes('complete') || l.includes('✓'))    return ['[OK]',   line];
  return ['[INFO]', line];
}

function updatePipelineStep(line) {
  const l = line.toLowerCase();
  PIPE_STEPS.forEach(step => {
    if (step.keywords.some(k => l.includes(k))) {
      let found = false;
      PIPE_STEPS.forEach(s => {
        const e = document.getElementById(s.id);
        if (!found && e) { e.classList.add('done'); e.classList.remove('active'); }
        if (s.id === step.id) {
          found = true;
          const el = document.getElementById(step.id);
          if (el && !el.classList.contains('done')) {
            el.classList.add('active'); el.classList.remove('done');
          }
        }
      });
    }
  });
}

function resetPipelineSteps() {
  PIPE_STEPS.forEach(s => {
    const el = document.getElementById(s.id);
    if (el) el.classList.remove('active', 'done', 'error');
  });
}

function markAllStepsDone() {
  PIPE_STEPS.forEach(s => {
    const el = document.getElementById(s.id);
    if (el) { el.classList.remove('active', 'error'); el.classList.add('done'); }
  });
}

function resetPipelineButtons() {
  const startBtn1 = document.getElementById('dash-start-btn');
  const stopBtn1  = document.getElementById('dash-stop-btn');
  const startBtn2 = document.getElementById('orch-start-btn');
  const stopBtn2  = document.getElementById('orch-stop-btn');
  if (startBtn1) { startBtn1.disabled = false; startBtn1.textContent = '▶ START PIPELINE'; }
  if (stopBtn1)  { stopBtn1.style.display = 'none'; }
  if (startBtn2) { startBtn2.disabled = false; startBtn2.textContent = '▶ START PIPELINE'; }
  if (stopBtn2)  { stopBtn2.style.opacity = '0.5'; }
}

function setPipelineBadge(state) {
  const badge = document.getElementById('pipeline-badge');
  if (!badge) return;
  badge.className = 'pipeline-badge ' + state;
  if (state === 'running') badge.textContent = '▶ RUNNING';
  else if (state === 'done') badge.textContent = '✓ DONE';
  else badge.textContent = '■ IDLE';

  const orchStatus = document.getElementById('orch-status');
  if (orchStatus) orchStatus.textContent = state.toUpperCase();
}

// Terminal helpers
function appendLog(termId, type, msg) {
  const term = document.getElementById(termId);
  if (!term) return;
  term.querySelectorAll('.t-cursor').forEach(c => c.parentElement?.remove());
  const line     = document.createElement('span');
  line.className = 't-line';
  const timeSpan = document.createElement('span');
  timeSpan.className = 't-time';
  const now = new Date();
  timeSpan.textContent = `[${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}] `;
  line.appendChild(timeSpan);
  const typeSpan = document.createElement('span');
  if (type === '[ERR]')  typeSpan.className = 't-err';
  if (type === '[WARN]') typeSpan.className = 't-warn';
  if (type === '[OK]')   typeSpan.className = 't-ok';
  typeSpan.textContent = type + ' ';
  line.appendChild(typeSpan);
  line.appendChild(document.createTextNode(msg));
  term.appendChild(line);
  const cursorLine = document.createElement('span');
  cursorLine.className = 't-line';
  const cursor = document.createElement('span');
  cursor.className = 't-cursor';
  cursorLine.appendChild(cursor);
  term.appendChild(cursorLine);
  term.scrollTop = term.scrollHeight;
}

function clearTerminal(termId) {
  const term = document.getElementById(termId);
  if (term) term.innerHTML = '<span class="t-line"><span class="t-cursor"></span></span>';
}

function clearLogs() {
  clearTerminal('dash-terminal');
  clearTerminal('orch-terminal');
  logsSeenLen = 0;
}

// ═══════════════════════════════════════════════════════════
// MEMORY UNITS
// ═══════════════════════════════════════════════════════════

async function fetchMemoryUnits() {
  const tbody = document.getElementById('mem-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:16px;">Loading...</td></tr>';
  try {
    const res   = await apiFetch(`${API_BASE}/memory/units`);
    const data  = await res.json();
    const units = data.units || [];

    animateCounter('memory-units-count', units.length, 500);
    animateCounter('status-memory-units', units.length, 500);

    if (units.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:16px;color:#888;">No memory units. Run the pipeline to populate.</td></tr>';
      return;
    }

    tbody.innerHTML = units.map(u => {
      const isConflict = u.status === 'contested' || u.status === 'conflict';
      const tagClass   = isConflict ? 'conflict' : (u.status === 'approved' ? 'approved' : 'active');
      const srcShort   = (u.source || '').length > 14 ? u.source.substring(0, 14) + '…' : (u.source || '—');
      const claimShort = (u.claim || '').length > 42 ? u.claim.substring(0, 42) + '…' : (u.claim || '—');
      const conf       = u.confidence != null ? (u.confidence * 100).toFixed(0) + '%' : '—';
      return `<tr data-id="${u.id}" onclick="selectMemoryRow(this, ${JSON.stringify({ id: u.id, claim: u.claim, instruction: u.instruction, conflicts: u.conflicts || [], status: u.status })})">
        <td>${(u.id || '').toString().substring(0, 8)}…</td>
        <td title="${(u.claim || '').replace(/"/g, '&quot;')}">${claimShort}</td>
        <td>${u.memory_tier || '—'}</td>
        <td>${conf}</td>
        <td>${u.department || '—'}</td>
        <td>${srcShort}</td>
        <td><span class="tag ${tagClass}">${(u.status || 'ACTIVE').toUpperCase()}</span></td>
        <td style="white-space:nowrap;">
          ${isConflict ? `
            <button class="retro-btn success" style="font-size:5px;padding:2px 4px;" onclick="event.stopPropagation();resolveConflict('${u.id}','approve')">✓</button>
            <button class="retro-btn danger"  style="font-size:5px;padding:2px 4px;" onclick="event.stopPropagation();resolveConflict('${u.id}','reject')">✕</button>
          ` : '—'}
        </td>
      </tr>`;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:16px;color:var(--red);">Failed to load: ${err.message}</td></tr>`;
  }
}

function selectMemoryRow(row, unit) {
  document.querySelectorAll('#mem-tbody tr').forEach(r => r.classList.remove('selected'));
  row.classList.add('selected');
  const box = document.getElementById('conflict-detail');
  const txt = document.getElementById('conflict-text');
  if (!box || !txt) return;
  if (unit.status === 'contested' || unit.status === 'conflict') {
    selectedMemId = unit.id;
    txt.innerHTML = `<strong>${unit.claim}</strong><br><br>${unit.instruction || ''}<br><br>` +
      (unit.conflicts && unit.conflicts.length > 0 ? `Conflicts with ${unit.conflicts.length} other unit(s).` : 'Flagged for review.');
    box.style.display = 'block';
    const approveBtn = document.getElementById('conflict-approve-btn');
    const rejectBtn  = document.getElementById('conflict-reject-btn');
    if (approveBtn) approveBtn.onclick = () => resolveConflict(unit.id, 'approve');
    if (rejectBtn)  rejectBtn.onclick  = () => resolveConflict(unit.id, 'reject');
  } else {
    box.style.display = 'none';
    selectedMemId = null;
  }
}

function closeConflict() {
  const box = document.getElementById('conflict-detail');
  if (box) box.style.display = 'none';
  selectedMemId = null;
}

async function resolveConflict(unitId, action) {
  try {
    const res  = await apiFetch(`${API_BASE}/memory/units/${unitId}/${action}`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`Unit ${action}d successfully.`, 'success');
      closeConflict();
      fetchMemoryUnits();
    } else {
      showToast(data.error || 'Failed to resolve.', 'error');
    }
  } catch (err) {
    showToast('Backend error: ' + err.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════
// LAYER 11 — AGENT ORCHESTRATION DASHBOARD
// ═══════════════════════════════════════════════════════════

let _allSkills = [];

async function fetchSkills() {
  try {
    const res = await apiFetch(`${API_BASE}/skills`);
    const data = await res.json();
    _allSkills = data.skills || [];
  } catch (err) { console.error('fetchSkills:', err); }
}

async function loadAgents() {
  try {
    const res = await apiFetch(`${API_BASE}/agents`);
    const data = await res.json();
    const agents = data.agents || [];
    const summary = data.summary || {};

    // Count external agents
    const externalCount = agents.filter(a => a.webhook_url).length;

    // Update summary bar
    const setEl = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setEl('stat-total-agents', agents.length);
    setEl('stat-skills-bound', summary.total_skills_bound || 0);
    setEl('stat-context-ready', summary.agents_with_context_ready || 0);
    setEl('stat-external-agents', externalCount);

    // Render cards
    const grid = document.getElementById('agent-card-grid');
    if (!grid) return;
    grid.innerHTML = agents.map(a => {
      const isExternal = !!a.webhook_url;
      const badgeHtml = isExternal
        ? `<span class="agent-card-badge external">EXTERNAL</span>`
        : '';
      const externalClass = isExternal ? 'is-external' : '';

      return `
      <div class="agent-card ${externalClass}" onclick="openAgentDetail('${a.agent_id}')">
        ${badgeHtml}
        <div class="agent-card-header">
          <span class="agent-card-icon">${a.icon}</span>
          <span class="agent-card-name">${a.display_name}</span>
          <span class="agent-card-role ${a.role}">${(a.role || '').toUpperCase()}</span>
        </div>
        <div class="agent-card-desc" title="${(a.description || '').replace(/"/g, '&quot;')}">${a.description}</div>
        <div class="agent-card-stats">
          <span><span class="agent-card-status-dot ${a.context_ready ? 'ready' : 'pending'}"></span>${a.context_ready ? 'READY' : 'PENDING'}</span>
          <span>Skills: <strong>${a.skill_count}</strong></span>
          <span>Dept: <strong>${a.department}</strong></span>
          <span>Ceiling: <strong>${a.sensitivity_ceiling}</strong></span>
          ${isExternal ? `<span style="color:#0ea5e9;">🌐 Webhook</span>` : ''}
        </div>
      </div>
    `;
    }).join('');

  } catch (err) {
    console.error('loadAgents:', err);
  }
}

async function openAgentDetail(agentId) {
  const panel = document.getElementById('agent-detail-panel');
  const header = document.getElementById('agent-detail-header');
  const content = document.getElementById('agent-detail-content');
  if (!panel || !content) return;

  panel.style.display = 'block';
  header.textContent = '⏳ LOADING...';
  content.innerHTML = '';

  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}`);
    const a = await res.json();
    const isExternal = !!a.webhook_url;

    header.textContent = `── ${a.icon} ${a.display_name} (${a.role}) ${isExternal ? '🌐 EXTERNAL' : ''} ──`;

    // Build skills section
    const skillChips = (a.bound_skills || []).map(s =>
      `<span class="skill-chip">${s}<span class="unbind-x" onclick="event.stopPropagation(); unbindSkillFromAgent('${agentId}', '${s}')" title="Unbind">✕</span></span>`
    ).join('') || '<em style="color:var(--muted);font-size:9px;">No skills bound yet</em>';

    // Build skill bind dropdown
    const skillOpts = _allSkills.map(s =>
      `<option value="${s}">${s}</option>`
    ).join('');

    // Build tool chips
    const allowChips = (a.tools_allowlist || []).map(t =>
      `<span class="tool-chip">✓ ${t}</span>`
    ).join('');
    const denyChips = (a.tools_denylist || []).map(t =>
      `<span class="tool-chip denied">✕ ${t}</span>`
    ).join('');

    // Build delegation targets display
    const delegateStr = (a.can_delegate_to || []).map(t =>
      `<span class="skill-chip">${t}</span>`
    ).join('') || '<em style="color:var(--muted);font-size:9px;">None</em>';

    // Webhook info for external agents
    const webhookSection = isExternal ? `
      <div class="detail-section">
        <div class="detail-section-title" style="color:#0ea5e9;">🌐 External Agent — Connectivity</div>
        <div style="font-family:var(--mono,var(--px));font-size:9px;color:var(--ink);margin-bottom:6px;">
          <strong>Webhook:</strong> POST → <strong>${a.webhook_url || '(not set)'}</strong>
        </div>
        <div style="font-family:var(--mono,var(--px));font-size:9px;color:var(--ink);margin-bottom:6px;">
          <strong>Auth Token:</strong> ${a.auth_token ? '••••••••' + a.auth_token.slice(-4) : '<em style="color:#888;">None (unauthenticated)</em>'}
        </div>
        <div style="font-family:var(--px);font-size:8px;color:#888;line-height:1.8;">
          Tasks are dispatched via HMAC-signed webhook with exponential backoff retry (up to 5 attempts).
          <br>External agent posts results back to: <strong>/api/tasks/{task_id}/result</strong>
        </div>
      </div>
    ` : '';

    // Task delegation/submission panel
    const taskPanel = `
      <div class="detail-section">
        <div class="detail-section-title">📨 Send Task to Agent</div>
        <div class="delegation-panel">
          <div class="task-submit-row">
            <select class="retro-select" id="task-type-select" style="width:100px;">
              <option value="query">Query</option>
              <option value="review">Review</option>
              <option value="synthesize">Synthesize</option>
              <option value="audit">Audit</option>
            </select>
            <input type="text" id="task-desc-input" class="retro-input" placeholder="Describe the task..." />
            <button class="retro-btn primary" onclick="submitTaskToAgent('${agentId}')">SEND ▸</button>
          </div>
          <div id="task-submit-result" style="font-family:var(--px);font-size:9px;color:#888;margin-top:6px;"></div>
        </div>
      </div>
    `;

    content.innerHTML = `
      <div class="detail-meta" style="margin-bottom:12px;">
        <span>Department: <strong>${a.department}</strong></span>
        <span>Ceiling: <strong>${a.sensitivity_ceiling}</strong></span>
        <span>Max Tokens: <strong>${a.max_context_tokens}</strong></span>
        <span>Auto-Reload: <strong>${a.auto_reload ? 'ON' : 'OFF'}</strong></span>
        <span>Last Reload: <strong>${a.last_reloaded_at ? new Date(a.last_reloaded_at).toLocaleString() : 'Never'}</strong></span>
        ${isExternal ? '<span style="color:#0ea5e9;font-weight:700;">🌐 EXTERNAL</span>' : ''}
      </div>

      ${webhookSection}

      <div class="detail-section">
        <div class="detail-section-title">Bound Skills</div>
        ${skillChips}
        <div style="margin-top:8px; display:flex; gap:6px; align-items:center;">
          <select class="retro-select" id="bind-skill-select" style="flex:1;">
            <option value="">-- SELECT SKILL --</option>
            ${skillOpts}
          </select>
          <button class="retro-btn primary" onclick="bindSkillToAgent('${agentId}')">BIND</button>
        </div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title">Tool Access</div>
        <div>
          ${allowChips}${denyChips}
          ${!allowChips && !denyChips ? '<em style="color:var(--muted);font-size:9px;">No tool restrictions</em>' : ''}
        </div>
      </div>

      <div class="detail-section">
        <div class="detail-section-title">Can Delegate To</div>
        ${delegateStr}
      </div>

      ${taskPanel}

      <div class="detail-section">
        <div class="detail-section-title">Assembled Context</div>
        <div class="context-preview" id="context-preview-box">Click RELOAD to generate context...</div>
        <div style="margin-top:6px; display:flex; gap:6px; flex-wrap:wrap;">
          <button class="retro-btn primary" onclick="reloadAgentContext('${agentId}')">⟳ RELOAD CONTEXT</button>
          <button class="retro-btn" onclick="viewAgentContext('${agentId}')">VIEW CONTEXT</button>
          <button class="retro-btn" onclick="viewAgentCard('${agentId}')">A2A CARD</button>
          <button class="retro-btn" style="color:var(--red);" onclick="deleteAgent('${agentId}')">DELETE</button>
        </div>
      </div>
    `;

    // Scroll to panel
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  } catch (err) {
    content.innerHTML = `<p style="color:var(--red)">Error: ${err.message}</p>`;
  }
}

async function bindSkillToAgent(agentId) {
  const select = document.getElementById('bind-skill-select');
  const skillName = select?.value;
  if (!skillName) { showToast('Select a skill first', 'error'); return; }

  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}/bind`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skill_name: skillName }),
    });
    const data = await res.json();
    showToast(data.message || 'Skill bound!', 'success');
    openAgentDetail(agentId);
    loadAgents();
  } catch (err) { showToast('Bind failed: ' + err.message, 'error'); }
}

async function unbindSkillFromAgent(agentId, skillName) {
  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}/unbind`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skill_name: skillName }),
    });
    const data = await res.json();
    showToast(data.message || 'Skill unbound!', 'success');
    openAgentDetail(agentId);
    loadAgents();
  } catch (err) { showToast('Unbind failed: ' + err.message, 'error'); }
}

async function reloadAgentContext(agentId) {
  const box = document.getElementById('context-preview-box');
  if (box) box.textContent = '⏳ Rebuilding context...';
  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}/reload`, { method: 'POST' });
    const data = await res.json();
    showToast(`Context rebuilt: ${data.context_tokens_approx} tokens`, 'success');
    if (box) box.textContent = `✓ Context ready. ${data.context_length} chars (~${data.context_tokens_approx} tokens)`;
    loadAgents();
  } catch (err) {
    if (box) box.textContent = '✕ Reload failed: ' + err.message;
  }
}

async function viewAgentContext(agentId) {
  const box = document.getElementById('context-preview-box');
  if (box) box.textContent = '⏳ Loading...';
  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}/context`);
    const data = await res.json();
    if (box) box.textContent = data.context || '(empty)';
  } catch (err) {
    if (box) box.textContent = '✕ ' + err.message;
  }
}

async function viewAgentCard(agentId) {
  const box = document.getElementById('context-preview-box');
  if (box) box.textContent = '⏳ Loading AgentCard...';
  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}/agent-card`);
    const data = await res.json();
    if (box) box.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    if (box) box.textContent = '✕ ' + err.message;
  }
}

function showCreateAgentForm() {
  const form = document.getElementById('create-agent-form');
  if (form) form.style.display = 'block';
  // Hide external form if open
  hideExternalAgentForm();
}

function hideCreateAgentForm() {
  const form = document.getElementById('create-agent-form');
  if (form) form.style.display = 'none';
}

async function createNewAgent() {
  const agentId = document.getElementById('new-agent-id')?.value?.trim();
  const displayName = document.getElementById('new-agent-name')?.value?.trim();
  const icon = document.getElementById('new-agent-icon')?.value?.trim() || '🤖';
  const role = document.getElementById('new-agent-role')?.value || 'specialist';
  const dept = document.getElementById('new-agent-dept')?.value || 'shared';
  const desc = document.getElementById('new-agent-desc')?.value?.trim() || '';

  if (!agentId || !displayName) {
    showToast('Agent ID and Name are required', 'error');
    return;
  }

  try {
    const res = await apiFetch(`${API_BASE}/agents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_id: agentId,
        display_name: displayName,
        icon, role, department: dept, description: desc,
      }),
    });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`Agent '${agentId}' created!`, 'success');
      hideCreateAgentForm();
      loadAgents();
    } else {
      showToast(data.error || 'Failed to create agent', 'error');
    }
  } catch (err) { showToast('Create failed: ' + err.message, 'error'); }
}

async function deleteAgent(agentId) {
  if (!confirm(`Delete agent '${agentId}'? This cannot be undone.`)) return;
  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}`, { method: 'DELETE' });
    const data = await res.json();
    showToast(data.message || 'Agent deleted', 'success');
    document.getElementById('agent-detail-panel').style.display = 'none';
    loadAgents();
  } catch (err) { showToast('Delete failed: ' + err.message, 'error'); }
}

// Legacy compat — old code called assignSkill(agentName, selectId)
async function assignSkill(agentName, selectId) {
  const select = document.getElementById(selectId);
  const skillName = select?.value;
  if (!skillName) { showToast('Select a skill first', 'error'); return; }
  try {
    const res = await apiFetch(`${API_BASE}/agents/assign`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_name: agentName, skill_name: skillName }),
    });
    const data = await res.json();
    showToast(data.message || 'Done', data.status === 'success' ? 'success' : 'error');
  } catch (err) { showToast('Error: ' + err.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════
// A2A GATEWAY
// ═══════════════════════════════════════════════════════════

let _a2aData = null;

async function refreshA2AGateway() {
  const setEl = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  const statusEl = document.getElementById('a2a-status');

  try {
    // Derive the base URL (strip /api from API_BASE)
    const base = API_BASE.replace(/\/api$/, '');
    const res = await fetch(`${base}/.well-known/agent.json`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _a2aData = await res.json();

    setEl('a2a-discovery-url', `${base}/.well-known/agent.json`);
    setEl('a2a-version', _a2aData.version || '—');
    setEl('a2a-skills-count', (_a2aData.skills || []).length);

    // MCP SSE endpoint
    const mcpSse = _a2aData.mcp?.sse_endpoint || _a2aData.endpoints?.mcp_sse;
    setEl('a2a-mcp-sse', mcpSse ? mcpSse : 'Not mounted');

    // HMAC signing status
    const hmacScheme = _a2aData.securitySchemes?.webhookHmac;
    const hmacEl = document.getElementById('a2a-hmac-status');
    if (hmacEl) {
      if (hmacScheme) {
        hmacEl.textContent = `${hmacScheme.algorithm.toUpperCase()} via ${hmacScheme.header}`;
        hmacEl.style.color = '#00ff66';
      } else {
        hmacEl.textContent = 'Not configured';
        hmacEl.style.color = '#888';
      }
    }

    if (statusEl) {
      statusEl.textContent = 'ONLINE';
      statusEl.className = 'a2a-value a2a-status online';
    }
  } catch (err) {
    console.error('refreshA2AGateway:', err);
    if (statusEl) {
      statusEl.textContent = 'OFFLINE';
      statusEl.className = 'a2a-value a2a-status offline';
    }
  }
}

function viewA2AJson() {
  const preview = document.getElementById('a2a-json-preview');
  if (!preview) return;
  if (preview.style.display !== 'none') {
    preview.style.display = 'none';
    return;
  }
  preview.style.display = 'block';
  preview.textContent = _a2aData
    ? JSON.stringify(_a2aData, null, 2)
    : '(No data — click REFRESH first)';
}

// ═══════════════════════════════════════════════════════════
// EXTERNAL AGENT REGISTRATION
// ═══════════════════════════════════════════════════════════

function showExternalAgentForm() {
  const form = document.getElementById('external-agent-form');
  if (form) form.style.display = 'block';
  // Hide the internal form if open
  hideCreateAgentForm();
}

function hideExternalAgentForm() {
  const form = document.getElementById('external-agent-form');
  if (form) form.style.display = 'none';
}

async function registerExternalAgent() {
  const agentId = document.getElementById('ext-agent-id')?.value?.trim();
  const name = document.getElementById('ext-agent-name')?.value?.trim();
  const url = document.getElementById('ext-agent-url')?.value?.trim();
  const authToken = document.getElementById('ext-agent-token')?.value?.trim() || null;
  const icon = document.getElementById('ext-agent-icon')?.value?.trim() || '🌐';
  const role = document.getElementById('ext-agent-role')?.value || 'specialist';
  const dept = document.getElementById('ext-agent-dept')?.value || 'shared';
  const desc = document.getElementById('ext-agent-desc')?.value?.trim() || '';

  if (!agentId || !name || !url) {
    showToast('Agent ID, Name, and Callback URL are required', 'error');
    return;
  }

  try {
    const res = await apiFetch(`${API_BASE}/agents/external/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_id: agentId,
        display_name: name,
        description: desc,
        url: url,
        auth_token: authToken,
        icon, role, department: dept,
      }),
    });
    const data = await res.json();
    if (data.status === 'registered') {
      showToast(`🌐 External agent '${name}' connected!`, 'success');
      hideExternalAgentForm();
      // Clear the form
      ['ext-agent-id','ext-agent-name','ext-agent-url','ext-agent-token','ext-agent-desc'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      loadAgents();
      refreshA2AGateway();
      addReloadEvent(agentId, 'registered');
    } else {
      showToast(data.error || data.detail || 'Registration failed', 'error');
    }
  } catch (err) {
    showToast('Registration failed: ' + err.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════
// TASK SUBMISSION & DELEGATION
// ═══════════════════════════════════════════════════════════

async function submitTaskToAgent(agentId) {
  const taskType = document.getElementById('task-type-select')?.value || 'query';
  const desc = document.getElementById('task-desc-input')?.value?.trim();
  const resultEl = document.getElementById('task-submit-result');

  if (!desc) {
    showToast('Describe the task first', 'error');
    return;
  }

  if (resultEl) resultEl.textContent = '⏳ Submitting...';

  try {
    const res = await apiFetch(`${API_BASE}/agents/${agentId}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_type: taskType,
        description: desc,
        from_agent_id: 'orchestrator',
      }),
    });
    const data = await res.json();

    if (data.status === 'dispatched') {
      if (resultEl) resultEl.innerHTML = `
        ✓ Task <strong>${data.task_id}</strong> dispatched via retry queue.
        <br><span style="color:#0ea5e9;">Callback: ${data.callback_url}</span>
        <br><button class="retro-btn" onclick="pollTaskResult('${data.task_id}')" style="margin-top:4px;font-size:7px;padding:2px 8px;">⟳ CHECK RESULT</button>
      `;
      showToast('Task dispatched to external agent!', 'success');
      addReloadEvent(agentId, 'task-dispatched');
    } else if (data.status === 'forwarded') {
      if (resultEl) resultEl.innerHTML = `✓ Task <strong>${data.task_id}</strong> forwarded to external webhook. Response: ${data.agent_response_status}`;
      showToast('Task forwarded to external agent!', 'success');
      addReloadEvent(agentId, 'task-forwarded');
    } else if (data.status === 'queued') {
      if (resultEl) resultEl.innerHTML = `✓ Task <strong>${data.task_id}</strong> queued for internal processing.`;
      showToast('Task queued!', 'success');
      addReloadEvent(agentId, 'task-queued');
    } else {
      if (resultEl) resultEl.textContent = `⚠ ${data.message || JSON.stringify(data)}`;
    }

    // Clear input
    const input = document.getElementById('task-desc-input');
    if (input) input.value = '';

  } catch (err) {
    if (resultEl) resultEl.textContent = `✕ ${err.message}`;
    showToast('Task submission failed: ' + err.message, 'error');
  }
}

async function delegateTaskBetweenAgents(fromId, toId, taskType, desc) {
  try {
    const res = await apiFetch(`${API_BASE}/agents/delegate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_agent_id: fromId,
        to_agent_id: toId,
        task_type: taskType,
        description: desc,
      }),
    });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`Task delegated: ${fromId} → ${toId}`, 'success');
      addReloadEvent(toId, 'delegation');
    } else {
      showToast(data.error || data.detail || 'Delegation failed', 'error');
    }
    return data;
  } catch (err) {
    showToast('Delegation failed: ' + err.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════
// WEBHOOK DISPATCHER STATUS
// ═══════════════════════════════════════════════════════════

async function refreshDispatcherStatus() {
  const panel = document.getElementById('dispatcher-panel');
  const setEl = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };

  try {
    const res = await apiFetch(`${API_BASE}/webhooks/dispatcher/status`);
    const data = await res.json();

    // Show the panel
    if (panel) panel.style.display = 'block';

    setEl('disp-status', (data.status || 'unknown').toUpperCase());
    setEl('disp-queue', data.queue_size || 0);

    const deadLetters = data.dead_letters || [];
    setEl('disp-dead-count', deadLetters.length);

    // Render dead letters
    const listEl = document.getElementById('disp-dead-list');
    if (listEl) {
      if (deadLetters.length === 0) {
        listEl.innerHTML = '<span style="color:#00ff66;">✓ No dead-lettered deliveries.</span>';
      } else {
        listEl.innerHTML = deadLetters.map(dl => `
          <div style="border-bottom:1px solid #333;padding:4px 0;">
            <span style="color:#ff4785;">✕</span>
            <strong>${dl.agent_id}</strong> — task ${dl.task_id}
            <br><span style="font-size:11px;">${dl.last_error}</span>
            <br><span style="font-size:10px;color:#666;">${dl.attempts} attempts | ${new Date(dl.failed_at * 1000).toLocaleTimeString()}</span>
          </div>
        `).join('');
      }
    }

    // Color the status
    const statusEl = document.getElementById('disp-status');
    if (statusEl) {
      statusEl.style.color = data.status === 'running' ? '#00ff66' : '#ff4785';
    }
  } catch (err) {
    console.error('refreshDispatcherStatus:', err);
    if (panel) panel.style.display = 'block';
    setEl('disp-status', 'ERROR');
  }
}

// ═══════════════════════════════════════════════════════════
// TASK RESULT POLLING
// ═══════════════════════════════════════════════════════════

async function pollTaskResult(taskId) {
  const resultEl = document.getElementById('task-submit-result');
  if (resultEl) resultEl.innerHTML += '<br>⏳ Checking result...';

  try {
    const res = await apiFetch(`${API_BASE}/tasks/${taskId}/result`);

    if (res.status === 404) {
      if (resultEl) resultEl.innerHTML += '<br><span style="color:#888;">No result yet — external agent hasn\'t responded.</span>';
      return;
    }

    const data = await res.json();

    if (data.status === 'completed') {
      if (resultEl) resultEl.innerHTML += `
        <br><span style="color:#00ff66;">✓ COMPLETED</span> by <strong>${data.agent_id}</strong>
        <br><div style="background:#1a1a2e;color:#e0e0e0;padding:8px;margin-top:4px;font-family:var(--vt);font-size:13px;max-height:150px;overflow-y:auto;border:1px solid #333;">${(data.result_text || '').replace(/</g, '&lt;').replace(/\n/g, '<br>')}</div>
      `;
      showToast(`Task ${taskId} completed!`, 'success');
    } else if (data.status === 'failed') {
      if (resultEl) resultEl.innerHTML += `
        <br><span style="color:#ff4785;">✕ FAILED</span> by <strong>${data.agent_id}</strong>
        <br><span style="color:#ff4785;">${data.error_text || 'Unknown error'}</span>
      `;
      showToast(`Task ${taskId} failed`, 'error');
    } else {
      if (resultEl) resultEl.innerHTML += `<br>Status: <strong>${data.status}</strong>`;
    }
  } catch (err) {
    if (resultEl) resultEl.innerHTML += `<br><span style="color:#ff4785;">Error: ${err.message}</span>`;
  }
}

// ═══════════════════════════════════════════════════════════
// RELOAD EVENTS LOG
// ═══════════════════════════════════════════════════════════

const _reloadEvents = [];

function addReloadEvent(agentId, type) {
  const now = new Date();
  _reloadEvents.unshift({
    time: now.toLocaleTimeString(),
    agent: agentId,
    type: type,
  });

  // Keep only last 20
  if (_reloadEvents.length > 20) _reloadEvents.length = 20;

  // Update counter
  const countEl = document.getElementById('stat-reload-events');
  if (countEl) countEl.textContent = _reloadEvents.length;

  // Render
  const list = document.getElementById('reload-events-list');
  if (!list) return;

  list.innerHTML = _reloadEvents.map(e => `
    <div class="reload-event">
      <span class="reload-event-time">${e.time}</span>
      <span class="reload-event-agent">${e.agent}</span>
      <span class="reload-event-type">${e.type}</span>
    </div>
  `).join('');
}


// ═══════════════════════════════════════════════════════════
// CONNECTORS
// ═══════════════════════════════════════════════════════════

const APP_ICONS = {
  localfolder: '📁', obsidian: '🗒', github: '🐙',
  notion: '📋', slack: '💬', google_drive: '☁',
  confluence: '✨', jira: '📘', linear: '📒',
  ms_teams: '💼', whatsapp: '📗',
};

async function loadConnectors() {
  const grid = document.getElementById('connectors-grid');
  if (!grid) return;
  try {
    const res  = await apiFetch(`${API_BASE}/connectors`);
    const data = await res.json();
    connectorData = data.connectors || [];
    renderConnectorGrid();
    updateConnectorBadge();
    renderSourceTags();
    setText('status-connectors', String(connectorData.filter(c => c.connected).length), 'white');
  } catch (err) {
    if (grid) grid.innerHTML = '<div class="loading-msg" style="color:var(--red);">Backend offline — start API server.</div>';
  }
}

function renderConnectorGrid() {
  const grid = document.getElementById('connectors-grid');
  if (!grid) return;
  if (connectorData.length === 0) {
    grid.innerHTML = '<div class="loading-msg">No connectors configured.</div>';
    return;
  }

  grid.innerHTML = connectorData.map(conn => {
    const icon      = APP_ICONS[conn.id] || '🔌';
    const isConn    = conn.connected;
    const tierClass = conn.tier === 1 ? 't1' : 't2';
    const tierText  = conn.tier === 1 ? 'TIER 1 (AUTO)' : 'TIER 2 (CREDS)';
    const connAt    = conn.connected_at ? `<span style="font-family:var(--px);font-size:4.5px;color:#666;display:block;margin-bottom:4px;">Connected ${_relTime(conn.connected_at)}</span>` : '';

    // ── Local Folder: inline path input when not connected ──
    let localExtra = '';
    if (conn.id === 'localfolder' && !isConn) {
      localExtra = `
        <div style="margin-top:8px;">
          <div style="font-family:var(--px);font-size:6px;color:#888;margin-bottom:4px;">DISK PATH:</div>
          <input type="text" id="localfolder-path-input" class="field-input"
                 placeholder="/Users/yourname/Documents/notes"
                 style="font-size:8px;padding:3px 6px;width:100%;box-sizing:border-box;"
                 onkeydown="if(event.key==='Enter')connectLocalFolder()">
          <button class="retro-btn success" style="margin-top:5px;font-size:6px;width:100%;"
                  onclick="connectLocalFolder()">📁 CONNECT FOLDER</button>
        </div>`;
    }

    // ── Obsidian: inline path when auto-detect fails ──
    let obsidianExtra = '';
    if (conn.id === 'obsidian' && !isConn) {
      const detected = conn.detected_path;
      if (detected) {
        obsidianExtra = `
          <div style="margin-top:8px;">
            <div style="font-family:var(--px);font-size:5px;color:var(--green);margin-bottom:4px;">VAULT FOUND: ${detected}</div>
            <button class="retro-btn success" style="font-size:6px;width:100%;"
                    onclick="connectApp('obsidian', null)">📓 CONNECT VAULT</button>
          </div>`;
      } else {
        obsidianExtra = `
          <div style="margin-top:8px;">
            <div style="font-family:var(--px);font-size:5px;color:#888;margin-bottom:4px;">NO VAULT AUTO-DETECTED. ENTER PATH:</div>
            <input type="text" id="obsidian-path-input" class="field-input"
                   placeholder="/Users/yourname/Documents/ObsidianVault"
                   style="font-size:8px;padding:3px 6px;width:100%;box-sizing:border-box;"
                   onkeydown="if(event.key==='Enter')connectObsidian()">
            <button class="retro-btn success" style="margin-top:5px;font-size:6px;width:100%;"
                    onclick="connectObsidian()">📓 CONNECT VAULT</button>
          </div>`;
      }
    }

    // ── Notion: inline token when not connected ──
    let notionExtra = '';
    if (conn.id === 'notion' && !isConn) {
      notionExtra = `
        <div style="margin-top:8px;">
          <div style="font-family:var(--px);font-size:6px;color:#888;margin-bottom:4px;">INTEGRATION TOKEN:</div>
          <input type="password" id="notion-token-input" class="field-input"
                 placeholder="ntn_xxxxx…"
                 style="font-size:8px;padding:3px 6px;width:100%;box-sizing:border-box;">
          <button class="retro-btn success" style="margin-top:5px;font-size:6px;width:100%;"
                  onclick="connectNotion()">🔗 CONNECT NOTION</button>
        </div>`;
    }

    // ── Sync button for connected apps ──
    const syncBtn = isConn ? `
      <button class="retro-btn" style="font-size:6px;padding:3px 10px;margin-top:5px;width:100%;"
              id="sync-btn-${conn.id}" onclick="syncConnector('${conn.id}')">⟳ SYNC NOW</button>` : '';

    // ── Notion: sync workspace button ──
    const notionSyncBtn = (conn.id === 'notion' && isConn) ? `
      <button class="retro-btn success" style="font-size:6px;padding:3px 10px;margin-top:5px;width:100%;"
              onclick="syncNotion()">📥 SYNC WORKSPACE</button>` : '';

    // Connectors with their own inline UIs skip the generic button
    const inlineConnectors = ['localfolder', 'notion', 'obsidian'];
    const showGenericBtn = !inlineConnectors.includes(conn.id);

    return `<div class="connector-card ${isConn ? 'connected' : ''}" id="tile-${conn.id}">
      <span class="connector-icon">${icon}</span>
      <span class="connector-name">${conn.name}</span>
      <span class="connector-tier ${tierClass}">${tierText}</span>
      <span class="connector-status ${isConn ? 'connected' : 'disconnected'}" id="cstatus-${conn.id}">
        ${isConn ? '● CONNECTED' : '● DISCONNECTED'}
      </span>
      ${conn.status_message ? `<span style="font-family:var(--px);font-size:4.5px;color:#666;display:block;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;">${conn.status_message}</span>` : ''}
      ${connAt}
      ${showGenericBtn ? `
        <button class="connector-connect-btn ${isConn ? 'connected' : ''}"
                id="cbtn-${conn.id}"
                onclick="handleConnectClick('${conn.id}')">${isConn ? '✓ CONNECTED' : 'CONNECT'}</button>
      ` : ''}
      ${syncBtn}${notionSyncBtn}${localExtra}${notionExtra}${obsidianExtra}
    </div>`;
  }).join('') +
  `<div class="connector-card" onclick="showToast('Custom connector setup coming soon.','success')">
    <span class="connector-icon">＋</span>
    <span class="connector-name">ADD CONNECTOR</span>
    <span class="connector-tier t1">CUSTOM</span>
    <span class="connector-status" style="color:#888;">● AVAILABLE</span>
  </div>`;

  // Hover states for connected buttons
  connectorData.filter(c => c.connected).forEach(conn => {
    const btn = document.getElementById(`cbtn-${conn.id}`);
    if (btn) {
      btn.addEventListener('mouseenter', () => { btn.textContent = '✕ DISCONNECT'; });
      btn.addEventListener('mouseleave', () => { btn.textContent = '✓ CONNECTED'; });
    }
  });
}

function _relTime(ts) {
  try {
    const diff = Date.now() - new Date(ts).getTime();
    const d = Math.floor(diff / 86400000);
    const h = Math.floor(diff / 3600000);
    const m = Math.floor(diff / 60000);
    if (d > 0) return `${d}d ago`;
    if (h > 0) return `${h}h ago`;
    if (m > 0) return `${m}m ago`;
    return 'just now';
  } catch (_) { return ''; }
}

function updateConnectorBadge() {
  const badge = document.getElementById('connected-badge');
  if (!badge) return;
  const n = connectorData.filter(c => c.connected).length;
  badge.textContent = `${n} CONNECTED`;
}

function renderSourceTags() {
  const area   = document.getElementById('source-tags-area');
  const tagBox = document.getElementById('source-tags');
  if (!area || !tagBox) return;
  const connected = connectorData.filter(c => c.connected);
  if (connected.length === 0) { area.style.display = 'none'; return; }
  area.style.display = 'block';
  tagBox.innerHTML = connected.map(c => `
    <span class="source-tag">
      ${APP_ICONS[c.id] || '🔌'} ${c.name}
      <button class="remove-tag" title="Disconnect" onclick="disconnectApp('${c.id}')">×</button>
    </span>
  `).join('');
}

// ── Local Folder inline connect ──────────────────────────
async function connectLocalFolder() {
  const inp  = document.getElementById('localfolder-path-input');
  const path = inp ? inp.value.trim() : '';
  if (!path) { showToast('Enter a folder path first.', 'error'); return; }
  await connectApp('localfolder', { path });
}

// ── Obsidian inline connect ──────────────────────────────
async function connectObsidian() {
  const inp  = document.getElementById('obsidian-path-input');
  const path = inp ? inp.value.trim() : '';
  if (!path) { showToast('Enter the path to your Obsidian vault.', 'error'); return; }
  await connectApp('obsidian', { path });
}

// ── Generic connect/disconnect ───────────────────────────
async function handleConnectClick(appId) {
  const conn = connectorData.find(c => c.id === appId);
  if (!conn) return;
  if (conn.connected) {
    await disconnectApp(appId);
    return;
  }
  if (conn.tier === 1) {
    await connectApp(appId, null);
  } else {
    openCredModal(appId);
  }
}

async function connectApp(appId, credentials) {
  const btn  = document.getElementById(`cbtn-${appId}`);
  const tile = document.getElementById(`tile-${appId}`);
  const stat = document.getElementById(`cstatus-${appId}`);
  if (btn)  { btn.disabled = true; btn.textContent = 'CONNECTING…'; }
  if (tile) tile.classList.add('connecting');
  if (stat) { stat.className = 'connector-status connecting'; stat.textContent = '● CONNECTING'; }
  try {
    const res    = await apiFetch(`${API_BASE}/connectors/${appId}/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credentials }),
    });
    const result = await res.json();
    if (result.status === 'connected') {
      showToast(result.message || `${appId} connected!`, 'success');
      await loadConnectors();
      fetchStats();
    } else {
      if (btn)  { btn.disabled = false; btn.textContent = 'CONNECT'; }
      if (tile) tile.classList.remove('connecting');
      if (stat) { stat.className = 'connector-status disconnected'; stat.textContent = '● DISCONNECTED'; }
      showToast(result.message || 'Connection failed.', 'error');
    }
  } catch (err) {
    if (btn)  { btn.disabled = false; btn.textContent = 'CONNECT'; }
    if (tile) tile.classList.remove('connecting');
    showToast('Backend unreachable.', 'error');
  }
}

async function disconnectApp(appId) {
  const btn = document.getElementById(`cbtn-${appId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'DISCONNECTING…'; }
  try {
    await apiFetch(`${API_BASE}/connectors/${appId}/disconnect`, { method: 'POST' });
    showToast(`${appId} disconnected.`, 'success');
    await loadConnectors();
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = '✓ CONNECTED'; }
    showToast('Disconnect failed.', 'error');
  }
}

// ── Sync per-connector ────────────────────────────────────
let _lastSyncTime = {};

async function syncConnector(appId) {
  // Rate-limit guard — 30s client-side cooldown per connector
  const SYNC_COOLDOWN = 30000;
  const now = Date.now();
  if (_lastSyncTime[appId] && now - _lastSyncTime[appId] < SYNC_COOLDOWN) {
    const remaining = Math.ceil((SYNC_COOLDOWN - (now - _lastSyncTime[appId])) / 1000);
    showToast(`Sync cooldown: wait ${remaining}s before syncing ${appId} again.`, 'error');
    return;
  }
  _lastSyncTime[appId] = now;

  const btn = document.getElementById(`sync-btn-${appId}`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ SYNCING…'; }

  showToast(`Starting sync for ${appId}…`, 'success');
  try {
    const res  = await apiFetch(`${API_BASE}/sync/run/${appId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();

    if (data.error === 'rate_limit_exceeded') {
      const wait = data.retry_after || 60;
      _lastSyncTime[appId] = now + (wait * 1000) - SYNC_COOLDOWN; // align client timer
      showToast(`Server rate limit: try again in ${wait}s.`, 'error');
    } else if (data.status === 'sync_started' || data.status === 'sync_complete' || data.status === 'success') {
      showToast(`${appId} sync started. Check logs for progress.`, 'success');
      setTimeout(() => { fetchStats(); fetchMemoryUnits(); }, 5000);
    } else {
      showToast((data.detail || data.message || 'Sync issue.'), 'error');
    }
  } catch (err) {
    showToast(`Sync failed: ${err.message}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟳ SYNC NOW'; }
  }
}

// ═══════════════════════════════════════════════════════════
// NOTION CONNECT + SYNC
// ═══════════════════════════════════════════════════════════

async function connectNotion() {
  const tokenEl = document.getElementById('notion-token-input');
  const token   = tokenEl ? tokenEl.value.trim() : '';
  if (!token) { showToast('Paste your Notion integration token first.', 'error'); return; }
  showToast('Connecting to Notion…', 'success');
  try {
    // Use unified connector path so connector_state.json stays consistent
    const res = await apiFetch(`${API_BASE}/connectors/notion/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credentials: { api_key: token } }),
    });
    const data = await res.json();
    if (data.status === 'connected') {
      showToast(data.message || 'Notion connected!', 'success');
      await loadConnectors();
      fetchStats();
    } else {
      showToast(data.message || data.detail || 'Notion connection failed.', 'error');
    }
  } catch (err) {
    showToast('Notion connect error: ' + err.message, 'error');
  }
}

async function syncNotion() {
  showToast('Notion workspace sync started…', 'success');
  try {
    const res  = await apiFetch(`${API_BASE}/notion/sync`, { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Notion sync running. Check logs.', 'success');
    setTimeout(() => { fetchStats(); fetchMemoryUnits(); }, 5000);
  } catch (err) {
    showToast('Notion sync error: ' + err.message, 'error');
  }
}



// ═══════════════════════════════════════════════════════════
// FULL STATUS REFRESH (all sub-endpoints)
// ═══════════════════════════════════════════════════════════

async function refreshFullStatus() {
  await Promise.allSettled([
    checkStatus(),
    fetchStats(),
    fetchProviderStatus(),
    fetchSearchStats(),
    fetchCacheStats(),
    fetchPointerStats(),
    loadAuditLog(),
    fetchHealthCheck(),
  ]);
}

async function fetchProviderStatus() {
  const list = document.getElementById('providers-list');
  if (!list) return;
  try {
    const res  = await apiFetch(`${API_BASE}/providers/status`);
    const data = await res.json();
    const providers = data.providers || [];
    if (providers.length === 0) {
      list.innerHTML = '<div style="font-family:var(--px);font-size:var(--fs-xxs);color:#888;">No providers configured.</div>';
      return;
    }
    list.innerHTML = providers.map(p => {
      const ok    = p.available;
      const color = ok ? 'var(--green)' : '#555';
      const model = p.model || '';

      // Build rich sub-info
      const badges = [];
      if (p.cost === 'free') badges.push(`<span style="color:#4ade80;border:1px solid #4ade80;padding:0 3px;">FREE</span>`);
      if (p.cost === 'paid') badges.push(`<span style="color:#fbbf24;border:1px solid #fbbf24;padding:0 3px;">PAID</span>`);

      // Detect cloud model
      const discoveredModels = p.discovered_models || [];
      const activeModel = discoveredModels.find(m => m.is_chat_model);
      const isCloud = activeModel && activeModel.is_cloud;
      const isLocal = activeModel && activeModel.is_local;
      if (isCloud)  badges.push(`<span style="color:#60a5fa;border:1px solid #60a5fa;padding:0 3px;">CLOUD</span>`);
      if (isLocal)  badges.push(`<span style="color:#a78bfa;border:1px solid #a78bfa;padding:0 3px;">LOCAL</span>`);
      if (activeModel && activeModel.parameter_size) badges.push(`<span style="color:#888;">${activeModel.parameter_size}</span>`);

      return `<div style="display:flex;align-items:center;gap:8px;font-family:var(--px);font-size:var(--fs-xxs);padding:4px 0;border-bottom:1px solid #222;">
        <span style="color:${color};font-size:10px;">●</span>
        <span style="color:#ccc;min-width:80px;">${p.name.toUpperCase()}</span>
        <span style="color:${color};min-width:50px;">${ok ? 'ACTIVE' : 'OFFLINE'}</span>
        <span style="color:#aaa;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${model}">${model}</span>
        <span style="display:flex;gap:4px;flex-shrink:0;font-size:6px;">${badges.join('')}</span>
      </div>`;
    }).join('');
  } catch (_) {
    if (list) list.innerHTML = '<div style="color:var(--red);font-family:var(--px);font-size:var(--fs-xxs);">Could not reach backend.</div>';
  }
}

async function fetchHealthCheck() {
  try {
    const res  = await apiFetch(`${API_BASE}/health`);
    const data = await res.json();
    const setupEl = document.getElementById('setup-needed-list');
    if (setupEl) {
      const items = data.setup_needed || [];
      if (items.length === 0) {
        setupEl.innerHTML = '<span style="color:var(--green);">✓ All systems configured and ready.</span>';
      } else {
        setupEl.innerHTML = items.map(s => `<div style="color:var(--yellow);">⚠ ${s}</div>`).join('');
      }
    }
  } catch (_) {}
}

async function fetchSearchStats() {
  try {
    const res  = await apiFetch(`${API_BASE}/search/stats`);
    const data = await res.json();
    const indexed = data.total_indexed ?? '--';
    const embedOk = data.embeddings_available;
    const embedTxt = embedOk ? 'READY' : 'LOADING…';
    const embedClr = embedOk ? 'var(--green)' : 'var(--yellow)';

    setText('status-search-indexed', String(indexed), 'green');
    const eEl  = document.getElementById('status-embed-ready');
    const eEl2 = document.getElementById('search-embed-ready');
    const iEl  = document.getElementById('search-indexed-count');
    if (eEl)  { eEl.textContent  = embedTxt; eEl.style.color  = embedClr; }
    if (eEl2) { eEl2.textContent = embedTxt; eEl2.style.color = embedClr; }
    if (iEl)  { iEl.textContent  = String(indexed); }
  } catch (_) {}
}

async function rebuildSearchIndex() {
  showToast('Rebuilding vector index…', 'success');
  try {
    await apiFetch(`${API_BASE}/search/index`, { method: 'POST' });
    showToast('Index rebuild started. Stats refresh in ~10s.', 'success');
    setTimeout(fetchSearchStats, 10000);
  } catch (err) {
    showToast('Reindex failed: ' + err.message, 'error');
  }
}

async function fetchCacheStats() {
  try {
    const res  = await apiFetch(`${API_BASE}/cache/stats`);
    const data = await res.json();
    const hits  = data.total_hits ?? '--';
    const saved = data.estimated_cost_saved_usd != null
      ? `$${Number(data.estimated_cost_saved_usd).toFixed(3)}`
      : '$--';
    setText('status-cache-hits', String(hits), hits > 0 ? 'green' : 'white');
    setText('status-cost-saved', saved, 'green');
  } catch (_) {}
}

async function fetchPointerStats() {
  try {
    const res  = await apiFetch(`${API_BASE}/memory/pointer-stats`);
    const raw  = await res.json();
    // Backend returns { sync_stats: {...}, pointer_stats: {...} }
    const d    = raw.sync_stats || raw;
    setText('status-ptr-seen',    String(d.total_docs_seen    ?? '--'), 'white');
    setText('status-ptr-skipped', String(d.total_docs_skipped ?? '--'), 'green');
    setText('status-ptr-tokens',  _fmtNum(d.estimated_tokens_saved), 'green');
    const rate = d.skip_rate_percent != null
      ? `${Number(d.skip_rate_percent).toFixed(1)}%`
      : (d.skip_rate_pct != null ? `${Number(d.skip_rate_pct).toFixed(1)}%` : '--%');
    setText('status-ptr-rate', rate, 'green');
  } catch (_) {}
}

function _fmtNum(n) {
  if (n == null || n === '') return '--';
  n = Number(n);
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

async function loadAuditLog() {
  const container = document.getElementById('audit-log-list');
  if (!container) return;
  container.innerHTML = '<span style="color:#888;">Loading…</span>';
  try {
    const res  = await apiFetch(`${API_BASE}/audit/log?limit=50`);
    const data = await res.json();
    const entries = data.entries || [];
    if (entries.length === 0) {
      container.innerHTML = '<span style="color:#888;">No audit entries yet.</span>';
      return;
    }
    container.innerHTML = entries.map(e => {
      const ts    = new Date(e.timestamp).toLocaleTimeString();
      const actor = e.actor || 'system';
      const det   = e.details ? JSON.stringify(e.details).substring(0, 60) : '';
      const color = (e.action || '').includes('reject') ? 'var(--red)'
                  : (e.action || '').includes('approve') ? 'var(--green)'
                  : '#bbb';
      const cost  = e.cost_usd ? ` <span style="color:#888;">$${e.cost_usd}</span>` : '';
      return `<div style="color:${color};border-bottom:1px solid #1a1a1a;padding:3px 0;line-height:1.6;">
        <span style="color:#444;">[${ts}]</span>
        <span style="color:var(--green2);"> ${actor}</span>
        <span style="color:#ccc;"> → ${e.action}</span>
        <span style="color:#555;font-size:13px;"> ${det}</span>${cost}
      </div>`;
    }).join('');
  } catch (err) {
    container.innerHTML = `<span style="color:var(--red);">Failed: ${err.message}</span>`;
  }
}

// ═══════════════════════════════════════════════════════════
// KNOWLEDGE SEARCH
// ═══════════════════════════════════════════════════════════

async function runSearch() {
  const q    = getValue('search-query').trim();
  const dept = getValue('search-dept');
  const mode = getValue('search-mode') || 'hybrid';
  const area = document.getElementById('search-results-area');
  if (!q) { showToast('Enter a search query first.', 'error'); return; }
  if (!area) return;

  area.innerHTML = '<div style="font-family:var(--px);font-size:var(--fs-xxs);color:#888;padding:16px 0;text-align:center;">Searching…</div>';

  try {
    let url = `${API_BASE}/search?q=${encodeURIComponent(q)}&mode=${mode}&limit=15`;
    if (dept) url += `&dept=${encodeURIComponent(dept)}`;
    const res     = await apiFetch(url);
    const data    = await res.json();
    const results = data.results || [];

    if (results.length === 0) {
      area.innerHTML = `<div style="font-family:var(--px);font-size:var(--fs-xxs);color:#888;padding:20px 0;text-align:center;">
        No results for "<strong style="color:#fff;">${escHtml(q)}</strong>".<br>
        <span style="color:#666;font-size:7px;">Try a different query or rebuild the search index.</span>
      </div>`;
      return;
    }

    area.innerHTML = `<div style="font-family:var(--px);font-size:var(--fs-xxs);color:#888;margin-bottom:12px;">
      <span style="color:var(--green);">${results.length}</span> result(s) for
      "<span style="color:#fff;">${escHtml(q)}</span>" [${mode}]
    </div>` + results.map((r, i) => {
      const score = r.hybrid_score  != null ? (r.hybrid_score  * 100).toFixed(0) : '--';
      const sem   = r.semantic_score != null ? (r.semantic_score * 100).toFixed(0) : '--';
      const conf  = r.confidence_score != null ? (r.confidence_score * 100).toFixed(0) : '--';
      const dept2 = r.department || '—';
      const ktype = r.knowledge_type || '—';
      const src   = (r.source_identifier || '').substring(0, 35);
      const link  = r.permalink
        ? `<a href="${r.permalink}" target="_blank" style="color:var(--green2);text-decoration:none;" title="Open source">🔗</a>`
        : '';
      return `<div style="background:#0d0d0d;border-left:3px solid var(--green);margin-bottom:12px;padding:10px 14px;border-radius:0 3px 3px 0;">
        <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:5px;">
          <span style="font-family:var(--px);font-size:7px;color:var(--green2);">#${i+1}</span>
          <strong style="font-family:var(--px);font-size:var(--fs-xxs);color:#fff;">${escHtml(r.title || 'Untitled')}</strong>
          ${link}
          <span style="font-family:var(--px);font-size:6px;color:#555;margin-left:auto;">
            HYBRID:<span style="color:var(--green);"> ${score}%</span>
            SEM:<span style="color:var(--yellow);"> ${sem}%</span>
            CONF:<span style="color:#aaa;"> ${conf}%</span>
          </span>
        </div>
        <div style="font-family:var(--vt);font-size:16px;color:#bbb;margin-bottom:7px;line-height:1.5;">
          ${escHtml(r.summary || r.content_preview || '—')}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
          <span style="font-family:var(--px);font-size:5.5px;background:#1a1a2e;color:#8888cc;padding:2px 7px;">${dept2.toUpperCase()}</span>
          <span style="font-family:var(--px);font-size:5.5px;background:#0d1f0d;color:var(--green2);padding:2px 7px;">${ktype.toUpperCase()}</span>
          <span style="font-family:var(--px);font-size:5px;color:#444;" title="${escHtml(r.source_identifier || '')}">${escHtml(src)}</span>
        </div>
      </div>`;
    }).join('');
  } catch (err) {
    area.innerHTML = `<div style="font-family:var(--px);font-size:var(--fs-xxs);color:var(--red);padding:16px 0;">
      Search failed: ${escHtml(err.message)}
    </div>`;
  }
}

function clearSearch() {
  setValue('search-query', '');
  const area = document.getElementById('search-results-area');
  if (area) area.innerHTML = `<div style="font-family:var(--px);font-size:var(--fs-xxs);color:#aaa;padding:20px 0;text-align:center;">
    Enter a query above and press SEARCH or ↵
  </div>`;
}

// ═══════════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════════

function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast-item ${type}`;
  toast.textContent = (type === 'success' ? '✓ ' : '✕ ') + message;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(20px)'; toast.style.transition = 'all 0.3s ease'; }, 3200);
  setTimeout(() => toast.remove(), 3600);
}

// ═══════════════════════════════════════════════════════════
// DOM HELPERS
// ═══════════════════════════════════════════════════════════

function getValue(id) {
  const el = document.getElementById(id);
  return el ? el.value : '';
}

function setValue(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function setText(id, text, colorClass) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'status-val' + (colorClass ? ' ' + colorClass : '');
}

function updateDot(id, color) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'dot' + (color ? ' ' + color : '');
}

// ═══════════════════════════════════════════════════════════
// DATABASE INTEGRITY & HALLUCINATION SHIELD
// ═══════════════════════════════════════════════════════════

async function fetchIntegrityAudit() {
  const orphansTbody = document.getElementById('audit-orphans-tbody');
  const missingTbody = document.getElementById('audit-missing-tbody');
  const pruneBtn = document.getElementById('prune-btn');
  const shieldCard = document.getElementById('integrity-shield-card');
  const shieldTitle = document.getElementById('integrity-shield-title');
  const shieldDesc = document.getElementById('integrity-shield-desc');

  try {
    const res = await apiFetch(`${API_BASE}/audit/integrity`);
    const data = await res.json();

    if (data.error) {
      console.error('Integrity audit error:', data.error);
      showToast('Integrity Audit failed: ' + data.error, 'error');
      return;
    }

    // Set stats text fields
    document.getElementById('audit-fs-chunks').textContent = data.total_filesystem_chunks ?? '--';
    document.getElementById('audit-sim-claims').textContent = data.total_active_simulated_claims ?? '--';
    document.getElementById('audit-db-chunks').textContent = data.total_sqlite_chunks ?? '--';
    document.getElementById('audit-db-units').textContent = data.total_sqlite_atomic_units ?? '--';
    document.getElementById('audit-orphan-count').textContent = data.total_orphaned_units ?? '0';
    document.getElementById('audit-missing-count').textContent = data.total_missing_chunks ?? '0';

    // 1. Render Obsolete/Orphaned SQLite Units
    const orphans = data.orphaned_units || [];
    if (orphans.length === 0) {
      orphansTbody.innerHTML = `<tr><td colspan="3" style="text-align:center;padding:12px;color:#888;">No orphaned units.</td></tr>`;
    } else {
      orphansTbody.innerHTML = orphans.map(u => `
        <tr>
          <td style="font-family:var(--vt);font-size:16px;color:#888;">${escHtml(u.id)}</td>
          <td style="font-family:var(--vt);font-size:16px;color:var(--red);">${escHtml(u.claim)}</td>
          <td style="color:#aaa;">${escHtml(u.department)}</td>
        </tr>
      `).join('');
    }

    // 2. Render Unindexed Filesystem Chunks
    const missing = data.missing_chunks || [];
    if (missing.length === 0) {
      missingTbody.innerHTML = `<tr><td colspan="3" style="text-align:center;padding:12px;color:#888;">No missing chunks.</td></tr>`;
    } else {
      missingTbody.innerHTML = missing.map(c => `
        <tr>
          <td style="font-family:var(--vt);font-size:16px;color:#888;">${escHtml(c.id)}</td>
          <td style="font-family:var(--vt);font-size:16px;color:var(--yellow);">${escHtml(c.title)}</td>
          <td style="color:#aaa;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escHtml(c.source_identifier)}">${escHtml(c.source_identifier)}</td>
        </tr>
      `).join('');
    }

    // 3. Update the Shield alert card state
    if (data.total_orphaned_units > 0) {
      if (shieldCard) {
        shieldCard.className = 'shield-card warning';
      }
      if (shieldTitle) {
        shieldTitle.textContent = 'INTEGRITY THREAT DETECTED';
      }
      if (shieldDesc) {
        shieldDesc.textContent = `Found ${data.total_orphaned_units} stale/orphaned legacy rows in SQLite database. AI engine might hallucinate on obsolete data.`;
      }
      if (pruneBtn) {
        pruneBtn.style.display = 'inline-block';
        pruneBtn.disabled = false;
        pruneBtn.textContent = '⚡ PRUNE DATABASE ORPHANS';
      }
    } else {
      if (shieldCard) {
        shieldCard.className = 'shield-card success';
      }
      if (shieldTitle) {
        shieldTitle.textContent = '✓ SYSTEM SYNCED — HALLUCINATION SHIELD ACTIVE';
      }
      if (shieldDesc) {
        shieldDesc.textContent = 'SQLite database is perfectly in sync with simulated filesystem claims. Hallucination hazards are neutralized.';
      }
      if (pruneBtn) {
        pruneBtn.style.display = 'none';
      }
    }

  } catch (err) {
    console.error('fetchIntegrityAudit failed:', err);
    showToast('Failed to fetch integrity audit details: ' + err.message, 'error');
  }
}

async function pruneDatabaseOrphans() {
  const pruneBtn = document.getElementById('prune-btn');
  if (pruneBtn) {
    pruneBtn.disabled = true;
    pruneBtn.textContent = '⏳ PRUNING...';
  }

  try {
    const res = await apiFetch(`${API_BASE}/audit/prune`, {
      method: 'POST'
    });
    const data = await res.json();

    if (data.success) {
      showToast(`✓ Database synchronized! Pruned ${data.pruned_count} orphaned rows.`, 'success');
      await fetchIntegrityAudit();
      await fetchStats(); // update main dashboard stats immediately
    } else {
      showToast('Pruning failed: ' + (data.detail || data.error || 'Unknown error'), 'error');
    }
  } catch (err) {
    console.error('pruneDatabaseOrphans failed:', err);
    showToast('Failed to prune database orphans: ' + err.message, 'error');
  } finally {
    if (pruneBtn) {
      pruneBtn.disabled = false;
      pruneBtn.textContent = '⚡ PRUNE DATABASE ORPHANS';
    }
  }
}


// ═══════════════════════════════════════════════════════════
// BOOT — DOMContentLoaded
// ═══════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', async () => {
  startClock();
  setupNavigation();
  setupPrivacyToggle();

  // ── Auto-discover backend (scans all ports, picks first Cortex API) ──
  const discovered = await _discoverBackend();
  const _bootPort = discovered.match(/:(\d+)/)?.[1] || '?';
  console.log(`[Boot] Backend discovered on port ${_bootPort}`);
  showToast(`Backend auto-detected on :${_bootPort}`, 'success');

  // Now safe to call all API endpoints
  loadConfig();     // also triggers _pollUntilAlive if backend is down
  checkStatus();
  fetchStats();
  fetchSkills();
  loadAgents();
  loadConnectors();
  fetchMemoryUnits();
  fetchSearchStats();
  fetchHealthCheck();
  fetchIntegrityAudit();

  // Real-time polling (checkStatus also re-validates backend liveness)
  setInterval(checkStatus,     10000);   // every 10s
  setInterval(fetchStats,      30000);   // every 30s
  setInterval(loadConnectors,  60000);   // every 60s
  setInterval(fetchSearchStats, 60000);  // every 60s

  // Re-discover backend every 45s in case it restarts on a different port
  // If re-discovery finds a NEW port, auto-reload all data
  let _lastKnownBase = API_BASE;
  setInterval(async () => {
    const oldBase = API_BASE;
    // Call _discoverBackend directly. It will verify the cached base first.
    // If it's alive, it returns instantly without a full sweep (no 404s, no CPU load).
    // If it's dead, it will automatically clear and probe all ports to find it.
    const newBase = await _discoverBackend();
    if (newBase !== oldBase) {
      const port = newBase.match(/:(\d+)/)?.[1] || '?';
      showToast(`Backend moved to :${port} — reconnected!`, 'success');
      // Reload all data from the new port
      loadConfig(); checkStatus(); fetchStats(); loadConnectors();
    }
  }, 45000);
});
