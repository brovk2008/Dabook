/**
 * DABOOK Control Room — Frontend JavaScript
 * ES Modules; connects to SSE stream and REST API.
 */

// ─── State ────────────────────────────────────────────────
let state = {
  snapshot: null,
  settings: {},
  books: [],
  logs: [],
  charts: {},
  activePanel: 'overview',
  paused: false,
  throughputHistory: [],
  pendingHistory: [],
  swimlaneWorkers: {},
  datasets: [],
  selectedDataset: null,
  datasetOffset: 0,
  datasetLimit: 10,
  datasetView: 'rendered',
};

const MAX_LOGS = 500;
const API = '';  // same origin

// ─── SSE Connection ───────────────────────────────────────
let evtSource = null;
let reconnectDelay = 1000;

function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`${API}/api/stream`);

  evtSource.addEventListener('snapshot', (e) => {
    const data = JSON.parse(e.data);
    state.snapshot = data;
    renderOverview(data);
    reconnectDelay = 1000;
    document.getElementById('stale-indicator').style.display = 'none';
  });

  evtSource.addEventListener('tick', (e) => {
    const data = JSON.parse(e.data);
    state.snapshot = data;
    renderOverview(data);
  });

  evtSource.addEventListener('log', (e) => {
    const row = JSON.parse(e.data);
    appendLog(row);
  });

  evtSource.addEventListener('alert', (e) => {
    const data = JSON.parse(e.data);
    showAlert(data.msg);
  });

  evtSource.onerror = () => {
    document.getElementById('stale-indicator').style.display = 'flex';
    evtSource.close();
    setTimeout(connectSSE, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  };
}

// ─── Panel switching ──────────────────────────────────────
function switchPanel(name) {
  state.activePanel = name;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById(`panel-${name}`).classList.add('active');
  document.querySelector(`[data-panel="${name}"]`).classList.add('active');

  if (name === 'books') loadBooks();
  if (name === 'datasets') loadDatasets();
  if (name === 'settings') loadSettings();
}

