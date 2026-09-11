import { Viewer } from '/static/viewer.js';

const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

const ACCEPT = ['image/png', 'image/jpeg', 'image/webp'];
const ACCEPT_EXT = ['.png', '.jpg', '.jpeg', '.webp'];
const STATE_LABEL = {
  queued: 'Queued', loading_model: 'Loading model', generating: 'Generating',
  exporting: 'Exporting GLB', complete: 'Complete', error: 'Failed',
};
const STATE_HINT = {
  queued: 'Waiting for the worker.',
  loading_model: 'Loading TRELLIS.2 into memory — only on the first run after a restart.',
  generating: 'Running the diffusion pipeline on the GPU.',
  exporting: 'Remeshing, unwrapping UVs and baking PBR textures into the GLB.',
};
const STEP_ORDER = ['queued', 'loading_model', 'generating', 'exporting', 'complete'];

const el = {
  app: $('app'), model: $('modelSelect'), headroom: $('headroom'), pill: $('statusPill'),
  drop: $('dropzone'), file: $('fileInput'), prev: $('refPreview'), dropEmpty: $('dropEmpty'),
  clearRef: $('clearRef'), changeRef: $('changeRef'), fileErr: $('fileError'), fileName: $('fileName'),
  modes: $('modes'), modeNote: $('modeNote'),
  gen: $('generateBtn'), genLabel: $('genLabel'), genHint: $('genHint'),
  alert: $('alertBox'), alertTitle: $('alertTitle'), alertBody: $('alertBody'), alertData: $('alertData'),
  stage: $('viewport'), canvas: $('glcanvas'), vpEmpty: $('vpEmpty'), vpLoading: $('vpLoading'),
  vpTitle: $('vpTitle'), vpName: $('vpName'), vpStats: $('vpStats'), veil: $('dropVeil'),
  toolRotate: $('toolRotate'), toolWire: $('toolWire'), reset: $('resetCam'), toggleInspector: $('toggleInspector'),
  run: $('jobPanel'), jobState: $('jobState'), jobMeta: $('jobMeta'), jobTime: $('jobTime'),
  steps: $('jobSteps'), jobHint: $('jobHint'),
  genKv: $('genKv'), advanced: $('advanced'), seed: $('seedInput'), rollSeed: $('rollSeed'),
  randomSeed: $('randomSeed'), texSeg: $('texSeg'),
  assetEmpty: $('assetEmpty'), assetInfo: $('assetInfo'), aiName: $('aiName'), aiKv: $('aiKv'),
  aiDownload: $('aiDownload'), aiReuse: $('aiReuse'), aiRename: $('aiRename'), aiDelete: $('aiDelete'),
  lib: $('library'), libToggle: $('libToggle'), mini: $('libMini'), count: $('assetCount'),
  refresh: $('refreshAssets'), strip: $('assetStrip'), libEmpty: $('libEmpty'),
  modal: $('modal'), modalTitle: $('modalTitle'), modalBody: $('modalBody'),
  modalInput: $('modalInput'), modalOk: $('modalOk'), modalCancel: $('modalCancel'),
  toast: $('toast'),
};

const S = {
  sys: null, file: null, mode: null, texture: null, seedMax: 2147483647,
  jobId: null, poll: null, tick: null, busy: false, sawLoad: false, hideTimer: null,
  assets: [], currentId: null,
};
const viewer = new Viewer(el.canvas);

/* ------------------------------ helpers ------------------------------ */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtBytes = (b) => !b ? '—' : b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1024).toFixed(0) + ' KB';
const fmtNum = (n) => n == null ? '—' : n.toLocaleString();
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? '—' : d.toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function randomSeed() {
  const a = new Uint32Array(1);
  crypto.getRandomValues(a);
  return a[0] % (S.seedMax + 1);
}
function parseSeed(v) {
  const t = String(v).trim();
  if (!/^\d+$/.test(t)) return null;
  const n = Number(t);
  return n <= S.seedMax ? n : null;
}
let toastTimer;
function toast(msg) {
  el.toast.textContent = msg;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 2600);
}
function showAlert(title, body, data) {
  el.alertTitle.textContent = title;
  el.alertBody.textContent = body || '';
  el.alertData.innerHTML = '';
  if (data) {
    for (const [k, v] of Object.entries(data)) {
      el.alertData.insertAdjacentHTML('beforeend', `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`);
    }
  }
  el.alertData.hidden = !data;
  el.alert.hidden = false;
}
const hideAlert = () => { el.alert.hidden = true; };

