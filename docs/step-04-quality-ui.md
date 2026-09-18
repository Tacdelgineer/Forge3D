# Step 04 — Quality controls + studio UI

> Historical milestone record. Use [the current README](../README.md) and [SETUP.md](SETUP.md) for installation and current behavior.

**Host:** `aitopatom-c85a` — GIGABYTE AI TOP ATOM (GB10 Grace-Blackwell, DGX Spark class)
**Scope:** Better generation experience on the existing TRELLIS.2 backend: quality modes,
seed and texture controls, an honest memory gate, and a studio-style UI. No new backends,
no new services, no frontend build system.
**Prerequisites:** [step-01](step-01-dgx-audit.md) · [step-02](step-02-trellis-backend.md) · [step-03](step-03-dashboard.md)

---

## What changed

| Area | Change |
| --- | --- |
| Quality | Three dashboard modes: Standard `512`, High Quality `1024_cascade`, **Ultra `1536_cascade` (Experimental)**. Each is individually gated by memory. |
| Texture | Texture resolution is now a real control (1K / 2K / 4K, default 4K). The UI states that 4K is *texture* pixels, not geometry. |
| Seed | Random by default, visible before you generate, editable, and reusable from any asset. |
| Memory | The gate now credits memory Forge3D already holds and projects the lowest `MemAvailable` each mode will reach. After every run, everything but the model is handed back. |
| Measurement | The engine samples memory every 0.5 s for the whole run, including GLB export, and records the true minimum. |
| UI | Rebuilt as a three-column studio: reference + quality left, dominant viewport centre, inspector right, collapsible library bottom. |
| Model picker | Top bar shows **Model: TRELLIS.2**, rendered from `/system.models`, so another generator can appear later. |

Nothing about the Step 2 CUDA build changed. The Dockerfile is untouched; the image rebuilt only
its final `COPY` layers.

---

## Quality modes

### Exact TRELLIS.2 pipeline identifiers

From `Trellis2ImageTo3DPipeline.run(pipeline_type=...)` in
`trellis2/pipelines/trellis2_image_to_3d.py` (pinned commit `75fbf01…`):

```
'512'   '1024'   '1024_cascade'   '1536_cascade'
```

Nothing else is accepted; anything else raises `ValueError`. The official Gradio demo
(`app.py`) maps its "1536" resolution radio to `'1536_cascade'`.

| Dashboard mode | `pipeline_type` | Checkpoints used | Geometry |
| --- | --- | --- | --- |
| **Standard** | `512` | shape 512 + texture 512 | 512³ |
| **High Quality** | `1024_cascade` | shape 512 → shape 1024 + texture 1024 | 1024³ |
| **Ultra** (Experimental) | `1536_cascade` | shape 512 → shape 1024 + texture 1024 | **up to** 1536³ |
| *(API only)* | `1024` | shape 1024 + texture 1024, no cascade | 1024³ |

### What `1536_cascade` actually does

It reuses **the same three checkpoints** as `1024_cascade`. The only differences are the
target resolution handed to `sample_shape_slat_cascade` (1536 instead of 1024) and the decode
resolution. Both cascades share one token budget, `max_num_tokens = 49152`:

```python
hr_resolution = resolution          # 1536
while True:
    ...quantise the upsampled coords to hr_resolution // 16...
    if num_tokens < max_num_tokens or hr_resolution == 1024:
        break
    hr_resolution -= 128            # 1536 -> 1408 -> 1280 -> 1152 -> 1024
```

So "Ultra" is honestly **up to 1536³**. A complex object that exceeds the token budget is
silently stepped down in 128 increments, never below 1024, and TRELLIS prints
`Due to the limited number of tokens, the resolution is reduced to N`. The dashboard labels it
"Up to 1536³ geometry" for that reason.

---

## Texture resolution

`o_voxel.postprocess.to_glb(texture_size=...)` is genuinely configurable. It allocates the
bake buffers at `texture_size × texture_size` on the GPU and bakes base colour and
metallic-roughness maps at that size. The official demo exposes 1024–4096.

Forge3D offers **1K / 2K / 4K**, default **4K (4096×4096)**, the same PBR output as every
earlier milestone. The inspector says it outright:

> **4K** means 4096×4096 **texture** pixels — surface colour and material detail. It does not
> change geometry; Quality does.