// ─── Overview rendering ───────────────────────────────────
function renderOverview(data) {
  // ETA
  document.getElementById('eta-val').textContent = data.eta || '—';

  // Status badge
  const statusBadge = document.getElementById('status-badge');
  statusBadge.className = 'stat-badge';
  if (data.books?.active > 0) statusBadge.classList.add('status-running');
  statusBadge.textContent = data.books?.active > 0 ? 'Running' : 'Idle';

  // Counters
  const t = data.tasks || {};
  const b = data.books || {};
  const done = (t.done || 0) + (t.skipped || 0);
  const total = done + (t.pending || 0) + (t.running || 0) + (t.failed || 0) + (t.dead || 0);

  set('c-books-done', b.done || 0);
  set('c-books-active', b.active || 0);
  set('c-books-queued', b.queued || 0);
  set('c-books-failed', b.failed || 0);
  set('c-tasks-done', done);
  set('c-tasks-running', t.running || 0);
  set('c-tasks-pending', t.pending || 0);
  set('c-tasks-dead', t.dead || 0);

  // Overall progress
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  document.getElementById('overall-progress-bar').style.width = `${pct}%`;
  document.getElementById('overall-pct').textContent = `${pct}%`;

  // Running table
  renderRunningTable(data.running || []);

  // System resources
  renderSystem(data.system || {});

  // Charts
  const now = Date.now();
  state.pendingHistory.push({ x: now, y: t.pending || 0 });
  if (state.pendingHistory.length > 60) state.pendingHistory.shift();
  updateCharts();

  // Swimlane
  renderSwimlane(data.running || []);
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function renderRunningTable(running) {
  const tbody = document.getElementById('running-tbody');
  if (!running.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No tasks currently running</td></tr>';
    return;
  }
  tbody.innerHTML = running.map(r => {
    const pct = r.units_total > 0
      ? Math.round((r.units_done / r.units_total) * 100) : 0;
    const elapsed = r.started_at
      ? fmtDuration(Date.now() / 1000 - r.started_at) : '—';
    const bookName = r.title || (r.sha256 || '').slice(0, 8) || '—';
    return `<tr>
      <td><code style="font-size:11px;color:var(--text-muted)">${esc(r.worker_id || '—')}</code></td>
      <td title="${esc(bookName)}">${esc(bookName.slice(0, 24))}</td>
      <td><span class="stage-chip">${esc(r.stage || '—')}</span></td>
      <td style="font-size:11px;color:var(--text-muted)">${r.shard_idx ?? '—'}</td>
      <td>
        <div class="mini-progress">
          <div class="mini-bar-outer"><div class="mini-bar-inner" style="width:${pct}%"></div></div>
          <span style="font-size:11px;min-width:32px">${pct}%</span>
        </div>
      </td>
      <td style="font-size:11px;color:var(--text-muted)">${elapsed}</td>
      <td style="font-size:11px;color:var(--text-muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.substep || '—')}</td>
    </tr>`;
  }).join('');
}

function renderSystem(sys) {
  if (!sys.cpu_pct) return;

  // CPU
  const cpu = sys.cpu_pct;
  document.getElementById('cpu-bar').style.width = `${cpu}%`;
  document.getElementById('cpu-val').textContent = `${cpu.toFixed(0)}%`;
  setBarColor('cpu-bar', cpu, 70, 90);

  // RAM
  const ramPct = sys.ram_total_mb > 0 ? (sys.ram_used_mb / sys.ram_total_mb * 100) : 0;
  document.getElementById('ram-bar').style.width = `${ramPct}%`;
  document.getElementById('ram-val').textContent =
    `${fmtMB(sys.ram_used_mb)} / ${fmtMB(sys.ram_total_mb)}`;
  setBarColor('ram-bar', ramPct, 70, 90);

  // Disk (invert: high free = good)
  const diskFreeGB = sys.disk_free_gb || 0;
  // Show as "low" when < 5 GB
  document.getElementById('disk-val').textContent = `${diskFreeGB.toFixed(1)} GB free`;
  const diskPct = Math.max(0, Math.min(100, (1 - diskFreeGB / 50) * 100));
  document.getElementById('disk-bar').style.width = `${diskPct}%`;

  // GPUs
  const gpuSec = document.getElementById('gpu-section');
  if (sys.gpus && sys.gpus.length > 0) {
    gpuSec.innerHTML = sys.gpus.map(g => `
      <div class="gpu-card">
        <div class="gpu-name">GPU ${g.index} — ${esc(g.name || '')}</div>
        <div class="resource-row">
          <label style="font-size:11px">Util</label>
          <div class="resource-bar-wrap"><div class="resource-bar" style="width:${g.util_pct}%;background:linear-gradient(90deg,var(--purple),var(--blue))"></div></div>
          <span style="font-size:11px;width:40px">${g.util_pct}%</span>
        </div>
        <div class="resource-row">
          <label style="font-size:11px">VRAM</label>
          <div class="resource-bar-wrap"><div class="resource-bar" style="width:${g.vram_total_mb > 0 ? g.vram_used_mb/g.vram_total_mb*100 : 0}%;background:linear-gradient(90deg,var(--purple),var(--blue))"></div></div>
          <span style="font-size:11px;width:80px">${fmtMB(g.vram_used_mb)}/${fmtMB(g.vram_total_mb)}</span>
        </div>
        <div style="font-size:11px;color:var(--text-muted)">🌡 ${g.temp_c ?? '—'}°C${g.power_w != null ? ` · ⚡ ${g.power_w.toFixed(0)}W` : ''}</div>
      </div>
    `).join('');
  } else {
    gpuSec.innerHTML = '<div style="font-size:11px;color:var(--text-muted);margin-top:8px">No GPU detected</div>';
  }
}

function setBarColor(id, pct, warn, crit) {
  const el = document.getElementById(id);
  if (!el) return;
  if (pct >= crit)       el.style.background = 'linear-gradient(90deg,var(--red),#c00)';
  else if (pct >= warn)  el.style.background = 'linear-gradient(90deg,var(--orange),#d97706)';
  else                   el.style.background = 'linear-gradient(90deg,var(--blue),var(--purple))';
}

// ─── Charts ───────────────────────────────────────────────
function initCharts() {
  const defaults = {
    responsive: true,
    animation: { duration: 300 },
    plugins: { legend: { display: false } },
    scales: {
      x: { display: false },
      y: {
        grid: { color: 'rgba(255,255,255,0.05)' },
        ticks: { color: '#8b8fa8', font: { family: "'JetBrains Mono'" } },
      },
    },
  };

  // Throughput chart (placeholder)
  const tCtx = document.getElementById('chart-throughput').getContext('2d');
  state.charts.throughput = new Chart(tCtx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        data: [],
        borderColor: 'rgba(99,102,241,0.8)',
        backgroundColor: 'rgba(99,102,241,0.1)',
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: { ...defaults },
  });

  // Burn-down chart (tasks pending over time)
  const bCtx = document.getElementById('chart-burndown').getContext('2d');
  state.charts.burndown = new Chart(bCtx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: 'Pending tasks',
        data: [],
        borderColor: 'rgba(168,85,247,0.8)',
        backgroundColor: 'rgba(168,85,247,0.1)',
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: { ...defaults },
  });
}