/* modal: confirm / prompt */
function ask({ title, body, value, okText = 'OK', danger = false }) {
  return new Promise((resolve) => {
    el.modalTitle.textContent = title;
    el.modalBody.textContent = body || '';
    el.modalBody.hidden = !body;
    const isPrompt = value !== undefined;
    el.modalInput.hidden = !isPrompt;
    if (isPrompt) el.modalInput.value = value;
    el.modalOk.textContent = okText;
    el.modalOk.classList.toggle('danger', danger);
    el.modal.hidden = false;
    (isPrompt ? el.modalInput : el.modalOk).focus();
    if (isPrompt) el.modalInput.select();

    const done = (v) => {
      el.modal.hidden = true;
      el.modalOk.onclick = el.modalCancel.onclick = null;
      el.modalInput.onkeydown = null;
      document.onkeydown = null;
      resolve(v);
    };
    el.modalOk.onclick = () => done(isPrompt ? el.modalInput.value : true);
    el.modalCancel.onclick = () => done(null);
    el.modalInput.onkeydown = (e) => { if (e.key === 'Enter') done(el.modalInput.value); };
    document.onkeydown = (e) => { if (e.key === 'Escape') done(null); };
  });
}

/* ------------------------------ reference image ------------------------------ */
function setFile(f) {
  el.fileErr.hidden = true;
  if (!f) return;
  const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
  if (!ACCEPT.includes(f.type) && !ACCEPT_EXT.includes(ext)) {
    el.fileErr.textContent = `Unsupported file type "${f.type || ext || 'unknown'}". Use PNG, JPG, JPEG or WebP.`;
    el.fileErr.hidden = false;
    clearFile();
    return;
  }
  if (el.prev.src.startsWith('blob:')) URL.revokeObjectURL(el.prev.src);
  S.file = f;
  el.prev.src = URL.createObjectURL(f);
  el.prev.hidden = false;
  el.dropEmpty.hidden = true;
  el.clearRef.hidden = false;
  el.changeRef.hidden = false;
  el.drop.classList.add('has-img');
  el.fileName.textContent = `${f.name} · ${fmtBytes(f.size)}`;
  el.fileName.hidden = false;
  hideAlert();
  updateGenerate();
}
function clearFile() {
  S.file = null;
  if (el.prev.src.startsWith('blob:')) URL.revokeObjectURL(el.prev.src);
  el.prev.removeAttribute('src');
  el.prev.hidden = true;
  el.dropEmpty.hidden = false;
  el.clearRef.hidden = true;
  el.changeRef.hidden = true;
  el.drop.classList.remove('has-img');
  el.fileName.hidden = true;
  el.file.value = '';
  updateGenerate();
}
el.drop.addEventListener('click', () => el.file.click());
el.changeRef.addEventListener('click', () => el.file.click());
el.drop.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.file.click(); }
});
el.file.addEventListener('change', () => setFile(el.file.files[0]));
el.clearRef.addEventListener('click', (e) => { e.stopPropagation(); clearFile(); });
['dragenter', 'dragover'].forEach((t) => el.drop.addEventListener(t, (e) => {
  e.preventDefault(); el.drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((t) => el.drop.addEventListener(t, (e) => {
  e.preventDefault(); el.drop.classList.remove('over');
}));
el.drop.addEventListener('drop', (e) => { e.stopPropagation(); setFile(e.dataTransfer.files[0]); });

// The whole stage is a drop target too.
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
let dragDepth = 0;
el.stage.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth++; el.veil.hidden = false;
});
el.stage.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
el.stage.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) el.veil.hidden = true;
});
el.stage.addEventListener('drop', (e) => {
  e.preventDefault(); dragDepth = 0; el.veil.hidden = true;
  if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
});

/* ------------------------------ system, modes, status ------------------------------ */
const modeInfo = (id = S.mode) => S.sys?.modes.find((m) => m.id === id);

