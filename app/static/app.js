import { Viewer } from '/static/viewer.js';

const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

const ACCEPT = ['image/png', 'image/jpeg', 'image/webp'];
const ACCEPT_EXT = ['.png', '.jpg', '.jpeg', '.webp'];
const VIEWS = ['front', 'back', 'left', 'right'];
const VIEW_LABEL = { front: 'Front', back: 'Back', left: 'Left', right: 'Right' };
const STATE_LABEL = {
  queued: 'Queued', preparing: 'Freeing memory…', loading_model: 'Loading model',
  generating: 'Generating', exporting: 'Exporting GLB', restoring: 'Restoring services…',
  complete: 'Complete', error: 'Failed',
};
const STATE_HINT = {
  queued: 'Waiting for the worker.',
  preparing: 'Pausing the selected AI workloads and waiting for the memory to come back.',
  loading_model: 'Loading the model into memory — only on the first run after a restart.',
  generating: 'Running the diffusion pipeline on the GPU.',
  exporting: 'Remeshing, unwrapping UVs and writing the GLB.',
  restoring: 'Putting the paused workloads back the way they were.',
};
const STEP_ORDER = ['queued', 'preparing', 'loading_model', 'generating', 'exporting',
                    'restoring', 'complete'];

const el = {
  app: $('app'), model: $('modelSelect'), modelNote: $('modelNote'),
  headroom: $('headroom'), pill: $('statusPill'),
  refHead: $('refHead'), guide: $('guideText'),
  drop: $('dropzone'), file: $('fileInput'), prev: $('refPreview'), dropEmpty: $('dropEmpty'),
  clearRef: $('clearRef'), changeRef: $('changeRef'), fileErr: $('fileError'), fileName: $('fileName'),
  viewGrid: $('viewGrid'),
  modes: $('modes'), modeNote: $('modeNote'), qualityAside: $('qualityAside'),
  excl: $('excl'), exclToggle: $('exclToggle'), exclDetail: $('exclDetail'),
  exclWarn: $('exclWarn'),
  gen: $('generateBtn'), genLabel: $('genLabel'), genHint: $('genHint'),
  alert: $('alertBox'), alertTitle: $('alertTitle'), alertBody: $('alertBody'), alertData: $('alertData'),
  stage: $('viewport'), canvas: $('glcanvas'), vpEmpty: $('vpEmpty'), vpLoading: $('vpLoading'),
  vpTitle: $('vpTitle'), vpName: $('vpName'), vpStats: $('vpStats'), veil: $('dropVeil'),
  toolRotate: $('toolRotate'), toolWire: $('toolWire'), reset: $('resetCam'), toggleInspector: $('toggleInspector'),
  run: $('jobPanel'), jobState: $('jobState'), jobMeta: $('jobMeta'), jobTime: $('jobTime'),
  steps: $('jobSteps'), jobHint: $('jobHint'),
  genKv: $('genKv'), licNote: $('licNote'), advanced: $('advanced'),
  seed: $('seedInput'), rollSeed: $('rollSeed'), randomSeed: $('randomSeed'),
  texField: $('texField'), texSeg: $('texSeg'),
  assetEmpty: $('assetEmpty'), assetInfo: $('assetInfo'), aiName: $('aiName'), aiKv: $('aiKv'),
  aiDownload: $('aiDownload'), aiReuse: $('aiReuse'), aiRename: $('aiRename'), aiDelete: $('aiDelete'),
  lib: $('library'), libToggle: $('libToggle'), mini: $('libMini'), count: $('assetCount'),
  refresh: $('refreshAssets'), strip: $('assetStrip'), libEmpty: $('libEmpty'),
  modal: $('modal'), modalTitle: $('modalTitle'), modalBody: $('modalBody'),
  modalInput: $('modalInput'), modalOk: $('modalOk'), modalCancel: $('modalCancel'),
  toast: $('toast'),
};

