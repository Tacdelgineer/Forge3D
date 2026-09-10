import { Viewer } from '/static/viewer.js';

const $ = (id) => document.getElementById(id);
const ACCEPT = ['image/png', 'image/jpeg', 'image/webp'];
const ACCEPT_EXT = ['.png', '.jpg', '.jpeg', '.webp'];

const el = {
  drop: $('dropzone'), file: $('fileInput'), prev: $('refPreview'), dropEmpty: $('dropEmpty'),
  clearRef: $('clearRef'), fileErr: $('fileError'), fileName: $('fileName'),
  gen: $('generateBtn'), jobPanel: $('jobPanel'), jobState: $('jobState'),
  jobTime: $('jobTime'), jobHint: $('jobHint'),
  alert: $('alertBox'), alertTitle: $('alertTitle'), alertBody: $('alertBody'), alertData: $('alertData'),
  pill: $('statusPill'), mem: $('memReadout'),
  strip: $('assetStrip'), libEmpty: $('libEmpty'), count: $('assetCount'), refresh: $('refreshAssets'),
  vpEmpty: $('vpEmpty'), vpLoading: $('vpLoading'), vpName: $('vpName'), vpStats: $('vpStats'),
  reset: $('resetCam'), canvas: $('glcanvas'),
  modal: $('modal'), modalTitle: $('modalTitle'), modalBody: $('modalBody'),
  modalInput: $('modalInput'), modalOk: $('modalOk'), modalCancel: $('modalCancel'),
  toast: $('toast'),
};

const state = { file: null, jobId: null, poll: null, tick: null, assets: [], currentId: null, busy: false };
const viewer = new Viewer(el.canvas);