async function pollSystem() {
  let s;
  try { s = await (await fetch('/system')).json(); }
  catch { setStatus('unknown', 'Offline', 'The Forge3D backend is not responding.'); return; }
  S.sys = s;
  S.seedMax = s.seed_max ?? S.seedMax;
  if (S.mode === null) S.mode = s.model.default_pipeline_type;
  if (S.texture === null) S.texture = s.texture.default;
  renderModels();
  renderModes();
  renderTexture();
  renderStatus();
  renderGenKv();
  updateGenerate();
}

function renderModels() {
  const models = S.sys.models;
  if (el.model.options.length === models.length) return;
  el.model.innerHTML = models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  el.model.value = S.sys.model.id;
}

function renderModes() {
  const modes = S.sys.modes;
  if (!el.modes.children.length) {
    el.modes.innerHTML = modes.map((m) => `
      <label class="mode" data-mode="${esc(m.id)}">
        <input type="radio" name="mode" value="${esc(m.id)}">
        <span class="mode-card">
          <span class="mode-row"><b>${esc(m.label)}</b><code>${esc(m.id)}</code></span>
          <span class="mode-sub">${esc(m.summary)}</span>
          <span class="mode-foot">
            ${m.badge ? `<span class="badge ${m.experimental ? 'exp' : ''}">${esc(m.badge)}</span>` : '<span></span>'}
            <span class="mode-state"></span>
          </span>
        </span>
      </label>`).join('');
    el.modes.addEventListener('change', (e) => {
      if (e.target.name !== 'mode') return;
      S.mode = e.target.value;
      hideAlert();
      renderModeNote();
      renderStatus();
      renderGenKv();
      updateGenerate();
    });
  }
  for (const m of modes) {
    const lab = el.modes.querySelector(`[data-mode="${m.id}"]`);
    if (!lab) continue;
    lab.dataset.available = String(m.available);
    lab.querySelector('input').checked = m.id === S.mode;
    const st = lab.querySelector('.mode-state');
    st.className = 'mode-state ' + (m.available ? 'mode-time' : 'mode-avail');
    st.textContent = m.available ? m.time_hint : 'Unavailable now';
    lab.title = m.available
      ? `Peak ~${m.peak_gb} GiB (${m.peak_measured ? 'measured' : 'estimate'}) · leaves ~${m.projected_min_gb} GiB free`
      : (m.reason || '');
  }
  renderModeNote();
}

function renderModeNote() {
  const m = modeInfo();
  el.modeNote.hidden = !m || m.available;
  if (m && !m.available) el.modeNote.textContent = m.reason;
}

function renderTexture() {
  const sizes = S.sys.texture.sizes;
  if (!el.texSeg.children.length) {
    el.texSeg.innerHTML = sizes.map((sz) =>
      `<button type="button" role="radio" data-size="${sz}" aria-checked="false" title="${sz} × ${sz} texture pixels">${sz / 1024}K</button>`,
    ).join('');
    el.texSeg.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      S.texture = Number(b.dataset.size);
      renderTexture();
      renderGenKv();
    });
  }
  el.texSeg.querySelectorAll('button').forEach((b) =>
    b.setAttribute('aria-checked', String(Number(b.dataset.size) === S.texture)));
}

function setStatus(state, label, title = '') {
  el.pill.dataset.state = state;
  el.pill.querySelector('b').textContent = label;
  el.pill.title = title;
}

// Ready / Model loaded / Generating / Can't start <mode>. A resident model is
// not a memory problem on its own: the backend credits what Forge3D already
// holds, so the pill only goes red when the *selected* mode can't start.
function renderStatus() {
  const s = S.sys;
  if (!s) return;
  const m = modeInfo();
  if (S.busy || s.state === 'generating') {
    setStatus('generating', 'Generating');
  } else if (m && !m.available) {
    setStatus('blocked', `Can't start ${m.label}`, m.reason || '');
  } else if (s.model.loaded) {
    setStatus('loaded', 'Model loaded', 'TRELLIS.2 is resident — the next run skips the ~75 s load.');
  } else {
    setStatus('ready', 'Ready', 'TRELLIS.2 loads on the first generation.');
  }
  const mem = s.memory;
  el.headroom.textContent = `${mem.headroom_gb.toFixed(1)} GiB headroom`;
  el.headroom.title =
    `MemAvailable ${mem.available_gb.toFixed(1)} GiB + ${mem.forge3d_footprint_gb.toFixed(1)} GiB Forge3D already holds.\n` +
    `A run must leave at least ${mem.floor_gb} GiB free for other services.`;
}