const S = {
  sys: null, gen: null, mode: null, texture: null, seedMax: 2147483647,
  file: null,                       // single-reference generators
  views: { front: null, back: null, left: null, right: null },  // multi-view
  exclusive: false,
  jobId: null, poll: null, tick: null, busy: false, sawLoad: false, hideTimer: null,
  assets: [], currentId: null,
};
const viewer = new Viewer(el.canvas);

/* ------------------------------ helpers ------------------------------ */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtBytes = (b) => !b ? '—' : b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1024).toFixed(0) + ' KB';
const fmtNum = (n) => n == null ? '—' : n.toLocaleString();
const fmtViews = (vs) => (vs && vs.length)
  ? VIEWS.filter((v) => vs.includes(v)).map((v) => VIEW_LABEL[v]).join(' · ')
  : '—';
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

/* ------------------------------ generator state ------------------------------ */
const curGen = () => S.sys?.generators?.find((g) => g.id === S.gen) || null;
const isMulti = () => curGen()?.inputs === 'views';
const modeInfo = (id = S.mode, gen = S.gen) =>
  S.sys?.generators?.find((g) => g.id === gen)?.modes.find((m) => m.id === id);

/* ------------------------------ reference image(s) ------------------------------ */
// Client-side format check only; the backend re-validates by decoding the bytes.
/* ------------------------------ exclusive ------------------------------ */
const exclSys = () => S.sys?.exclusive || null;
// Is the checkbox usable at all? Only when the host helper is there, has
// something to free, and is not already holding a lease for someone else.
const exclUsable = () => {
  const x = exclSys();
  return !!(x && x.supported && !x.held && x.releasable_gb > 0);
};
// The availability a mode has under the CURRENT toggle position. With the
// toggle off this is exactly the pre-Step-6 value.
const modeAvailable = (m) => (m ? (S.exclusive ? m.exclusive_available : m.available) : false);
const modeReason = (m) => (m ? (S.exclusive ? m.exclusive_reason : m.reason) : '');
const modeFloor = (m) => (m ? (S.exclusive ? m.exclusive_projected_min_gb : m.projected_min_gb) : null);

function renderExclusive() {
  const x = exclSys();
  const g = curGen();
  // Only TRELLIS has modes this can unlock, and it is the only generator whose
  // worker is not itself one of the things being paused.
  const relevant = !!x && g?.id === 'trellis';
  el.excl.hidden = !relevant;
  if (!relevant) { if (S.exclusive) { S.exclusive = false; el.exclToggle.checked = false; } return; }

  const usable = exclUsable() && !S.busy;
  el.excl.dataset.enabled = String(usable);
  el.exclToggle.disabled = !usable;
  if (!usable && S.exclusive) { S.exclusive = false; el.exclToggle.checked = false; }
  el.exclToggle.checked = S.exclusive;

  // Never imply memory we cannot actually free.
  let warn = '';
  let bad = false;
  if (!x.supported) warn = x.reason;
  else if (x.held) warn = 'Exclusive mode is currently held by another run.';
  else if (!x.releasable_gb) warn = 'Nothing is currently releasable, so this would free no memory.';
  const lr = x.last_restore;
  if (lr && !lr.ok) {
    bad = true;
    warn = `A previous run did not restore everything (${lr.trigger}): ${lr.errors.join('; ')}. `
         + 'Run `sudo forge3d-resctl recover` on the host.';
  }
  el.exclWarn.hidden = !warn;
  el.exclWarn.textContent = warn;
  el.exclWarn.classList.toggle('bad', bad);

  const items = x.would_pause || [];
  el.exclDetail.hidden = !(S.exclusive && items.length);
  if (!el.exclDetail.hidden) {
    el.exclDetail.innerHTML =
      `<b>Will pause for this run, then restore:</b><ul>${items.map((i) =>
        `<li>${esc(i.label)} <b>${esc(i.name)}</b>`
        + (i.est_gb > 0 ? ` <span class="est">~${i.est_gb} GiB</span>` : '')
        + `</li>`).join('')}</ul>`
      + `<div style="margin-top:6px">Projected MemAvailable <span class="est">`
      + `~${x.projected_available_gb} GiB</span> (estimated from what each workload `
      + `holds now; the run is gated on the real figure measured after they stop).</div>`;
  }
}