function updateCharts() {
  const hist = state.pendingHistory;
  if (!hist.length || !state.charts.burndown) return;

  const labels = hist.map(p => new Date(p.x).toLocaleTimeString());
  const vals = hist.map(p => p.y);

  const bd = state.charts.burndown;
  bd.data.labels = labels;
  bd.data.datasets[0].data = vals;
  bd.update('none');
}

// ─── Swimlane ─────────────────────────────────────────────
function renderSwimlane(running) {
  const container = document.getElementById('swimlane-container');
  if (!running.length) {
    container.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px">No workers active</div>';
    return;
  }

  // Group by worker_id
  const byWorker = {};
  running.forEach(r => {
    byWorker[r.worker_id] = r;
  });

  // Also show idle workers
  const allWorkers = new Set([...Object.keys(state.swimlaneWorkers), ...Object.keys(byWorker)]);
  Object.keys(byWorker).forEach(k => { state.swimlaneWorkers[k] = byWorker[k]; });

  container.innerHTML = `<div class="swimlane-grid">` +
    Array.from(allWorkers).map(wid => {
      const task = byWorker[wid];
      const pct = task
        ? (task.units_total > 0 ? task.units_done / task.units_total * 100 : 50)
        : 0;
      const label = task
        ? `${task.stage} · ${task.shard_idx}`
        : 'idle';
      return `<div class="swimlane-row">
        <div class="swimlane-label" title="${esc(wid)}">${esc(wid)}</div>
        <div class="swimlane-track">
          <div class="swimlane-block ${task ? 'swimlane-busy' : 'swimlane-idle'}"
               style="left:0;width:${task ? Math.max(10, pct) : 100}%">
            ${esc(label)}
          </div>
        </div>
      </div>`;
    }).join('') + `</div>`;
}

// ─── Books panel ──────────────────────────────────────────
async function loadBooks() {
  const res = await fetch(`${API}/api/books`);
  state.books = await res.json();
  renderBooks(state.books);
}

function renderBooks(books) {
  const container = document.getElementById('books-list');
  if (!books.length) {
    container.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:40px">No books yet. Add PDFs to get started.</div>';
    return;
  }
  container.innerHTML = books.map(b => {
    const stageMap = {};
    (b.stages || []).forEach(s => {
      if (!stageMap[s.stage] || s.state === 'running') stageMap[s.stage] = s.state;
    });
    const stageDots = Array.from({length: 14}, (_, i) => {
      const name = `s${String(i).padStart(2,'0')}`;
      const st = Object.keys(stageMap).find(k => k.startsWith(name));
      const cls = st ? `stage-dot ${stageMap[st]}` : 'stage-dot';
      return `<div class="${cls}" title="${name}"></div>`;
    }).join('');

    const stateChip = `<span class="chip chip-${b.state}">${b.state}</span>`;
    const pages = b.pages ? `${b.pages} pages` : '';
    const qs = b.quality_score != null ? `Q: ${(b.quality_score * 100).toFixed(0)}%` : '';

    return `<div class="book-row" id="book-${b.id}">
      <div>
        <div class="book-title">${esc(b.title || b.sha256?.slice(0,12) || 'Unknown')}</div>
        <div class="book-meta">${[pages, qs, b.doc_type].filter(Boolean).join(' · ')}</div>
        <div class="book-stage-dots" style="margin-top:6px">${stageDots}</div>
      </div>
      <div>${stateChip}</div>
      <div class="book-actions">
        ${b.state === 'active' || b.state === 'queued'
          ? `<button class="btn btn-ghost btn-sm" onclick="bookAction(${b.id},'pause')">⏸</button>`
          : `<button class="btn btn-ghost btn-sm" onclick="bookAction(${b.id},'resume')">▶</button>`}
        ${['failed','done'].includes(b.state)
          ? `<button class="btn btn-ghost btn-sm" onclick="bookAction(${b.id},'retry')">↺</button>`
          : ''}
      </div>
    </div>`;
  }).join('');
}