function renderGenKv() {
  const s = S.sys;
  const m = modeInfo();
  if (!s || !m) return;
  const rows = [
    ['Model', esc(s.model.name)],
    ['Quality', `${esc(m.label)} · <span class="mono">${esc(m.id)}</span>`],
    ['Texture', `${S.texture} × ${S.texture}`],
    ['Peak memory', `~${m.peak_gb} GiB · ${m.peak_measured ? 'measured' : 'estimate'}`],
    ['Leaves free', `~${m.projected_min_gb} GiB`, m.available ? 'ok' : 'bad'],
    ['Floor', `${s.memory.floor_gb} GiB`],
  ];
  el.genKv.innerHTML = rows.map(([k, v, cls]) => `<dt>${k}</dt><dd class="${cls || ''}">${v}</dd>`).join('');
}

function updateGenerate() {
  const m = modeInfo();
  let hint;
  let ok = false;
  if (S.busy) hint = 'A generation is running.';
  else if (!S.file) hint = 'Add a reference image to begin.';
  else if (!m) hint = 'Connecting…';
  else if (!m.available) hint = `${m.label} can't start safely right now.`;
  else {
    ok = true;
    hint = `${m.label} · ${m.time_hint}${S.sys?.model.loaded ? '' : ' · +~75 s first load'}`;
  }
  el.gen.disabled = !ok;
  el.genHint.textContent = hint;
}

/* ------------------------------ seed ------------------------------ */
el.rollSeed.addEventListener('click', () => {
  el.seed.value = randomSeed();
  el.seed.classList.remove('invalid');
});
el.seed.addEventListener('input', () => {
  // Typing a seed means you want that seed; stop replacing it after each run.
  el.randomSeed.checked = false;
  el.seed.classList.toggle('invalid', parseSeed(el.seed.value) === null);
});

/* ------------------------------ generation ------------------------------ */
const memData = (d) => ({
  Available: `${d.available_gb} GiB`,
  Required: `${d.required_gb} GiB`,
  'Would bottom out at': `~${d.projected_min_gb} GiB`,
  'Protected floor': `${d.floor_gb} GiB`,
});

el.gen.addEventListener('click', async () => {
  if (el.gen.disabled || S.busy) return;
  hideAlert();
  const m = modeInfo();
  const seed = parseSeed(el.seed.value);
  if (seed === null) {
    el.advanced.open = true;
    el.seed.classList.add('invalid');
    showAlert('Invalid seed', `Use a whole number from 0 to ${S.seedMax.toLocaleString()}.`);
    return;
  }
  const fd = new FormData();
  fd.append('image', S.file);
  fd.append('mode', S.mode);
  fd.append('seed', String(seed));
  fd.append('texture_size', String(S.texture));

  setBusy(true);
  S.sawLoad = false;
  showRun({ state: 'queued', mode_label: m.label, seed, texture_size: S.texture });

  let res;
  let body;
  try {
    res = await fetch('/jobs', { method: 'POST', body: fd });
    body = await res.json();
  } catch (e) {
    failStart('Network error', String(e));
    return;
  }
  if (!res.ok) {
    if (body.error === 'insufficient_memory') {
      failStart(`Can't start ${body.mode_label || m.label}`,
        `Not enough available memory to safely start generation.\n${body.reason || ''}`, memData(body));
    } else if (body.error === 'unsupported_image') {
      failStart('Unsupported image', body.detail || 'Use PNG, JPG, JPEG or WebP.');
    } else if (body.error === 'generation_in_progress') {
      failStart('Already generating', 'Another generation is already running.');
    } else if (body.error === 'invalid_seed') {
      failStart('Invalid seed', `Use a whole number from ${body.min} to ${body.max}.`);
    } else if (body.error === 'invalid_texture_size') {
      failStart('Invalid texture size', `Choose one of ${(body.valid || []).join(', ')}.`);
    } else {
      failStart('Could not start', body.detail || body.error || `HTTP ${res.status}`);
    }
    return;
  }
  S.jobId = body.job_id;
  if (el.randomSeed.checked) el.seed.value = randomSeed(); // the seed for the *next* run
  startTicker();
  S.poll = setInterval(pollJob, 1500);
  pollJob();
});