`POST /jobs` and `POST /generate` accept `texture_size` ∈ {1024, 2048, 4096}; anything else
returns `400 invalid_texture_size`.

---

## Seed behaviour

- **Random by default.** The page rolls a seed on load. It sits visibly in the Seed field, so
  you always know which seed the *next* run will use.
- **"New random seed after each run"** (on by default) rolls a fresh seed as soon as a run
  starts. The seed actually used is shown in the run panel and stored on the asset.
- **Typing a seed** turns that off automatically: typing one means you want it.
- **Reuse settings** on any asset restores its mode, seed and texture resolution.
- Range **0 … 2,147,483,647** (`2³¹ − 1`), the same as TRELLIS.2's own demo
  (`np.iinfo(np.int32).max`). Out-of-range or non-numeric seeds return `400 invalid_seed`.
- `POST /jobs` with no seed picks a random one server-side (`secrets.randbelow`). The legacy
  `POST /generate` keeps its Step 2 default of 42.

### Reproducibility — honest version

TRELLIS seeds its noise with `torch.manual_seed(seed)`. Measured in this milestone: two runs
with the **same image and seed** (20260910, Standard) produced a **bit-identical raw mesh**
out of `pipeline.run()`: 1,033,836 vertices and 2,083,564 faces both times, 0.000 %
difference. Every metadata.json now records these pre-export counts as `mesh_raw`, so this
can be checked after the fact.

The **exported GLB can still differ slightly.** Run 2 deliberately used a 2K texture, so its
GLB (755,183 vertices vs 757,438) is not a like-for-like comparison; UV charting depends on
texture size. The like-for-like evidence comes from Steps 2 and 3: the same RGBA crown, seed
42, `512` and 4K texture exported **803,278 vs 811,111** GLB vertices. With the raw geometry
now shown to be identical, that points at the export stage (CuMesh remeshing, xatlas UV
unwrapping) rather than the diffusion model.

So: **same image + same seed reproduce the geometry TRELLIS generates. The final GLB is the
same shape and may differ by about 1 % in vertex count.** The inspector's help text says
exactly this.

A side measurement from the same pair: at 2K the GLB export took **12.8 s**, against
**29.9 s** at 4K. Texture size is the main lever on export time.

---

## Memory policy

### The bug: "Insufficient Memory" right after a successful run

The Step 2/3 gate compared bare `MemAvailable` against 65 GiB. Investigating the live
container showed the gate was wrong in two ways.

**1. It counted Forge3D's own model against itself.** A resident model is memory Forge3D has
already paid for and will reuse. Refusing a run because the model is loaded is backwards.

**2. Worse: finished runs left a lot more than the model behind.** Measured on the running
container before this milestone, after the user's own High Quality runs:

| Held by the idle Forge3D process | Size |
| --- | ---: |
| `RssAnon` (anonymous heap: model weights + leftovers) | **23.9 GiB** |
| PyTorch CUDA cache — *reserved* | **6.0 GiB** |
| PyTorch CUDA cache — *actually allocated* | **0.01 GiB** |
| TRELLIS.2 model weights (for comparison) | ~16 GiB |

About **14 GiB was freeable garbage**: PyTorch's caching allocator holding blocks it wasn't
using, plus glibc heap pages never returned to the OS. The model alone does not push
`MemAvailable` under 65 GiB; the garbage did.

A side finding: `docker stats` reported **39.15 GiB** for that same container. It counts
reclaimable page cache (the safetensors files it read), which the kernel already includes in
`MemAvailable`. Using it double-counts. Forge3D measures `RssAnon` instead.

### The fix, part 1 — give memory back

After every run, `engine.release_memory()` runs `gc.collect()`, `torch.cuda.empty_cache()`
and glibc `malloc_trim(0)`. The diffusion stage's CUDA cache is also released between
generation and GLB export, instead of being carried through the export. On GB10 all of it
lands straight back in `MemAvailable`, because there is only one pool.