el.exclToggle.addEventListener('change', () => {
  S.exclusive = el.exclToggle.checked;
  hideAlert();
  renderExclusive();
  renderModes();
  renderModeNote();
  updateGenerate();
});

function imageError(f) {
  const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
  if (!ACCEPT.includes(f.type) && !ACCEPT_EXT.includes(ext)) {
    return `Unsupported file type "${f.type || ext || 'unknown'}". Use PNG, JPG, JPEG or WebP.`;
  }
  return null;
}

function setFile(f) {
  el.fileErr.hidden = true;
  if (!f) return;
  if (isMulti()) { setView('front', f); return; }
  const err = imageError(f);
  if (err) {
    el.fileErr.textContent = err;
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

/* ---- multi-view slots: one real image per view, sent separately ---- */
const slotOf = (view) => el.viewGrid.querySelector(`.slot[data-view="${view}"]`);

function setView(view, f) {
  el.fileErr.hidden = true;
  if (!f) return;
  const err = imageError(f);
  if (err) {
    el.fileErr.textContent = err;
    el.fileErr.hidden = false;
    return;
  }
  const slot = slotOf(view);
  const img = slot.querySelector('img');
  if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
  S.views[view] = f;
  img.src = URL.createObjectURL(f);
  img.hidden = false;
  slot.querySelector('.slot-empty').hidden = true;
  slot.querySelector('.slot-x').hidden = false;
  slot.classList.add('has-img');
  hideAlert();
  updateGenerate();
}

function clearView(view) {
  const slot = slotOf(view);
  const img = slot.querySelector('img');
  if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
  S.views[view] = null;
  img.removeAttribute('src');
  img.hidden = true;
  slot.querySelector('.slot-empty').hidden = false;
  slot.querySelector('.slot-x').hidden = true;
  slot.querySelector('input[type=file]').value = '';
  slot.classList.remove('has-img');
  updateGenerate();
}

for (const view of VIEWS) {
  const slot = slotOf(view);
  const input = slot.querySelector('input[type=file]');
  input.addEventListener('change', () => setView(view, input.files[0]));
  // The clear button sits inside the <label>, whose labeled control is the file
  // input, so suppress the label's own activation or removing a view would
  // immediately reopen the file picker.
  slot.querySelector('.slot-x').addEventListener('click', (e) => {
    e.preventDefault(); e.stopPropagation(); clearView(view);
  });
  ['dragenter', 'dragover'].forEach((t) => slot.addEventListener(t, (e) => {
    e.preventDefault(); slot.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((t) => slot.addEventListener(t, (e) => {
    e.preventDefault(); slot.classList.remove('over');
  }));
  slot.addEventListener('drop', (e) => { e.stopPropagation(); setView(view, e.dataTransfer.files[0]); });
}

const providedViews = () => VIEWS.filter((v) => S.views[v]);
const hasInput = () => (isMulti() ? !!S.views.front : !!S.file);

// The whole stage is a drop target too; in multi-view mode it fills Front.
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
async function pollSystem() {
  let s;
  try { s = await (await fetch('/system')).json(); }
  catch { setStatus('unknown', 'Offline', 'The Forge3D backend is not responding.'); return; }
  S.sys = s;
  S.seedMax = s.seed_max ?? S.seedMax;
  if (S.gen === null || !s.generators.some((g) => g.id === S.gen)) {
    S.gen = store.get('forge3d.generator', s.default_generator);
    if (!s.generators.some((g) => g.id === S.gen)) S.gen = s.default_generator;
  }
  if (S.texture === null) S.texture = s.texture.default;
  if (S.mode === null || !modeInfo(S.mode)) S.mode = curGen()?.default_mode ?? null;
  renderGenerators();
  renderInputs();
  renderExclusive();
  renderModes();
  renderTexture();
  renderStatus();
  renderGenKv();
  updateGenerate();
}

function renderGenerators() {
  const gens = S.sys.generators;
  const sig = gens.map((g) => g.id).join(',');
  if (el.model.dataset.sig !== sig) {
    el.model.innerHTML = gens.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
    el.model.dataset.sig = sig;
    el.model.addEventListener('change', onGeneratorChange, { once: true });
  }
  el.model.value = S.gen;
  const g = curGen();
  el.modelNote.textContent = g ? g.blurb : '';
  el.modelNote.title = g ? `${g.upstream.weights} · ${g.upstream.license}` : '';
  el.model.title = g && !g.backend_ok ? (g.backend_reason || '') : '';
  el.model.classList.toggle('warn', !!g && !g.backend_ok);
}

function onGeneratorChange() {
  S.gen = el.model.value;
  store.set('forge3d.generator', S.gen);
  const g = curGen();
  if (!modeInfo(S.mode)) S.mode = g?.default_mode ?? null;
  hideAlert();
  el.fileErr.hidden = true;
  // Re-attach the one-shot listener and redraw everything the generator owns.
  el.model.addEventListener('change', onGeneratorChange, { once: true });
  renderGenerators();
  renderInputs();
  renderExclusive();
  el.modes.dataset.gen = '';   // force a rebuild: different modes entirely
  renderModes();
  renderStatus();
  renderGenKv();
  updateGenerate();
}

// Single uploader vs four labelled view slots. Files already chosen are kept,
// so switching back and forth does not lose a selection.
function renderInputs() {
  const g = curGen();
  if (!g) return;
  const multi = g.inputs === 'views';
  el.drop.hidden = multi;
  el.fileName.hidden = multi || !S.file;
  el.changeRef.hidden = multi || !S.file;
  el.viewGrid.hidden = !multi;
  el.refHead.textContent = multi ? 'Reference views' : 'Reference';
  el.guide.textContent = multi
    ? 'Front is required; Back, Left and Right are optional. Each view is sent to the model as a separate image — use consistent lighting and scale.'
    : 'Best results: one isolated subject, full object visible, simple background.';
  el.qualityAside.textContent = g.texture_control ? 'geometry' : 'output';
  el.texField.hidden = !g.texture_control;
}

function renderModes() {
  const g = curGen();
  if (!g) return;
  if (el.modes.dataset.gen !== g.id) {
    el.modes.dataset.gen = g.id;
    el.modes.innerHTML = g.modes.map((m) => `
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
  }
  for (const m of g.modes) {
    const lab = el.modes.querySelector(`[data-mode="${m.id}"]`);
    if (!lab) continue;
    const ok = modeAvailable(m);
    lab.dataset.available = String(ok);
    // Flagged so the card can show that this mode is only reachable because
    // Exclusive mode is on, rather than looking like ordinary availability.
    lab.dataset.exclusiveOnly = String(!!(S.exclusive && ok && !m.available));
    lab.querySelector('input').checked = m.id === S.mode;
    const st = lab.querySelector('.mode-state');
    st.className = 'mode-state ' + (ok ? 'mode-time' : 'mode-avail');
    st.textContent = ok
      ? (S.exclusive && !m.available ? `${m.time_hint} · exclusive` : m.time_hint)
      : (g.backend_ok ? 'Unavailable now' : 'Worker offline');
    lab.title = ok
      ? `Peak ~${m.peak_gb} GiB (${m.peak_measured ? 'measured' : 'estimate'}) · leaves ~${modeFloor(m)} GiB free`
      : (modeReason(m) || '');
  }
  renderModeNote();
}

el.modes.addEventListener('change', (e) => {
  if (e.target.name !== 'mode') return;
  S.mode = e.target.value;
  hideAlert();
  renderExclusive();
  renderModeNote();
  renderStatus();
  renderGenKv();
  updateGenerate();
});

function renderModeNote() {
  const m = modeInfo();
  const ok = modeAvailable(m);
  el.modeNote.hidden = !m || ok;
  if (m && !ok) el.modeNote.textContent = modeReason(m);
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

// Ready / Model loaded / Generating / Can't start <mode> / Worker offline.
// A resident model is not a memory problem on its own: the backend credits
// what it already holds, so the pill only goes red when the *selected*
// generator and mode genuinely cannot start.
function renderStatus() {
  const s = S.sys;
  if (!s) return;
  const g = curGen();
  const m = modeInfo();
  if (S.busy || s.state === 'generating') {
    setStatus('generating', 'Generating');
  } else if (g && !g.backend_ok) {
    setStatus('blocked', 'Worker offline', g.backend_reason || '');
  } else if (m && !m.available) {
    setStatus('blocked', `Can't start ${m.label}`, m.reason || '');
  } else if (g && g.loaded) {
    setStatus('loaded', 'Model loaded', `${g.name} is resident — the next run skips the load.`);
  } else {
    setStatus('ready', 'Ready', `${g ? g.name : 'The model'} loads on the first generation.`);
  }
  const mem = s.memory;
  el.headroom.textContent = `${mem.headroom_gb.toFixed(1)} GiB headroom`;
  el.headroom.title =
    `MemAvailable ${mem.available_gb.toFixed(1)} GiB + ${mem.forge3d_footprint_gb.toFixed(1)} GiB Forge3D already holds.\n` +
    `A run must leave at least ${mem.floor_gb} GiB free for other services.`;
}

function renderGenKv() {
  const s = S.sys;
  const g = curGen();
  const m = modeInfo();
  if (!s || !g || !m) return;
  const rows = [
    ['Model', esc(g.name)],
    ['Quality', `${esc(m.label)} · <span class="mono">${esc(m.id)}</span>`],
  ];
  if (g.inputs === 'views') {
    const vs = providedViews();
    rows.push(['Views', vs.length ? esc(fmtViews(vs)) : 'Front required', vs.length ? '' : 'bad']);
  }
  if (g.texture_control) rows.push(['Texture', `${S.texture} × ${S.texture}`]);
  rows.push(
    ['Peak memory', `~${m.peak_gb} GiB · ${m.peak_measured ? 'measured' : 'estimate'}`],
    ['Leaves free', `~${m.projected_min_gb} GiB`, m.available ? 'ok' : 'bad'],
    ['Floor', `${s.memory.floor_gb} GiB`],
  );
  el.genKv.innerHTML = rows.map(([k, v, cls]) => `<dt>${k}</dt><dd class="${cls || ''}">${v}</dd>`).join('');

  const up = g.upstream;
  el.licNote.hidden = false;
  el.licNote.innerHTML = up.restricted
    ? `<b>${esc(up.license)}</b> — ${esc(up.license_note)} <a href="${esc(up.license_url)}" target="_blank" rel="noreferrer noopener">Licence</a>`
    : `${esc(up.license)} · <a href="${esc(up.license_url)}" target="_blank" rel="noreferrer noopener">Licence</a>`;
  el.licNote.classList.toggle('restricted', !!up.restricted);
}

function updateGenerate() {
  const g = curGen();
  const m = modeInfo();
  let hint;
  let ok = false;
  if (S.busy) hint = 'A generation is running.';
  else if (!g) hint = 'Connecting…';
  else if (!g.backend_ok) hint = g.backend_reason || `${g.name} is not available.`;
  else if (!hasInput()) hint = isMulti() ? 'Add a Front view to begin.' : 'Add a reference image to begin.';
  else if (!m) hint = 'Connecting…';
  else if (!modeAvailable(m)) hint = `${m.label} can't start safely right now.`;
  else {
    ok = true;
    const views = isMulti() ? ` · ${providedViews().length} view${providedViews().length === 1 ? '' : 's'}` : '';
    const xc = S.exclusive ? ' · exclusive' : '';
    hint = `${m.label} · ${m.time_hint}${views}${xc}${g.loaded ? '' : ' · plus first load'}`;
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
  const g = curGen();
  const m = modeInfo();
  const seed = parseSeed(el.seed.value);
  if (seed === null) {
    el.advanced.open = true;
    el.seed.classList.add('invalid');
    showAlert('Invalid seed', `Use a whole number from 0 to ${S.seedMax.toLocaleString()}.`);
    return;
  }
  const fd = new FormData();
  fd.append('generator', S.gen);
  fd.append('mode', S.mode);
  fd.append('seed', String(seed));
  fd.append('texture_size', String(S.texture));
  if (S.exclusive) fd.append('exclusive', 'true');
  const views = providedViews();
  if (isMulti()) {
    for (const v of views) fd.append(v, S.views[v]);
  } else {
    fd.append('image', S.file);
  }

  setBusy(true);
  S.sawLoad = false;
  showRun({ state: 'queued', mode_label: m.label, seed, texture_size: S.texture,
            generator_name: g.name, views: isMulti() ? views : [],
            exclusive: S.exclusive });

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
    } else if (body.error === 'backend_unavailable') {
      failStart(`${g.name} unavailable`, body.reason || 'The worker for this generator is not running.');
    } else if (body.error === 'view_required') {
      failStart('Front view required',
        `Add ${(body.missing || ['front']).map((v) => VIEW_LABEL[v] || v).join(', ')} to generate.`);
    } else if (body.error === 'unsupported_image') {
      failStart('Unsupported image', body.detail || 'Use PNG, JPG, JPEG or WebP.');
    } else if (body.error === 'exclusive_unavailable') {
      failStart('Exclusive mode unavailable', body.reason || 'The host helper is not available.');
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

// A workload left down is the one outcome that must never be quiet, so it is
// reported whether the generation itself succeeded or failed.
function reportRestore(j) {
  const r = j.exclusive_report;
  if (!r) return;
  if (r.restore_ok) {
    const back = (r.restored || []).filter((a) => a.ok).length;
    if (back) toast(`Services restored (${back})`);
    return;
  }
  showAlert('Services were NOT fully restored',
    `${(r.restore_errors || []).join('\n')}\n\n`
    + 'Run `sudo forge3d-resctl recover` on the DGX to put them back.',
    { 'Freed at start': `${r.freed_gb ?? '—'} GiB`,
      'MemAvailable before': `${r.available_before_gb ?? '—'} GiB` });
}

function runMeta(j) {
  const bits = [j.generator_name, j.mode_label, `seed ${j.seed}`];
  if (j.exclusive) bits.push('exclusive');
  if (j.views && j.views.length) bits.push(fmtViews(j.views));
  else if (j.texture_size && curGen()?.texture_control) bits.push(`${j.texture_size}px texture`);
  return bits.join(' · ');
}

function showRun(j) {
  clearTimeout(S.hideTimer);
  el.steps.querySelectorAll('.excl-step').forEach((li) => { li.hidden = !j.exclusive; });
  el.run.hidden = false;
  el.run.className = 'run';
  el.jobState.textContent = STATE_LABEL[j.state] || j.state;
  el.jobMeta.textContent = runMeta(j);
  el.jobTime.textContent = '0s';
  el.jobHint.textContent = STATE_HINT[j.state] || '';
  renderSteps(j.state);
}

function renderSteps(state) {
  const cur = STEP_ORDER.indexOf(state);
  if (state === 'loading_model') S.sawLoad = true;
  const loadIdx = STEP_ORDER.indexOf('loading_model');
  el.steps.querySelectorAll('li').forEach((li) => {
    const i = STEP_ORDER.indexOf(li.dataset.step);
    let c = li.classList.contains('excl-step') ? 'excl-step' : '';
    if (state === 'complete' || i < cur) c += ' done';
    else if (i === cur) c += ' current';
    if (li.dataset.step === 'loading_model' && !S.sawLoad && cur > loadIdx) c += ' skipped';
    li.className = c.trim();
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
  el.jobMeta.textContent = runMeta(j);
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
    reportRestore(j);
    S.hideTimer = setTimeout(() => { el.run.hidden = true; }, 9000);
  } else if (j.state === 'error') {
    if (j.error_kind === 'insufficient_memory' && j.error_data) {
      failJob(`Can't start ${j.error_data.mode_label}`,
        `Not enough available memory to safely start generation.\n${j.error_data.reason || ''}`,
        memData(j.error_data));
    } else if (j.error_kind === 'backend_unavailable') {
      failJob('Worker unavailable', j.error || 'The worker for this generator is not running.');
    } else if (j.error_kind === 'exclusive_unavailable') {
      failJob('Exclusive mode unavailable', j.error || 'The host helper is not available.');
    } else {
      failJob('Generation failed', j.error || 'Unknown error');
    }
    // A failed generation still had to put the workloads back; say if it did not.
    if (j.exclusive_report && !j.exclusive_report.restore_ok) reportRestore(j);
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
const genTag = (id) => (id === 'hunyuan21' ? 'HY 2.1' : id === 'hunyuan2mv' ? 'HY MV' : 'TRELLIS');
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
        <span class="card-meta">
          <span class="tag model">${esc(genTag(a.generator))}</span>
          <span class="tag ${tagClass(a.mode)}">${esc(a.mode_label)}</span>
          <span>${fmtBytes(a.glb_bytes)}</span>
        </span>
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
  // Assets generated before Step 5 carry no generator field; assets.py
  // defaults them to TRELLIS rather than showing a gap.
  const rows = [
    ['Model', esc(a.generator_name || genTag(a.generator))],
    ['Quality', `${esc(a.mode_label)} · <span class="mono">${esc(a.mode)}</span>`],
    ['Created', esc(fmtDate(a.created_at))],
    ['Seed', a.seed != null ? `<span class="mono">${a.seed}</span>` : '—'],
  ];
  if (a.views && a.views.length > 1) rows.push(['Views', esc(fmtViews(a.views))]);
  if (a.texture_size) rows.push(['Texture', `${a.texture_size} × ${a.texture_size}`]);
  rows.push(
    ['Geometry', `${fmtNum(a.vertices)} v · ${fmtNum(a.faces)} f`],
    ['GLB', fmtBytes(a.glb_bytes)],
    ['Time', t.total != null ? `${Math.round(t.total)} s` : '—',
      t.total != null ? `load ${t.pipeline_load} s · generate ${t.generation} s · export ${t.glb_export} s` : ''],
    ['Source', esc(a.source_filename || '—'), a.source_filename || ''],
  );
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
  const gen = a.generator && S.sys?.generators?.some((g) => g.id === a.generator) ? a.generator : S.gen;
  if (gen !== S.gen) {
    S.gen = gen;
    store.set('forge3d.generator', S.gen);
    el.modes.dataset.gen = '';
    renderGenerators();
    renderInputs();
  }
  if (modeInfo(a.mode, gen)) S.mode = a.mode;
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

// Test hook: lets the end-to-end suite drive the studio and inspect viewer
// state without scraping pixels. Harmless in normal use.
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
  selectGenerator: (id) => { el.model.value = id; onGeneratorChange(); },
  setView, clearView, setFile, providedViews,
};