function showRun(j) {
  clearTimeout(S.hideTimer);
  el.run.hidden = false;
  el.run.className = 'run';
  el.jobState.textContent = STATE_LABEL[j.state] || j.state;
  el.jobMeta.textContent = `${j.mode_label} · seed ${j.seed} · ${j.texture_size}px texture`;
  el.jobTime.textContent = '0s';
  el.jobHint.textContent = STATE_HINT[j.state] || '';
  renderSteps(j.state);
}

function renderSteps(state) {
  const cur = STEP_ORDER.indexOf(state);
  if (state === 'loading_model') S.sawLoad = true;
  el.steps.querySelectorAll('li').forEach((li) => {
    const i = STEP_ORDER.indexOf(li.dataset.step);
    let c = '';
    if (state === 'complete' || i < cur) c = 'done';
    else if (i === cur) c = 'current';
    if (li.dataset.step === 'loading_model' && !S.sawLoad && cur > 1) c = 'skipped';
    li.className = c;
  });
}

function startTicker() {
  const t0 = Date.now();
  clearInterval(S.tick);
  S.tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    el.jobTime.textContent = `${s}s`;
    el.genLabel.textContent = `Generating… ${s}s`;
  }, 500);
}

function setBusy(on) {
  S.busy = on;
  el.gen.classList.toggle('busy', on);
  if (!on) el.genLabel.textContent = 'Generate';
  renderStatus();
  updateGenerate();
}

function endJob() {
  clearInterval(S.poll);
  clearInterval(S.tick);
  S.poll = S.tick = null;
  S.jobId = null;
  setBusy(false);
  pollSystem();
}

// Refused before a job existed (memory gate, bad input): no run to show.
function failStart(title, body, data) {
  el.run.hidden = true;
  showAlert(title, body, data);
  endJob();
}

function failJob(title, body, data) {
  el.run.className = 'run failed';
  el.jobState.textContent = 'Failed';
  el.jobHint.textContent = '';
  showAlert(title, body, data);
  endJob();
}

async function pollJob() {
  if (!S.jobId) return;
  let j;
  try { j = await (await fetch(`/jobs/${S.jobId}`)).json(); }
  catch { return; }
  if (j.error === 'job_not_found') {
    failJob('Job lost', 'The server restarted while this generation was running.');
    return;
  }
  el.jobState.textContent = STATE_LABEL[j.state] || j.state;
  el.jobTime.textContent = `${Math.round(j.elapsed_s)}s`;
  el.jobHint.textContent = STATE_HINT[j.state] || '';
  renderSteps(j.state);

  if (j.state === 'complete') {
    el.run.className = 'run done';
    const t = j.result?.timings_s || {};
    el.jobHint.textContent = `Done in ${Math.round(t.total ?? j.elapsed_s)} s · ${j.result?.glb_mb ?? '?'} MB GLB`;
    endJob();
    await loadAssets();
    if (j.asset_id) await openAsset(j.asset_id);
    toast('Generation complete');
    S.hideTimer = setTimeout(() => { el.run.hidden = true; }, 9000);
  } else if (j.state === 'error') {
    if (j.error_kind === 'insufficient_memory' && j.error_data) {
      failJob(`Can't start ${j.error_data.mode_label}`,
        `Not enough available memory to safely start generation.\n${j.error_data.reason || ''}`,
        memData(j.error_data));
    } else {
      failJob('Generation failed', j.error || 'Unknown error');
    }
  }
}

/* ------------------------------ library ------------------------------ */
function setLibOpen(open) {
  el.lib.dataset.open = String(open);
  el.libToggle.setAttribute('aria-expanded', String(open));
  store.set('forge3d.library', open ? 'open' : 'closed');
}
el.libToggle.addEventListener('click', () => setLibOpen(el.lib.dataset.open !== 'true'));
el.refresh.addEventListener('click', loadAssets);