Measured on the live container right after a Step 4 Standard run (model resident, cleanup
done), against the idle process before this milestone (after the user's High Quality runs):

| Idle Forge3D process | Before Step 4 (after HQ) | Step 4 (after Standard) |
| --- | ---: | ---: |
| `RssAnon` | 23.9 GiB | **18.5 GiB** |
| torch CUDA *reserved* | 6.0 GiB (0.01 GiB in use) | **0.05 GiB** |
| nvidia-smi, per process | 6,371 MiB | **275 MiB** |

The GPU cache is now returned in full. `RssAnon` settles about 2.5 GiB above the ~16 GiB of
model weights: glibc heap fragmentation that `malloc_trim` cannot release. (The two columns
come after different modes, so the CPU-side comparison is indicative. The GPU side is
unambiguous.)

The effect on the bug, from the browser test: right after the run, `MemAvailable` was
**60.6 GiB**, below 65, which the old gate would have shown as "Insufficient Memory".
Headroom was **79.6 GiB** and Standard projected to ~53 GiB, so the pill correctly read
**Model loaded**.

### The fix, part 2 — an honest, per-mode gate

```
footprint   = RssAnon + torch CUDA reserved (+ context)      what Forge3D holds now
headroom    = MemAvailable + footprint                        memory available to Forge3D
projected   = headroom − peak(mode)                           lowest MemAvailable the run reaches

allow mode  ⇔  headroom ≥ 65 GiB            (TRELLIS_MIN_AVAILABLE_GB — unchanged)
            and projected ≥ 40 GiB          (TRELLIS_PROTECTED_FLOOR_GB)
```

- **65 GiB is not lowered.** It is still the minimum headroom. It is now measured against
  headroom rather than bare `MemAvailable`, so a resident model no longer counts against
  itself.
- **40 GiB is content-factory's own hard gate** (`VISUAL_MIN_AVAILABLE_GB=40`, unchanged).
  If a Forge3D run takes `MemAvailable` under 40, content-factory's next visual job fails.
  That makes it the principled floor, not an arbitrary one.
- For Standard, the per-mode rule is **stricter** than before: `40 + 27 = 67 GiB` of
  headroom versus the old flat 65.
- The gate runs three times: when `/system` reports availability (every 4 s), at `POST /jobs`,
  and again inside the generation lock, immediately before any allocation.

### Peak memory per mode

"Peak" is what a whole run (load + generate + export) takes out of `MemAvailable`, relative
to Forge3D holding nothing. It is stored in `config.MODE_PEAK_GB` and rounded up.

| Mode | `MODE_PEAK_GB` | Source |
| --- | ---: | --- |
| Standard `512` | **27** | measured twice: 26.9 (Step 2, external 10 s sampler) and **26.65** (Step 4, in-engine 0.5 s sampler, cold, RGB input through background removal) |
| High Quality `1024_cascade` | **44** | measured: 43.3 (Step 2) |
| Ultra `1536_cascade` | **64** | **estimate**: not measured, see below |
| `1024` (API only) | 44 | estimate: same token count as `1024_cascade` |

**Where the peak comes from.** From the Step 4 Standard run's own sampler: anonymous RSS
peaked at **22.5 GiB** (~16 GiB of weights plus CPU-side export work) while torch's CUDA
reserve peaked at only **4.7 GiB**. The minimum `MemAvailable` falls during **GLB export**,
which is CPU-heavy: remeshing, xatlas UV unwrapping, texture baking. That is why releasing
the diffusion stage's CUDA cache before export helps the idle state but does **not** move the
peak (26.9 → 26.65 GiB).

**Ultra's estimate.** The part of the peak that isn't model weights grew from ~10.9 GiB
(`512`) to ~27.3 GiB (`1024_cascade`): ×2.5 for 2× resolution, i.e. ≈ resolution^1.32.
Extrapolating to 1.5× resolution gives ×1.71 → ~46.7 GiB, plus ~16 GiB of weights ≈
62.7, rounded up to **64 GiB**. That sits at the high end of plausible, because
`1536_cascade` shares `1024_cascade`'s token cap. If Ultra is ever run, replace it with that
run's recorded `memory_gb.peak_drawdown`.

### What this means for High Quality and Ultra right now

Headroom on this host is now **~80 GiB** with Forge3D idle; it was 83.4 GiB in Step 2,
before the reboot. The largest single holder is Ollama's `qwen3.6:35b-a3b`, 32.9 GB pinned
with `keep_alive: -1`.

| Mode | Needs headroom | Projected minimum at ~80 GiB | Verdict |
| --- | ---: | ---: | --- |
| Standard | 67 GiB | ~53 GiB | **allowed** |
| High Quality | 84 GiB | ~36 GiB | **refused** — ~4 GiB short |
| Ultra | 104 GiB | ~16 GiB | **refused** — ~24 GiB short |

**This is the most important consequence of the corrected gate: High Quality is refused on
this machine by default, where Step 3 allowed it.** The old gate never made HQ safe. It only
checked `MemAvailable ≥ 65` at the start, so a cold HQ start at ~80–83 GiB passed and then
bottomed out at ~36–40 GiB during export, at or below content-factory's 40 GiB threshold.
The user's two HQ runs on 2026-09-10 passed the old gate exactly this way. content-factory's
job queue was unreachable at the time, so nothing was actually harmed.

**Neither HQ nor Ultra was generated in this milestone.** The gate refuses both at the current
headroom, and forcing a run would contradict the policy this milestone introduces. The
between-phase cache release was the only thing that might have lowered HQ's peak, and the
Standard measurement above shows it doesn't move the peak, so an HQ run would not have
changed the verdict.

To enable them — both are user decisions, and Forge3D does neither automatically:

- **Free headroom.** Unloading Ollama's resident model returns ~33 GB:
  `curl http://<tailscale-ip>:11434/api/generate -d '{"model":"qwen3.6:35b-a3b","keep_alive":0}'`.
  With headroom ~113 GiB, HQ projects to ~69 GiB and Ultra to ~49 GiB, and both unlock.
  `ollama-warm-qwen3.service` pins it again at the next boot.
- **Or lower `TRELLIS_PROTECTED_FLOOR_GB`** in `.env`, knowingly accepting that a run can take
  content-factory under its gate.

### Status the UI shows

| Pill | Meaning |
| --- | --- |
| **Ready** (green) | Selected mode can start; TRELLIS not loaded yet (first run adds ~75 s) |
| **Model loaded** (blue) | Selected mode can start; TRELLIS resident, next run skips the load |
| **Generating** (orange, pulsing) | A job is running |
| **Can't start ⟨Mode⟩** (red) | The *selected* mode would breach the floor; the reason and exact numbers appear under the quality selector |

"Insufficient Memory" as a blanket label is gone. Selecting a mode the machine can't safely
run now tells you that mode, why, and by how much.

---

## UI redesign

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ■ Forge3D  │ Model [TRELLIS.2 ▾]                  80.3 GiB headroom ● Ready   │
├───────────────┬─────────────────────────────────────────────┬────────────────┤
│ REFERENCE     │ crown                          [↻][⬡][⤢]│[▥]│ GENERATION     │
│ ┌───────────┐ │ 710,855 verts · Standard · 35 MB            │ Model TRELLIS.2│
│ │   image   │ │                                             │ Quality  512   │
│ └───────────┘ │                                             │ Texture 4096²  │
│ ⓘ Best results│              3D VIEWPORT                    │ Peak ~27 GiB   │
│                │                                             │ Leaves ~53 GiB │
│ QUALITY  geom  │                                             │ ▸ Advanced     │
│ ◉ Standard     │                                             │   Seed / 1K2K4K│
│ ○ High Quality │        ┌──────────────────────────┐        ├────────────────┤
│ ○ Ultra  Exp.  │        │ ◌ Generating · 73s        │        │ ASSET          │
│                │        │ ▰▰▰ Load ▰▰ Gen ▱ Export  │        │ name, seed,    │
│ [ ✦ Generate ] │        └──────────────────────────┘        │ texture, verts │
│                │                                             │ [Download GLB] │
├───────────────┴─────────────────────────────────────────────┴────────────────┤
│ ⌃ Library  6   ▪▪▪▪▪▪                                              Refresh    │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Viewport gets the space.** The library collapses to a 42 px bar with thumbnails (it
  remembers its state), and the inspector can be hidden from the viewport toolbar.
  Measured at 1680×1000: Step 3's viewport was **1384×784**. Step 4's is **1096×909** with
  the inspector open and **1396×909** with it collapsed. The shorter side, which is what
  bounds a centred model, grows from 784 to 909 px (**+16 %**). With the inspector collapsed
  the viewport is also ~17 % larger in area than Step 3; with it open, it is ~8 % smaller in
  area. That is the cost of the right-hand inspector the layout calls for.
- **Quality selector.** Cards show the label, the exact `pipeline_type`, a one-line summary,
  a time hint and a badge. An unavailable mode stays selectable — so you can see *why* — but it
  is hatched, marked "Unavailable now", and Generate is disabled with the reason underneath.
- **Generation state.** A run panel floats over the bottom of the viewport: live elapsed time,
  mode/seed/texture, and four real steps (Load model · Generate · Export GLB · Done).
  "Load model" shows as skipped when TRELLIS is already resident. No invented percentages.
- **Asset selection.** Clicking a card or a mini-thumbnail loads it into the viewer and the
  inspector. Actions live in the inspector: Download GLB, Reuse settings, Rename, Delete (with
  confirmation).
- **Viewer controls.** Auto-rotate, wireframe (for judging geometry density between modes),
  reset view, inspector toggle. The viewer uses a `ResizeObserver`, so it re-fits when panels
  open or close.
- **Empty states** in the viewport, inspector and library each say what to do next. The whole
  viewport is also a drop target for the reference image.
- Same dark Forge3D identity and single orange accent; plain HTML/CSS/ES modules; three.js
  still vendored. No framework, no build step, no new container.

---

## API changes

| Endpoint | Change |
| --- | --- |
| `GET /system` | Adds `state` (`ready` / `model_loaded` / `generating`), `models`, per-mode `modes[]` (availability, peak, projected minimum, reason), `texture`, `seed_max`, and `memory.{forge3d_footprint_gb, headroom_gb, floor_gb, min_headroom_gb}`. Step 2/3 keys kept. |
| `POST /jobs` | Adds `seed` (optional; random when omitted) and `texture_size`. Refusals are `503 insufficient_memory` with `mode`, `available_gb`, `required_gb`, `projected_min_gb`, `floor_gb`, `peak_gb`, `shortfall_gb`, `reason`. |
| `POST /generate` | Same per-mode gate; accepts `texture_size`. Seed default unchanged (42). |
| `GET /assets*` | Summaries add `texture_size`, `mesh_raw`, `generator`, and the run's recorded memory minimum. |

New `metadata.json` fields per generation: `texture_size`, `mode_label`, `generator`,
`mesh_raw` (vertex/face counts straight out of the pipeline, before export), and a richer
`memory_gb` block (`available_min_observed` — now a true 0.5 s-sampled minimum —
`peak_drawdown`, `anon_peak`, `gpu_reserved_peak`, `footprint_after_cleanup`).

---

## Tests performed

Everything ran against the live container on the DGX Spark. The browser tests drive headless
Chromium (Playwright, SwiftShader WebGL) against the real dashboard, and both generations
are real TRELLIS runs.

### Generations run in this milestone — two, both Standard

| Run | Path | Input | Seed | Texture | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | Dashboard (browser) | RGB crown, **no alpha** → background removal and the GB10 torchvision path | 20260910 | 4K | **complete, 176.5 s** (load 75.0 · generate 70.9 · export 29.9) · 757,438 verts · 969,636 faces · 38.75 MB · min `MemAvailable` **53.35 GiB** · drawdown **26.65 GiB** |
| 2 | API `POST /jobs` | same image | 20260910 | 2K | **complete, 163.6 s** (load 75.1 · generate 75.1 · export **12.8**) · raw mesh **identical** to run 1 · 2048² textures · min 53.71 GiB · drawdown 26.79 GiB |

No High Quality or Ultra generation was run: the gate refuses both at the current headroom
(see *Memory policy*).

### Browser end-to-end (`test_step4.py`) — 31 passed; 3 failed, all on one bug

| Group | Checks | Result |
| --- | --- | --- |
| Load and laziness | dashboard loads; no JS errors; TRELLIS unloaded after opening (`state: ready`); model picker shows TRELLIS.2 | pass |
| Layout | viewport 1096×909 (inspector open) / 1396×909 (collapsed); library collapses 150 → 0 px | pass |
| Quality modes | exact ids `512` / `1024_cascade` / `1536_cascade`; Standard default; Ultra badged "Experimental"; every card's availability matches `/system`; selecting HQ/Ultra shows "Can't start …" plus the reason with numbers | pass |
| Seed and texture | 1K/2K/4K with 4K default; random seed rolls; invalid seed flagged; typing a seed turns off per-run randomising | pass |
| Upload | invalid type rejected and Generate stays disabled; RGB image accepted; guidance shown | pass |
| Generation | run panel shows mode, seed and texture; five real states; GLB auto-loads into the viewer | pass |
| Memory UX | after the run the pill reads **Model loaded** at `MemAvailable` 60.6 GiB (< 65) with headroom 79.6 GiB | pass |
| Viewer | auto-rotate, wireframe on/off, orbit, zoom, reset | pass, though the reset check was too weak (see *Bugs the tests caught*) |
| Library / inspector | asset count; inspector seed/texture; download | **fail**: `/assets` 500 from the shadowing bug; re-verified below |

### Re-verification after the `/assets` fix (`post_gen_ui.py`) — 9/9

Library lists all 7 assets · collapsed-bar thumbnails · clicking a card opens it · inspector
shows seed 20260910 and 4096 × 4096 · **Download GLB** (40.6 MB, `glTF` magic) · **Reuse
settings** restores seed, mode and texture · **Rename** · thumbnail switching · no JS errors.

### Delete from the inspector (`delete_ui.py`) — 3/3

Confirmation shown and cancel keeps the asset; confirm removes it and clears the viewer and
inspector; the deleted asset returns 404, and both its `outputs/` and `uploads/` directories
are gone.

### Viewer reset fix (`final_ui.py`)

After a quick drag and **Reset view**, the camera sits exactly at home: 0.0 off immediately
and 0.0 after 1.5 s (before the fix: 0.164 → 0.368).

### API and gate (`api_step4.sh`) — 17/17

| Check | Result |
| --- | --- |
| seed `-1`, `2147483648`, `abc` | `400 invalid_seed` |
| `texture_size=3000` | `400 invalid_texture_size` (valid: 1024, 2048, 4096) |
| unknown mode | `400 invalid_pipeline_type` |
| High Quality via `/jobs` and legacy `/generate` | **`503`**: available 80.0 · required 83.7 · projected 36.3 · floor 40 |
| Ultra via `/jobs` and legacy `/generate` | **`503`**: available 80.0 · required 103.7 · projected 16.3 · floor 40 |
| legacy `/generate` input validation | `400` on bad mode and bad texture |
| traversal: `..%2F…`, double-encoded, uppercase id, `%00` | all 4xx, nothing served |

### Restart, laziness, teardown (`persist.sh`) — 5/5

Assets 7 → 7 after `docker compose restart` · rename survives (`Crown RGB Step4`) · TRELLIS
unloaded again after restart · `docker compose down` leaves **0** Forge3D GPU processes ·
`MemAvailable` 80.2 → 80.5 GiB.

### GB10 compatibility

The minimal `deform_conv2d` repro passes on `sm_121`, and `torchvision/_C.so` is still
`ELF: sm_121`. Run 1 took the full background-removal (BiRefNet) path, which is exactly the
path that failed before `c77ae88`.

### Existing services

Ollama still holds 32,888 MiB, ComfyUI / Portainer / Trivy are up, and
`VISUAL_MIN_AVAILABLE_GB=40` is unchanged.

---

## Bugs the tests caught (and fixed) in this milestone

1. **`/assets` returned 500 for every pre-Step-4 asset.** The new texture-size fallback in
   `assets._summarise` looped with `size = mat.get("baseColorTexture")`, shadowing the GLB
   file size, so `size / 1024**2` raised `TypeError` on a `[4096, 4096]` list. Found because
   the browser test's first library check read 0 assets. Fixed by renaming the loop variable.
   The inspector checks downstream of it (seed, download, reuse, rename) were then re-run
   against the same generated asset without paying for another generation: 9/9.
2. **Reset view drifted.** OrbitControls keeps damped orbit momentum through a reset, so after
   a quick drag the camera sat 0.16 units off home immediately and 0.37 units off 1.5 s later.
   It also carried into the framing of the next model opened. The Step 3 and early Step 4
   reset checks only compared camera *distance*, which rotation preserves, so both missed it.
   `Viewer.reset()` now flushes the momentum with one undamped update before restoring the
   pose, and the test compares the full camera position.

Investigated and **ruled out**: a WebGL context loss dropping the PMREM environment map
(a forced `loseContext`/`restoreContext` left brightness and env-map state unchanged), and the
viewer showing a different model than its title (vertex counts matched the titled asset in
every sequence, including rapid switching in both orders).

---

## Manual test procedure

1. `cd ~/3d-generator && docker compose up -d`, then open `http://<tailscale-ip>:8189`.
2. Check the header shows **Model TRELLIS.2** and **Ready**, and that
   `curl -s http://<ip>:8189/system | jq .model.loaded` is `false`. Opening the page must not
   load TRELLIS.
3. Click each quality card. Standard should keep the pill green. A mode the machine can't
   safely run turns it red ("Can't start …") and shows the numbers under the selector.
4. Drop an **RGB photo with a plain background** (no alpha). This exercises background removal
   and the GB10 torchvision fix; an RGBA image with transparency skips that path entirely.
5. Open **Advanced**: note the seed, try 2K texture if you like, then **Generate**.
   Watch the run panel go Load model → Generate → Export GLB → Done.
6. The GLB opens automatically. Try orbit / zoom / auto-rotate / wireframe / reset.
7. In the inspector: **Download GLB**, **Rename**, **Reuse settings** (the seed returns to the
   one used), **Delete** (asks first).
8. The header should now read **Model loaded**, not a memory error.
9. `docker compose restart` → the renamed asset keeps its name, the library repopulates, and
   the model is unloaded again.
10. `docker compose down` → `MemAvailable` returns to baseline.

---

## Known-good state

Confirmed on 2026-09-11 against the live container:

- The dashboard at `http://<tailscale-ip>:8189` shows **Model TRELLIS.2** and opens with
  TRELLIS unloaded (`state: ready`, 0 MiB GPU).
- Three quality modes with their exact pipeline ids. At the current ~80 GiB headroom,
  Standard is allowed, while High Quality and Ultra show **Unavailable now** with the precise
  reason and shortfall.
- A Standard generation runs end to end from the browser with an RGB image, including
  background removal on GB10: 176.5 s cold, a 38.75 MB GLB with 4096² PBR textures.
- Seeds: random by default, shown before the run, recorded on the asset, and reusable. The
  same seed gives bit-identical raw geometry.
- Texture resolution 1K / 2K / 4K, with 4096² and 2048² both verified in real GLBs.
- After a generation the pill reads **Model loaded**; the CUDA cache is returned
  (0.05 GiB reserved), and the resident footprint is ~19 GiB (model plus ~2.5 GiB of heap).
- Rename, delete (with confirmation), download and reuse all work from the inspector. Renames
  and assets survive a restart.
- `docker compose down` releases all of Forge3D's GPU memory.
- Ollama, ComfyUI and content-factory were not modified.

---

## Known limitations

1. **High Quality and Ultra are refused by default on this host** while Ollama keeps ~33 GB
   resident (see *What this means for High Quality and Ultra*). That is the gate working as
   designed, but it is a real availability limitation compared with Step 3.
2. **Ultra is unmeasured.** Its 64 GiB peak is an extrapolation from two measured modes.
3. **Peaks are per-mode constants, not per-image.** Standard's measured peak moved by only
   0.25 GiB across two different inputs, but an unusually complex object could exceed its
   estimate. The floor equals content-factory's gate, so the only margin is the rounding-up
   of each estimate (roughly 0.3–0.7 GiB).
4. **The gate is still pre-flight.** It projects before a run and re-checks inside the
   generation lock, but does not intervene mid-run if another service allocates heavily.
5. **Seeds reproduce the shape, not a bit-identical mesh** (see *Seed behaviour*).
6. **About 2.5 GiB of heap stays behind** after cleanup (glibc fragmentation), above the model
   weights.
7. **Deliberately small Advanced section**: seed and texture resolution only. Sampler steps,
   guidance, and decimation target are not exposed.
8. **The model picker lists one generator.** There is no generator switching yet.
9. **Job records are still in memory** (unchanged from Step 3). A restart loses an in-flight
   run's status; finished assets are unaffected.
10. `TRELLIS_PROTECTED_FLOOR_GB` and `TRELLIS_MIN_AVAILABLE_GB` are read at container start;
    changing them takes `docker compose up -d`.