/* ------------------------------ helpers ------------------------------ */
const fmtBytes = (b) => !b ? '—' : b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1024).toFixed(0) + ' KB';
const fmtNum = (n) => n == null ? '—' : n.toLocaleString();
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? '—' : d.toLocaleString(undefined,
    { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
let toastTimer;
function toast(msg) {
  el.toast.textContent = msg; el.toast.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.toast.hidden = true; }, 2600);
}
function showAlert(title, body, data) {
  el.alertTitle.textContent = title;
  el.alertBody.textContent = body || '';
  el.alertData.innerHTML = '';
  if (data) {
    for (const [k, v] of Object.entries(data)) {
      el.alertData.insertAdjacentHTML('beforeend', `<dt>${k}</dt><dd>${v}</dd>`);
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
    if (isPrompt) { el.modalInput.focus(); el.modalInput.select(); }

    const done = (v) => {
      el.modal.hidden = true;
      el.modalOk.onclick = el.modalCancel.onclick = null;
      el.modalInput.onkeydown = null; document.onkeydown = null;
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
  state.file = f;
  el.prev.src = URL.createObjectURL(f);
  el.prev.hidden = false;
  el.dropEmpty.hidden = true;
  el.clearRef.hidden = false;
  el.fileName.textContent = `${f.name} · ${fmtBytes(f.size)}`;
  el.fileName.hidden = false;
  updateGenerate();
}
function clearFile() {
  state.file = null;
  if (el.prev.src.startsWith('blob:')) URL.revokeObjectURL(el.prev.src);
  el.prev.removeAttribute('src'); el.prev.hidden = true;
  el.dropEmpty.hidden = false; el.clearRef.hidden = true;
  el.fileName.hidden = true; el.file.value = '';
  updateGenerate();
}
el.drop.addEventListener('click', () => el.file.click());
el.drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.file.click(); } });
el.file.addEventListener('change', () => setFile(el.file.files[0]));
el.clearRef.addEventListener('click', (e) => { e.stopPropagation(); clearFile(); });
['dragenter', 'dragover'].forEach((t) => el.drop.addEventListener(t, (e) => {
  e.preventDefault(); el.drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((t) => el.drop.addEventListener(t, (e) => {
  e.preventDefault(); el.drop.classList.remove('over');
}));
el.drop.addEventListener('drop', (e) => setFile(e.dataTransfer.files[0]));

function updateGenerate() {
  el.gen.disabled = !state.file || state.busy;
}

/* ------------------------------ system status ------------------------------ */
async function pollSystem() {
  try {
    const s = await (await fetch('/system')).json();
    const m = s.memory;
    el.mem.textContent = `${m.available_gb.toFixed(1)} / ${m.min_required_gb.toFixed(0)} GiB`;
    let st = 'ready', label = 'Ready';
    if (s.generation.active || state.busy) { st = 'generating'; label = 'Generating'; }
    else if (!m.sufficient) { st = 'insufficient'; label = 'Insufficient Memory'; }
    el.pill.dataset.state = st;
    el.pill.querySelector('b').textContent = label;
  } catch {
    el.pill.dataset.state = 'unknown';
    el.pill.querySelector('b').textContent = 'Offline';
  }
}

/* ------------------------------ generation ------------------------------ */
const STATE_HINTS = {
  queued: 'Waiting for the worker.',
  loading_model: 'Loading TRELLIS.2 into memory — only happens on the first generation.',
  generating: 'Running the diffusion pipeline on the GPU.',
  exporting: 'Building the mesh and baking PBR textures into a GLB.',
  complete: 'Done.',
  error: '',
};

el.gen.addEventListener('click', async () => {
  if (!state.file || state.busy) return;
  hideAlert();
  const mode = document.querySelector('input[name=mode]:checked').value;
  const fd = new FormData();
  fd.append('image', state.file);
  fd.append('mode', mode);

  state.busy = true; updateGenerate();
  el.jobPanel.hidden = false;
  el.jobPanel.className = 'job';
  el.jobState.textContent = 'queued';
  el.jobTime.textContent = '0s';
  el.jobHint.textContent = STATE_HINTS.queued;

  let res, body;
  try {
    res = await fetch('/jobs', { method: 'POST', body: fd });
    body = await res.json();
  } catch (e) {
    failJob('Network error', String(e));
    return;
  }
  if (!res.ok) {
    if (body.error === 'insufficient_memory') {
      failJob('Insufficient memory',
        'Not enough available memory to safely start generation.',
        { Available: `${body.available_gb} GiB`, Required: `${body.required_gb} GiB` });
    } else if (body.error === 'unsupported_image') {
      failJob('Unsupported image', body.detail || 'Use PNG, JPG, JPEG or WebP.');
    } else if (body.error === 'generation_in_progress') {
      failJob('Already generating', 'Another generation is already running.');
    } else {
      failJob('Could not start', body.detail || body.error || `HTTP ${res.status}`);
    }
    return;
  }
  state.jobId = body.job_id;
  startTicker();
  state.poll = setInterval(pollJob, 1500);
  pollJob();
});

function startTicker() {
  const t0 = Date.now();
  clearInterval(state.tick);
  state.tick = setInterval(() => {
    el.jobTime.textContent = Math.round((Date.now() - t0) / 1000) + 's';
  }, 500);
}

function endJob() {
  clearInterval(state.poll); clearInterval(state.tick);
  state.poll = state.tick = null; state.jobId = null;
  state.busy = false; updateGenerate(); pollSystem();
}

function failJob(title, body, data) {
  el.jobPanel.className = 'job failed';
  el.jobState.textContent = 'error';
  el.jobHint.textContent = '';
  showAlert(title, body, data);
  endJob();
}

async function pollJob() {
  if (!state.jobId) return;
  let j;
  try { j = await (await fetch(`/jobs/${state.jobId}`)).json(); }
  catch { return; }
  el.jobState.textContent = j.state.replace(/_/g, ' ');
  el.jobTime.textContent = `${Math.round(j.elapsed_s)}s`;
  el.jobHint.textContent = STATE_HINTS[j.state] ?? '';

  if (j.state === 'complete') {
    el.jobPanel.className = 'job done';
    const t = j.result?.timings_s || {};
    el.jobHint.textContent = `Generated in ${t.total ?? '?'}s · ${j.result?.glb_mb ?? '?'} MB`;
    endJob();
    await loadAssets();
    if (j.asset_id) openAsset(j.asset_id);
    toast('Generation complete');
  } else if (j.state === 'error') {
    if (j.error_kind === 'insufficient_memory' && j.error_data) {
      failJob('Insufficient memory',
        'Not enough available memory to safely start generation.',
        { Available: `${j.error_data.available_gb} GiB`, Required: `${j.error_data.required_gb} GiB` });
    } else {
      failJob('Generation failed', j.error || 'Unknown error');
    }
  }
}

/* ------------------------------ asset library ------------------------------ */
async function loadAssets() {
  let data;
  try { data = await (await fetch('/assets')).json(); }
  catch { return; }
  state.assets = data.assets || [];
  el.count.textContent = state.assets.length;
  el.strip.innerHTML = '';
  if (!state.assets.length) {
    el.strip.appendChild(el.libEmpty); el.libEmpty.hidden = false;
    return;
  }
  el.libEmpty.hidden = true;
  for (const a of state.assets) el.strip.appendChild(card(a));
  markActive();
}

function card(a) {
  const d = document.createElement('div');
  d.className = 'card';
  d.dataset.id = a.id;
  const hq = a.mode !== '512';
  d.innerHTML = `
    <img class="card-thumb" alt="" loading="lazy"
         src="${a.has_thumbnail ? `/assets/${a.id}/thumbnail` : ''}">
    <div class="card-main">
      <span class="card-name" title="${escapeAttr(a.name)}">${escapeHtml(a.name)}</span>
      <span class="card-meta">
        <span class="tag ${hq ? 'hq' : ''}">${a.mode_label}</span>
        <span>${fmtBytes(a.glb_bytes)}</span>
      </span>
      <span class="card-meta">${fmtDate(a.created_at)}</span>
      <div class="card-acts">
        <button data-act="open">Open</button>
        <button data-act="download">Download</button>
        <button data-act="rename">Rename</button>
        <button data-act="delete" class="danger">Delete</button>
      </div>
    </div>`;
  d.addEventListener('click', (e) => {
    const act = e.target.dataset?.act;
    if (!act) { openAsset(a.id); return; }
    e.stopPropagation();
    if (act === 'open') openAsset(a.id);
    if (act === 'download') location.href = `/assets/${a.id}/download`;
    if (act === 'rename') renameAsset(a);
    if (act === 'delete') deleteAsset(a);
  });
  return d;
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const escapeAttr = escapeHtml;

function markActive() {
  el.strip.querySelectorAll('.card').forEach((c) =>
    c.classList.toggle('active', c.dataset.id === state.currentId));
}

async function openAsset(id) {
  const a = state.assets.find((x) => x.id === id);
  el.vpEmpty.hidden = true;
  el.vpLoading.hidden = false;
  try {
    const info = await viewer.load(`/assets/${id}/model.glb`);
    state.currentId = id;
    el.vpName.textContent = a ? a.name : id;
    el.vpStats.textContent =
      `${fmtNum(info.vertices)} verts · ${fmtNum(info.triangles)} tris` +
      (a ? ` · ${a.mode_label} · ${fmtBytes(a.glb_bytes)}` : '');
    markActive();
  } catch (e) {
    el.vpEmpty.hidden = false;
    toast('Could not load model: ' + e.message);
  } finally {
    el.vpLoading.hidden = true;
  }
}

async function renameAsset(a) {
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
  if (state.currentId === a.id) el.vpName.textContent = clean;
  toast('Renamed');
}

async function deleteAsset(a) {
  const ok = await ask({
    title: 'Delete asset?',
    body: `“${a.name}” and its GLB will be permanently removed from disk. This cannot be undone.`,
    okText: 'Delete', danger: true,
  });
  if (!ok) return;
  const res = await fetch(`/assets/${a.id}`, { method: 'DELETE' });
  if (!res.ok) { toast('Delete failed'); return; }
  if (state.currentId === a.id) {
    viewer.clear(); state.currentId = null;
    el.vpName.textContent = ''; el.vpStats.textContent = '';
    el.vpEmpty.hidden = false;
  }
  await loadAssets();
  toast('Deleted');
}

/* ------------------------------ wire up ------------------------------ */
el.reset.addEventListener('click', () => viewer.reset());
el.refresh.addEventListener('click', loadAssets);

pollSystem();
setInterval(pollSystem, 5000);
loadAssets();

// Test hook: lets the end-to-end suite inspect viewer/camera state without
// scraping pixels. Harmless in normal use.
window.__forge3d = {
  viewer, state,
  camera: () => ({
    pos: viewer.camera.position.toArray().map((n) => +n.toFixed(4)),
    target: viewer.controls.target.toArray().map((n) => +n.toFixed(4)),
    dist: +viewer.camera.position.distanceTo(viewer.controls.target).toFixed(4),
  }),
  hasModel: () => !!viewer.root,
  loadAssets, openAsset,
};