const tagClass = (mode) => (mode === '1024_cascade' ? 'hq' : mode === '1536_cascade' ? 'ultra' : '');
const current = () => S.assets.find((x) => x.id === S.currentId);

async function loadAssets() {
  let d;
  try { d = await (await fetch('/assets')).json(); }
  catch { return; }
  S.assets = d.assets || [];
  el.count.textContent = S.assets.length;
  renderLibrary();
  if (S.currentId) {
    const a = current();
    if (a) renderAssetInfo(a); else clearCurrent();
  }
}

function renderLibrary() {
  el.strip.innerHTML = '';
  el.mini.innerHTML = '';
  if (!S.assets.length) {
    el.strip.appendChild(el.libEmpty);
    el.libEmpty.hidden = false;
    return;
  }
  el.libEmpty.hidden = true;
  for (const a of S.assets) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'card';
    card.dataset.id = a.id;
    card.innerHTML = `
      <img alt="" loading="lazy" src="${a.has_thumbnail ? `/assets/${a.id}/thumbnail` : ''}">
      <span class="card-body">
        <span class="card-name" title="${esc(a.name)}">${esc(a.name)}</span>
        <span class="card-meta"><span class="tag ${tagClass(a.mode)}">${esc(a.mode_label)}</span><span>${fmtBytes(a.glb_bytes)}</span></span>
        <span class="card-date">${esc(fmtDate(a.created_at))}</span>
      </span>`;
    card.addEventListener('click', () => openAsset(a.id));
    el.strip.appendChild(card);
  }
  for (const a of S.assets.slice(0, 16)) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.id = a.id;
    b.title = a.name;
    b.innerHTML = a.has_thumbnail ? `<img alt="" loading="lazy" src="/assets/${a.id}/thumbnail">` : '';
    b.addEventListener('click', () => openAsset(a.id));
    el.mini.appendChild(b);
  }
  markActive();
}

function markActive() {
  document.querySelectorAll('.card, .lib-mini button').forEach((c) =>
    c.classList.toggle('active', c.dataset.id === S.currentId));
}

async function openAsset(id) {
  const a = S.assets.find((x) => x.id === id);
  el.vpEmpty.hidden = true;
  el.vpLoading.hidden = false;
  try {
    const info = await viewer.load(`/assets/${id}/model.glb`);
    S.currentId = id;
    el.vpTitle.hidden = false;
    el.vpName.textContent = a ? a.name : id;
    el.vpStats.textContent = `${fmtNum(info.vertices)} verts · ${fmtNum(info.triangles)} tris` +
      (a ? ` · ${a.mode_label} · ${fmtBytes(a.glb_bytes)}` : '');
    if (a) renderAssetInfo(a);
    markActive();
  } catch (e) {
    if (!S.currentId) el.vpEmpty.hidden = false;
    toast('Could not load model: ' + e.message);
  } finally {
    el.vpLoading.hidden = true;
  }
}

function renderAssetInfo(a) {
  el.assetEmpty.hidden = true;
  el.assetInfo.hidden = false;
  el.aiName.textContent = a.name;
  const t = a.timings_s || {};
  const rows = [
    ['Quality', `${esc(a.mode_label)} · <span class="mono">${esc(a.mode)}</span>`],
    ['Created', esc(fmtDate(a.created_at))],
    ['Seed', a.seed != null ? `<span class="mono">${a.seed}</span>` : '—'],
    ['Texture', a.texture_size ? `${a.texture_size} × ${a.texture_size}` : '—'],
    ['Geometry', `${fmtNum(a.vertices)} v · ${fmtNum(a.faces)} f`],
    ['GLB', fmtBytes(a.glb_bytes)],
    ['Time', t.total != null ? `${Math.round(t.total)} s` : '—',
      t.total != null ? `load ${t.pipeline_load} s · generate ${t.generation} s · export ${t.glb_export} s` : ''],
    ['Source', esc(a.source_filename || '—'), a.source_filename || ''],
  ];
  el.aiKv.innerHTML = rows.map(([k, v, title]) =>
    `<dt>${k}</dt><dd${title ? ` title="${esc(title)}"` : ''}>${v}</dd>`).join('');
}