function filterBooks() {
  const q = document.getElementById('books-search').value.toLowerCase();
  const filtered = state.books.filter(b =>
    (b.title || '').toLowerCase().includes(q) ||
    (b.sha256 || '').includes(q)
  );
  renderBooks(filtered);
}

async function bookAction(id, action) {
  await fetch(`${API}/api/books/${id}/${action}`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken()}` },
  });
  loadBooks();
}

function showUpload() {
  const el = document.getElementById('upload-drop');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function handleFileUpload(event) {
  const files = event.target.files;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  await fetch(`${API}/api/books`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken()}` },
    body: fd,
  });
  loadBooks();
}

// ─── Settings panel ───────────────────────────────────────
async function loadSettings() {
  const res = await fetch(`${API}/api/settings`);
  const rows = await res.json();
  state.settings = {};
  rows.forEach(r => { state.settings[r.k] = r; });
  renderSettings(rows);
}

const SETTING_LABELS = {
  'workers.cpu':       { label: 'CPU Workers',        live: true  },
  'workers.gpu':       { label: 'GPU Workers',        live: true  },
  'workers.io':        { label: 'IO Workers',         live: true  },
  'workers.llm':       { label: 'LLM Workers',        live: true  },
  'book_concurrency':  { label: 'Book Concurrency',   live: true  },
  'queue.paused':      { label: 'Queue Paused',       live: true  },
  'ram_ceiling_pct':   { label: 'RAM Ceiling %',      live: true  },
  'vram_ceiling_pct':  { label: 'VRAM Ceiling %',     live: true  },
  'min_free_gb':       { label: 'Min Free Disk (GB)', live: true  },
  'shard_pages':       { label: 'Pages per Shard',    live: true  },
  'retry.max_attempts':{ label: 'Max Retry Attempts', live: true  },
  'dataset.merge_all': { label: 'Consolidate All Books (all_*.jsonl)', live: true },
  'dataset.filter_boilerplate': { label: 'Filter Boilerplate & Index', live: true },
  'dataset.sft_format':{ label: 'SFT Format (both / chatml / alpaca)', live: true },
  'ui.port':           { label: 'Dashboard Port',     live: false },
};

function renderSettings(rows) {
  const form = document.getElementById('settings-form');
  form.innerHTML = rows.map(r => {
    const meta = SETTING_LABELS[r.k] || { label: r.k, live: r.live === 1 };
    return `<div class="setting-card">
      <div class="setting-key">${esc(r.k)}</div>
      <div class="setting-label">
        ${esc(meta.label)}
        <span class="setting-badge ${meta.live ? 'badge-live' : 'badge-restart'}">
          ${meta.live ? 'live' : 'restart'}
        </span>
      </div>
      <input class="setting-input" id="setting-${esc(r.k)}" value="${esc(r.v)}"
        ${meta.live ? '' : 'disabled title="Requires restart"'}>
    </div>`;
  }).join('');
}

async function saveSettings() {
  const updates = {};
  document.querySelectorAll('.setting-input:not([disabled])').forEach(inp => {
    const key = inp.id.replace('setting-', '');
    updates[key] = inp.value;
  });
  await fetch(`${API}/api/settings`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getToken()}`,
    },
    body: JSON.stringify(updates),
  });
}

// ─── Queue controls ───────────────────────────────────────
async function togglePause() {
  state.paused = !state.paused;
  const action = state.paused ? 'pause' : 'resume';
  document.getElementById('pause-btn').textContent = state.paused ? '▶ Resume' : '⏸ Pause';
  await fetch(`${API}/api/queue/${action}`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken()}` },
  });
}

// ─── Logs ─────────────────────────────────────────────────
function appendLog(row) {
  state.logs.push(row);
  if (state.logs.length > MAX_LOGS) state.logs.shift();
  if (state.activePanel === 'logs') renderLogLine(row);
}

function renderLogLine(row) {
  const filter = document.getElementById('log-level-filter').value;
  if (filter && row.level !== filter) return;

  const container = document.getElementById('log-tail');
  const div = document.createElement('div');
  div.className = `log-line level-${row.level}`;
  const ts = new Date(row.ts * 1000).toLocaleTimeString();
  div.innerHTML = `<span class="log-ts">${ts}</span>  [${esc(row.level)}]  ${esc(row.kind)}: ${esc(row.msg || '')}`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function filterLogs() {
  const container = document.getElementById('log-tail');
  container.innerHTML = '';
  state.logs.forEach(renderLogLine);
}

function clearLogs() {
  state.logs = [];
  document.getElementById('log-tail').innerHTML = '';
}

function copyDiagnostics() {
  const text = state.logs.map(r =>
    `${new Date(r.ts * 1000).toISOString()} [${r.level}] ${r.kind}: ${r.msg}`
  ).join('\n');
  navigator.clipboard.writeText(text).then(() => showAlert('Diagnostics copied to clipboard'));
}

// ─── Datasets Panel ───────────────────────────────────────
async function loadDatasets() {
  try {
    const res = await fetch(`${API}/api/datasets`);
    const list = await res.json();
    state.datasets = list || [];
    renderDatasetCards(state.datasets);
    populateDatasetSelect(state.datasets);

    if (state.datasets.length > 0) {
      if (!state.selectedDataset || !state.datasets.some(d => d.rel_path === state.selectedDataset.rel_path)) {
        const pref = state.datasets.find(d => d.rel_path === 'compiled/all_pretrain.jsonl')
          || state.datasets.find(d => d.rel_path === 'compiled/all_sft.jsonl')
          || state.datasets[0];
        state.selectedDataset = pref;
      }
      const sel = document.getElementById('dataset-select');
      if (sel) sel.value = state.selectedDataset.rel_path;
      loadDatasetPreview();
    } else {
      const container = document.getElementById('dataset-preview-content');
      if (container) {
        container.innerHTML = '<div class="empty-preview">No datasets compiled yet. Run processing to generate datasets.</div>';
      }
    }
  } catch (err) {
    console.error('Failed to load datasets:', err);
  }
}

function renderDatasetCards(datasets) {
  const container = document.getElementById('dataset-cards-summary');
  if (!container) return;

  const typeMeta = {
    'pretrain': { label: 'Pre-training (CLM)', icon: '📖', badge: 'Chapters', color: 'var(--blue)' },
    'sft':      { label: 'Fine-Tuning (SFT)',  icon: '💬', badge: 'ChatML & Alpaca', color: 'var(--purple)' },
    'code':     { label: 'Code & Asm',         icon: '💻', badge: 'Multi-line', color: 'var(--green)' },
    'rag':      { label: 'RAG Vectors',        icon: '🔍', badge: 'Breadcrumbs', color: 'var(--cyan)' },
    'raw':      { label: 'Raw Graph',          icon: '📦', badge: 'Faithful', color: 'var(--orange)' },
  };

  const byType = {};
  datasets.forEach(d => {
    if (!byType[d.type] || d.scope === 'workspace') {
      byType[d.type] = d;
    }
  });

  const cardsHtml = Object.keys(typeMeta).map(type => {
    const meta = typeMeta[type];
    const ds = byType[type];
    const records = ds ? ds.records.toLocaleString() : '0';
    const size = ds ? fmtMB(ds.size_bytes / (1024 * 1024)) : '0 MB';
    const isReady = Boolean(ds && ds.records > 0);
    const scopeLabel = ds?.scope === 'workspace' ? 'Consolidated Master' : (ds ? esc(ds.scope_label) : 'Pending');

    return `<div class="dataset-summary-card ${isReady ? 'ready' : ''}" onclick="selectDatasetByPath('${ds ? esc(ds.rel_path) : ''}')">
      <div class="dataset-card-top">
        <span class="dataset-card-icon">${meta.icon}</span>
        <span class="dataset-card-badge" style="border-color:${meta.color};color:${meta.color}">${meta.badge}</span>
      </div>
      <div class="dataset-card-title">${meta.label}</div>
      <div class="dataset-card-scope">${scopeLabel}</div>
      <div class="dataset-card-stat">
        <span class="dataset-stat-num">${records}</span>
        <span class="dataset-stat-label">records (${size})</span>
      </div>
    </div>`;
  }).join('');

  container.innerHTML = cardsHtml;
}

function populateDatasetSelect(datasets) {
  const sel = document.getElementById('dataset-select');
  if (!sel) return;

  const consolidated = datasets.filter(d => d.scope === 'workspace');
  const perBook = datasets.filter(d => d.scope !== 'workspace');

  let html = '';
  if (consolidated.length > 0) {
    html += '<optgroup label="🌟 Consolidated Master Datasets">';
    consolidated.forEach(d => {
      html += `<option value="${esc(d.rel_path)}">${esc(d.name)} (${d.records.toLocaleString()} rows · ${fmtMB(d.size_bytes / (1024*1024))})</option>`;
    });
    html += '</optgroup>';
  }

  if (perBook.length > 0) {
    html += '<optgroup label="📚 Per-Book Datasets">';
    perBook.forEach(d => {
      html += `<option value="${esc(d.rel_path)}">[${esc(d.scope_label)}] ${esc(d.name)} (${d.records.toLocaleString()} rows)</option>`;
    });
    html += '</optgroup>';
  }

  sel.innerHTML = html;
}

function onSelectDataset() {
  const sel = document.getElementById('dataset-select');
  if (!sel) return;
  selectDatasetByPath(sel.value);
}

function selectDatasetByPath(path) {
  if (!path) return;
  const match = state.datasets.find(d => d.rel_path === path);
  if (match) {
    state.selectedDataset = match;
    state.datasetOffset = 0;
    const sel = document.getElementById('dataset-select');
    if (sel) sel.value = path;
    loadDatasetPreview();
  }
}

function setDatasetView(view) {
  state.datasetView = view;
  document.getElementById('view-toggle-rendered')?.classList.toggle('active', view === 'rendered');
  document.getElementById('view-toggle-json')?.classList.toggle('active', view === 'json');
  loadDatasetPreview();
}

async function loadDatasetPreview() {
  if (!state.selectedDataset) return;
  const container = document.getElementById('dataset-preview-content');
  if (!container) return;

  container.innerHTML = '<div class="loading-preview">Loading preview records...</div>';

  try {
    const res = await fetch(`${API}/api/datasets/preview?file=${encodeURIComponent(state.selectedDataset.rel_path)}&offset=${state.datasetOffset}&limit=${state.datasetLimit}`);
    const data = await res.json();

    const total = data.total_records || 0;
    const start = total === 0 ? 0 : state.datasetOffset + 1;
    const end = Math.min(state.datasetOffset + state.datasetLimit, total);
    const pageInfo = document.getElementById('dataset-page-info');
    if (pageInfo) pageInfo.textContent = `${start.toLocaleString()} - ${end.toLocaleString()} of ${total.toLocaleString()}`;

    const prevBtn = document.getElementById('btn-prev-page');
    const nextBtn = document.getElementById('btn-next-page');
    if (prevBtn) prevBtn.disabled = (state.datasetOffset <= 0);
    if (nextBtn) nextBtn.disabled = (end >= total);

    renderDatasetPreview(data.records || [], state.selectedDataset.type);
  } catch (err) {
    container.innerHTML = `<div class="empty-preview" style="color:var(--red)">Failed to load preview: ${esc(err.message)}</div>`;
  }
}

function prevDatasetPage() {
  if (state.datasetOffset > 0) {
    state.datasetOffset = Math.max(0, state.datasetOffset - state.datasetLimit);
    loadDatasetPreview();
  }
}

function nextDatasetPage() {
  state.datasetOffset += state.datasetLimit;
  loadDatasetPreview();
}

function renderDatasetPreview(records, dtype) {
  const container = document.getElementById('dataset-preview-content');
  if (!container) return;

  if (!records.length) {
    container.innerHTML = '<div class="empty-preview">No records in this page</div>';
    return;
  }

  if (state.datasetView === 'json') {
    container.innerHTML = records.map((r, i) => {
      const idx = state.datasetOffset + i + 1;
      const jsonStr = JSON.stringify(r, null, 2);
      return `<div class="preview-item-json">
        <div class="preview-item-header">
          <span class="preview-item-idx">#${idx}</span>
          <button class="btn btn-ghost btn-sm" onclick="copyDatasetRaw(this)">📋 Copy JSON</button>
        </div>
        <pre class="json-code-block"><code>${esc(jsonStr)}</code></pre>
      </div>`;
    }).join('');
    return;
  }

  container.innerHTML = records.map((r, i) => {
    const idx = state.datasetOffset + i + 1;
    if (dtype === 'sft' || r.messages || r.instruction) {
      return renderSftRecord(r, idx);
    }
    if (dtype === 'pretrain' || r.doc_id) {
      return renderPretrainRecord(r, idx);
    }
    if (dtype === 'code' || r.code) {
      return renderCodeRecord(r, idx);
    }
    return renderGenericRecord(r, idx);
  }).join('');
}

function renderSftRecord(r, idx) {
  const category = r.category || 'instruction';
  const page = r.source_page ? `Page ${r.source_page}` : '';
  const breadcrumbs = (r.breadcrumbs || []).join(' › ');

  let msgsHtml = '';
  if (r.messages && Array.isArray(r.messages)) {
    msgsHtml = r.messages.map(m => {
      const role = m.role || 'user';
      return `<div class="chatml-msg chatml-${esc(role)}">
        <div class="chatml-role-badge">${esc(role.toUpperCase())}</div>
        <div class="chatml-body">${esc(m.content)}</div>
      </div>`;
    }).join('');
  } else {
    msgsHtml = `
      <div class="chatml-msg chatml-user">
        <div class="chatml-role-badge">USER (INSTRUCTION)</div>
        <div class="chatml-body"><strong>${esc(r.instruction || '')}</strong>${r.input ? `<div style="margin-top:6px">${esc(r.input)}</div>` : ''}</div>
      </div>
      <div class="chatml-msg chatml-assistant">
        <div class="chatml-role-badge">ASSISTANT</div>
        <div class="chatml-body">${esc(r.output || '')}</div>
      </div>
    `;
  }

  return `<div class="preview-item-card">
    <div class="preview-item-header">
      <div class="preview-header-left">
        <span class="preview-item-idx">#${idx}</span>
        <span class="stage-chip">${esc(category)}</span>
        <span class="crumb-chip">${esc(breadcrumbs || page)}</span>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="copyDatasetRaw(this)">📋 Copy Record</button>
    </div>
    <div class="chatml-thread">${msgsHtml}</div>
  </div>`;
}

function renderPretrainRecord(r, idx) {
  const title = r.chapter_title || r.title || 'Chapter';
  const words = (r.word_count || 0).toLocaleString();
  const tokens = (r.token_count || 0).toLocaleString();
  const pages = `Pages ${r.page_start || 1} - ${r.page_end || 1}`;

  return `<div class="preview-item-card">
    <div class="preview-item-header">
      <div class="preview-header-left">
        <span class="preview-item-idx">#${idx}</span>
        <span class="chip chip-active">${esc(title)}</span>
        <span class="crumb-chip">${pages} · ${words} words · ~${tokens} tokens</span>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="copyDatasetRaw(this)">📋 Copy Record</button>
    </div>
    <div class="pretrain-doc-body"><pre class="doc-markdown-pre"><code>${esc(r.text || '')}</code></pre></div>
  </div>`;
}

function renderCodeRecord(r, idx) {
  const lang = r.language || 'asm';
  const page = r.page ? `Page ${r.page}` : '';
  const lines = r.line_count ? `${r.line_count} lines` : '';
  const breadcrumbs = (r.breadcrumbs || []).join(' › ');

  return `<div class="preview-item-card">
    <div class="preview-item-header">
      <div class="preview-header-left">
        <span class="preview-item-idx">#${idx}</span>
        <span class="stage-chip">${esc(lang)}</span>
        <span class="crumb-chip">${esc(breadcrumbs || page)} · ${lines}</span>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="copyDatasetRaw(this)">📋 Copy Record</button>
    </div>
    ${r.surrounding_context ? `<div class="code-context-banner"><span class="context-label">Context:</span> ${esc(r.surrounding_context)}</div>` : ''}
    <pre class="code-train-pre"><code class="language-${esc(lang)}">${esc(r.code || '')}</code></pre>
  </div>`;
}

function renderGenericRecord(r, idx) {
  const page = r.page ? `Page ${r.page}` : '';
  const crumbs = (r.breadcrumbs || []).join(' › ');
  const content = r.content || r.text || JSON.stringify(r);

  return `<div class="preview-item-card">
    <div class="preview-item-header">
      <div class="preview-header-left">
        <span class="preview-item-idx">#${idx}</span>
        <span class="crumb-chip">${esc(crumbs || page)}</span>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="copyDatasetRaw(this)">📋 Copy Record</button>
    </div>
    <div class="generic-body">${esc(content)}</div>
  </div>`;
}

async function consolidateDatasets() {
  try {
    showAlert('Consolidating datasets across all books in workspace...');
    const res = await fetch(`${API}/api/datasets/consolidate`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${getToken()}` },
    });
    const result = await res.json();
    if (result.ok) {
      showAlert('All book datasets consolidated successfully into workspace master files!');
      loadDatasets();
    } else {
      showAlert(`Consolidation failed: ${result.error || 'Unknown error'}`);
    }
  } catch (err) {
    showAlert(`Consolidation request failed: ${err.message}`);
  }
}

function copyDatasetRaw(btn) {
  const card = btn.closest('.preview-item-card') || btn.closest('.preview-item-json');
  const code = card ? (card.querySelector('code')?.innerText || '') : '';
  if (code) {
    navigator.clipboard.writeText(code).then(() => {
      const orig = btn.innerText;
      btn.innerText = '✓ Copied';
      setTimeout(() => { btn.innerText = orig; }, 2000);
    });
  }
}

// ─── Alerts ───────────────────────────────────────────────
let alertTimeout;
function showAlert(msg) {
  const el = document.getElementById('alert-banner');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(alertTimeout);
  alertTimeout = setTimeout(() => { el.style.display = 'none'; }, 8000);
}

// ─── Token ────────────────────────────────────────────────
function getToken() {
  return sessionStorage.getItem('dabook_token') ||
    new URLSearchParams(location.search).get('t') || '';
}

// ─── Helpers ──────────────────────────────────────────────
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtDuration(secs) {
  if (secs < 60) return `${secs.toFixed(0)}s`;
  if (secs < 3600) return `${(secs / 60).toFixed(0)}m`;
  return `${(secs / 3600).toFixed(1)}h`;
}

function fmtMB(mb) {
  if (!mb) return '—';
  if (mb > 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb.toFixed(0)} MB`;
}

// ─── Init ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Persist token from URL to sessionStorage
  const urlToken = new URLSearchParams(location.search).get('t');
  if (urlToken) sessionStorage.setItem('dabook_token', urlToken);

  initCharts();
  connectSSE();

  // Initial load of first panel
  loadBooks();
});

// Expose for inline onclick handlers
window.switchPanel = switchPanel;
window.togglePause = togglePause;
window.bookAction = bookAction;
window.showUpload = showUpload;
window.handleFileUpload = handleFileUpload;
window.filterBooks = filterBooks;
window.loadSettings = loadSettings;
window.saveSettings = saveSettings;
window.filterLogs = filterLogs;
window.clearLogs = clearLogs;
window.copyDiagnostics = copyDiagnostics;
window.loadDatasets = loadDatasets;
window.onSelectDataset = onSelectDataset;
window.selectDatasetByPath = selectDatasetByPath;
window.setDatasetView = setDatasetView;
window.prevDatasetPage = prevDatasetPage;
window.nextDatasetPage = nextDatasetPage;
window.consolidateDatasets = consolidateDatasets;
window.copyDatasetRaw = copyDatasetRaw;