function clearCurrent() {
  viewer.clear();
  S.currentId = null;
  el.vpTitle.hidden = true;
  el.vpEmpty.hidden = false;
  el.assetInfo.hidden = true;
  el.assetEmpty.hidden = false;
  markActive();
}

el.aiDownload.addEventListener('click', () => {
  if (S.currentId) location.href = `/assets/${S.currentId}/download`;
});

el.aiReuse.addEventListener('click', () => {
  const a = current();
  if (!a) return;
  if (modeInfo(a.mode)) S.mode = a.mode;
  if (a.texture_size && S.sys?.texture.sizes.includes(a.texture_size)) S.texture = a.texture_size;
  if (a.seed != null) {
    el.seed.value = a.seed;
    el.seed.classList.remove('invalid');
    el.randomSeed.checked = false;
  }
  el.advanced.open = true;
  renderModes();
  renderTexture();
  renderStatus();
  renderGenKv();
  updateGenerate();
  toast(`Settings from “${a.name}” applied`);
});

el.aiRename.addEventListener('click', async () => {
  const a = current();
  if (!a) return;
  const name = await ask({ title: 'Rename asset', value: a.name, okText: 'Rename' });
  if (name === null) return;
  const clean = name.trim();
  if (!clean) { toast('Name cannot be empty'); return; }
  const res = await fetch(`/assets/${a.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: clean }),
  });
  if (!res.ok) { toast('Rename failed'); return; }
  await loadAssets();
  el.vpName.textContent = clean;
  toast('Renamed');
});

el.aiDelete.addEventListener('click', async () => {
  const a = current();
  if (!a) return;
  const ok = await ask({
    title: 'Delete asset?',
    body: `“${a.name}” and its GLB will be permanently removed from disk. This cannot be undone.`,
    okText: 'Delete',
    danger: true,
  });
  if (!ok) return;
  const res = await fetch(`/assets/${a.id}`, { method: 'DELETE' });
  if (!res.ok) { toast('Delete failed'); return; }
  clearCurrent();
  await loadAssets();
  toast('Deleted');
});

/* ------------------------------ viewer + layout ------------------------------ */
function toggle(btn, fn) {
  const on = btn.getAttribute('aria-pressed') !== 'true';
  btn.setAttribute('aria-pressed', String(on));
  fn(on);
}
el.toolRotate.addEventListener('click', () => toggle(el.toolRotate, (on) => viewer.setAutoRotate(on)));
el.toolWire.addEventListener('click', () => toggle(el.toolWire, (on) => viewer.setWireframe(on)));
el.reset.addEventListener('click', () => viewer.reset());

function setInspector(show) {
  el.app.classList.toggle('no-inspector', !show);
  el.toggleInspector.setAttribute('aria-pressed', String(show));
  store.set('forge3d.inspector', show ? 'open' : 'closed');
}
el.toggleInspector.addEventListener('click', () => setInspector(el.app.classList.contains('no-inspector')));

/* ------------------------------ start ------------------------------ */
setLibOpen(store.get('forge3d.library', 'closed') === 'open');
setInspector(store.get('forge3d.inspector', innerWidth < 1100 ? 'closed' : 'open') === 'open');
el.seed.value = randomSeed();
pollSystem();
setInterval(pollSystem, 4000);
loadAssets();

// Test hook: lets the end-to-end suite inspect viewer/camera state without
// scraping pixels. Harmless in normal use.
window.__forge3d = {
  viewer, state: S,
  camera: () => ({
    pos: viewer.camera.position.toArray().map((n) => +n.toFixed(4)),
    target: viewer.controls.target.toArray().map((n) => +n.toFixed(4)),
    dist: +viewer.camera.position.distanceTo(viewer.controls.target).toFixed(4),
  }),
  hasModel: () => !!viewer.root,
  wireframe: () => {
    let w = null;
    viewer.root?.traverse((o) => { if (o.isMesh && w === null) w = !!(Array.isArray(o.material) ? o.material[0] : o.material)?.wireframe; });
    return w;
  },
  loadAssets, openAsset,
};
