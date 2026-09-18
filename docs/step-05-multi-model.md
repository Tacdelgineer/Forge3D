# Step 05 — Multi-model generation (Hunyuan3D)

> Historical milestone record. Use [the current README](../README.md) and [SETUP.md](SETUP.md) for installation and current behavior.

Forge3D can now generate with three backends instead of one:

| id | name | inputs | backend |
|---|---|---|---|
| `trellis` | TRELLIS.2 | one reference image | in-process (unchanged) |
| `hunyuan21` | Hunyuan3D 2.1 | one reference image | `hunyuan` worker container |
| `hunyuan2mv` | Hunyuan3D Multi-View | 1–4 named views, front required | `hunyuan` worker container |

TRELLIS behaves exactly as it did after Step 4. Its container, its CUDA stack and
its dependency pins were not touched; the only changes inside it are the adapter,
the memory gate and the UI.

---

## Architecture

```
browser ── /jobs ──▶ FastAPI (trellis container)
                      ├── jobs.py      single-worker queue (one GPU → one run)
                      └── engine.py    generate(generator, inputs, settings)
                            ├── trellis      → TRELLIS.2 pipeline, in this process
                            └── hunyuan*     → hunyuan_client → HTTP → worker container
                                                                   ├── hy3dshape (2.1)
                                                                   ├── hy3dgen   (2mv)
                                                                   └── hy3dpaint (texture)
```

No Kubernetes, Redis, database, message broker or plugin framework. The
"registry" is a tuple of three dicts in `app/config.py`, and the client
(`app/hunyuan_client.py`) is stdlib-only so the known-good TRELLIS image gains
no new dependencies.

### Why Hunyuan is isolated from TRELLIS

They cannot share a Python environment:

| | TRELLIS.2 | Hunyuan3D |
|---|---|---|
| `transformers` | **4.57.1** (required; 5.x broke `DINOv3ViTModel`) | **4.46.0** (pinned) |
| `diffusers` | not pinned by us | **0.30.0** (pinned) |
| Python | 3.12 | 3.10 (what Hunyuan's pins target) |

Installing Hunyuan into the TRELLIS image would have meant downgrading
`transformers` under a working 4 B model, on a CUDA stack that took a full
regression hunt to get right in Step 3/4. The worker is a separate image on
Ubuntu 22.04 + Python 3.10, opt-in via a compose profile:

```bash
docker compose --profile hunyuan up -d hunyuan
```

With it stopped, TRELLIS runs normally and the Hunyuan generators report
`backend_unavailable` with the command to start them — not a memory error.

### Why both Hunyuan generators share one worker

Their real dependency sets are compatible: 2mv is the same `hy3dgen` shape
architecture as 2.0, and 2.1's paint pipeline textures an arbitrary mesh. One
worker holds one shape pipeline at a time (loading one unloads the other) and a
single paint pipeline.

The one genuine conflict is that **2.0's `texgen` and 2.1's `hy3dpaint` both
publish a distribution named `custom_rasterizer`**, so installing both would
have one silently overwrite the other. 2.0's `texgen` is therefore deliberately
not installed (`rm -rf hy3dgen/texgen`), and `verify_hunyuan.py` asserts it stays
absent.

---

## ARM64 / GB10 portability fixes

Every one of these was found by a failing build or a failing run on this host.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `compile_mesh_painter.sh: python3-config: not found` | script calls the **unversioned** name; `python3.10-dev` ships only `python3.10-config` | symlink, asserted against the venv's own `EXT_SUFFIX` |
| 2 | `bpy` has no linux-aarch64 wheel at all | upstream converts OBJ→GLB with Blender | `patches/patch_no_bpy.py` rewrites `convert_obj_to_glb` to trimesh; verified by a real OBJ→GLB round trip |
| 3 | `ImportError: libharfbuzz.so.0` on **every** Hunyuan import | pymeshlab bundles Qt and dlopens text-shaping libs | `libharfbuzz0b libfontconfig1 libfreetype6` |
| 4 | `numba: cannot cache function '_make_tree'`, `pooch: cannot create /.u2net` | container runs as the invoking user, whose `HOME=/` is not writable | `HOME=/tmp`, `NUMBA_CACHE_DIR`, `MPLCONFIGDIR`, `U2NET_HOME=/models/u2net` |
| 5 | `ModuleNotFoundError: pkg_resources` (textured runs only) | setuptools 81 removed it; basicsr/realesrgan still import it | `setuptools<81` |
| 6 | `ModuleNotFoundError: open3d` ~50 s into a textured run | **not** imported by Hunyuan at all — `trimesh.simplify_quadric_decimation()` delegates to open3d | `open3d==0.18.0`, the newest release with a linux-aarch64 wheel |
| 7 | CUDA extensions must contain real sm_121 code | GB10 is `sm_121`; PTX-only builds fail at launch | `TORCH_CUDA_ARCH_LIST="12.1+PTX"` + a build-time `cuobjdump --list-elf` assertion that fails the build without an `sm_121` cubin |

Fix 6 is worth remembering: grepping both Hunyuan repos for `open3d` returns
nothing, because the import lives inside trimesh. A source grep "proved" it was
unused; it is not.

**Build memory safety.** `MAX_JOBS=4`, `NVCC_THREADS=1`, and every build was run
with a 10 s MemAvailable sampler plus a watchdog. Lowest MemAvailable across all
Hunyuan image builds: **55.1 GiB** — no repeat of the 7 GiB flash-attn incident.

---

## Model storage

Weights live on the `./models/hunyuan` bind mount (`HF_HOME=/models/hf`), are
fetched on first use, and are never baked into the image or committed.

| repo | files taken | size |
|---|---|---|
| `tencent/Hunyuan3D-2.1` | `hunyuan3d-dit-v2-1/model.fp16.ckpt`, `hunyuan3d-vae-v2-1/*`, `hunyuan3d-paintpbr-v2-1/*` | 13.9 GiB |
| `tencent/Hunyuan3D-2mv` | `hunyuan3d-dit-v2-mv/model.fp16.safetensors` + config | 4.6 GiB |
| `tencent/Hunyuan3D-2` | `hunyuan3d-vae-v2-0/*` (2mv's VAE is **not** in the 2mv repo) | 0.8 GiB |
| `facebook/dinov2-giant` | pulled at runtime by 2.1's conditioner | ~4.6 GiB |

`.ckpt` vs `.safetensors` is not arbitrary: `hy3dshape` (2.1) defaults to
`use_safetensors=False` and loads `model.fp16.ckpt`, while `hy3dgen` (2mv)
defaults to `use_safetensors=True` and loads `model.fp16.safetensors`. Taking
only what each pipeline actually opens avoided a redundant 4.6 GiB download.
Both VAE variants are kept (0.4 GiB each) because `ShapeVAE.from_pretrained`
does not inherit the pipeline's `use_safetensors` flag.

---

## UI behaviour

- **Model** in the top bar is now functional, listing the three generators with
  a one-line blurb. A generator whose worker is down is still selectable and
  explains itself rather than silently disappearing.
- **TRELLIS / 2.1** keep the single drag-and-drop uploader.
- **Multi-View** replaces it with four labelled slots — Front (`REQUIRED`),
  Back, Left, Right (`optional`) — each its own drop target with preview and
  clear button. Generate stays disabled until Front is present.
- **Quality** cards are generator-specific: `512 / 1024_cascade / 1536_cascade`
  for TRELLIS, `shape / shape + PBR texture` for Hunyuan. The vocabularies are
  disjoint and the API rejects a mode belonging to another generator.
- **Advanced** stays small: seed (randomised by default, reusable) plus texture
  resolution, which is hidden for Hunyuan because it is a TRELLIS-only setting.
  No fake disabled controls.
- The restricted upstream licence is shown in the inspector, with the territory
  exclusions spelled out, not merely linked.
- The library tags each asset with its generator (`TRELLIS`, `HY 2.1`, `HY MV`).
  Pre-Step-5 assets still load; their metadata has no generator (or an early
  `trellis2`), and `assets.py` normalises anything unknown to `trellis`.

---

## Memory

### The gate

Two rules, both must hold, evaluated per `(generator, mode)`:

1. `headroom ≥ TRELLIS_MIN_AVAILABLE_GB` (65)
2. `headroom − peak(generator, mode) ≥ TRELLIS_PROTECTED_FLOOR_GB` (40)

Headroom is `MemAvailable` plus memory the run could actually **reuse**.

### A real floor breach, and the bug behind it

The Step 5 gate initially credited Forge3D's own resident model as headroom for
*every* generator. That is right for a TRELLIS run — a resident TRELLIS model is
reused — but wrong for a Hunyuan run: TRELLIS keeps holding its ~20 GiB while
the worker allocates on top. A multi-view run was cleared on a projection of
62 GiB and took **MemAvailable to 36.0 GiB, under the 40 GiB floor**.

Fixed in `memory.py`: only the backend that will run the job gets credited
(`footprint_credited_gb` reports which). `scripts/test_step5.py` pins it.

A second, larger breach followed: a textured 2.1 run drew **54 GiB against a
36 GiB estimate**, taking MemAvailable to **24.8 GiB**. Nothing died —
content-factory, ComfyUI and Ollama all survived — but the floor was breached
because the *estimate* was wrong, not the arithmetic. The textured peaks are now
measured (56 / 58 GiB), which makes those modes unavailable on this host while
Ollama holds ~33 GiB. That is the correct outcome and is shown in the UI with
the reason; the alternative is starving content-factory.

**The gate is pre-flight only.** It cannot stop a run that exceeds its own
estimate mid-flight, so conservative, measured peaks are the whole defence.

### Measurements

All on the same host, sampled every 0.5 s for the whole run.

| generator / mode | time | MemAvailable min | drop | GPU peak | anon peak | configured peak |
|---|---|---|---|---|---|---|
| `trellis` / 512 | 182.5 s | 52.5 GiB | 26.7 GiB | 4.7 GiB | 22.2 GiB | 27 (measured) |
| `hunyuan21` / shape | 39.8 s | 49.6 GiB | 1.7 GiB¹ | 11.5 GiB | 18.7 GiB | 22 (estimate) |
| `hunyuan2mv` / shape | 78.8 s | 36.1 GiB | 15.3 GiB | 13.2 GiB | 18.7 GiB | 24 (estimate) |
| `hunyuan21` / shape_texture (failed, 2026-09-11) | 156 s (failed) | **24.8 GiB** | **54.1 GiB** | — | — | 56 (measured) |
| `hunyuan21` / shape_texture (**validated, 2026-09-14**) | 134.6 s | **57.85 GiB** | 56.85 GiB | 45.4 GiB | 13.31 GiB | 56 (measured) |

¹ warm: the shape model was already resident from a prior attempt, so this is a
marginal figure, not a cold-start peak. The configured 22 GiB stays conservative.

TRELLIS's 26.7 GiB against a configured 27 GiB confirms the Step 4 calibration
is still accurate — the regression run behaved exactly as before.

### The validated textured run (2026-09-14)

Limitation 1 below is now closed. With Ollama's `qwen3.6:35b-a3b` temporarily
unloaded (`ollama stop`, ~33.6 GiB returned) **and** the TRELLIS container
restarted so its ~19 GiB resident model was not squatting on headroom,
`hunyuan21` / `shape_texture` cleared the gate on its own terms and ran to
completion. Nothing was overridden: the 40 GiB floor and the 56 GiB peak
estimate were left exactly as they are.

Freeing Ollama **alone is not enough**. It took MemAvailable to 86.56 GiB
against the 86.8 GiB the gate requires — refused by 0.24 GiB. TRELLIS's
resident model had to go too, because a Hunyuan run cannot reuse it and the
gate correctly refuses to credit it (see the cross-backend bug above).

| | |
|---|---|
| input | single reference image, 1086×1448 RGB, seed 42 |
| gate before | MemAvailable 105.45 GiB · headroom 114.7 · projected min 58.7 |
| total | **134.6 s** (shape 37.5 s, texture 94.1 s) |
| MemAvailable min | **57.85 GiB** — 17.85 GiB clear of the 40 GiB floor |
| peak drawdown | **56.85 GiB** |
| GPU reserved peak | 45.4 GiB (independent sampler: 45.68 GiB) |
| worker anon peak | 13.31 GiB (independent VmRSS peak: 13.84 GiB) |
| mesh (raw → GLB) | 151,544 v / 303,136 f → **25,854 v / 40,000 f** |
| GLB | **1,177,332 B (1.12 MB)** |

Sampled independently at 0.5 s alongside Forge3D's own instrumentation; the two
agree to within 0.03 GiB on the minimum.

**The 56 GiB estimate is 0.85 GiB optimistic.** The run actually drew
56.85 GiB. The floor held only because this run started with 17.85 GiB of
margin — at the gate's own minimum it would have landed at 39.15 GiB, just
under the floor. The gate is pre-flight only, so this matters: the configured
peak is left unchanged here deliberately (this was a measurement-only
exercise), but it is the first case where a *measured* peak has been
exceeded, and 56 → 58 is the obvious correction to consider.

**What "PBR texture" actually exports.** The GLB carries one material with a
single 2048×2048 baseColor JPEG (178,856 B, 15% of the file) — a real baked UV
atlas. It does **not** carry a metallic-roughness map or a normal map, and the
primitive has only `POSITION` and `TEXCOORD_0` — **no `NORMAL`**. Because the
material omits `metallicFactor`, glTF's default of **1.0** applies, so viewers
shade it as fully metallic and it reads far darker than the reference. It loads
cleanly in Forge3D's own three.js viewer with no console errors, but "PBR" here
means baked albedo, not a full PBR material set. Worth fixing separately;
`texture_size=4096` was also requested and 2048 was produced.

---

## Quality comparison

Same reference image (`crown_rgb.png`, 774×774 RGB) for TRELLIS and 2.1. The
multi-view run used four **genuinely separate** rendered views of a real mesh
(`scripts/render_views.py`, nvdiffrast, azimuths 0/90/180/270) — not one image
stitched into a grid.

| | TRELLIS.2 512 | Hunyuan3D 2.1 shape | Hunyuan3D 2mv shape |
|---|---|---|---|
| vertices | 740,306 | 130,042 | 306,134 |
| triangles | 931,864 | 260,080 | 613,450 |
| GLB | 36.05 MB | 4.47 MB | 10.52 MB |
| PBR texture | **yes**, 4096² base colour + metallic-roughness | no | no |
| time | 182.5 s | 39.8 s | 78.8 s |

TRELLIS.2 remains the quality choice on this host: it is the only one of the
three that produced a textured, production-ready asset, with ~5.7× the vertex
count of 2.1. The Hunyuan backends are markedly faster (2.1 is ~4.6× quicker)
and useful when geometry alone is wanted, and 2mv's extra views translate into
real density — 2.4× the vertices of single-image 2.1 on the same subject.

---

## Licences

**Forge3D redistributes no weights.** Everything is downloaded from Hugging Face
at runtime into a bind mount.

| generator | upstream | licence |
|---|---|---|
| `trellis` | [microsoft/TRELLIS.2](https://github.com/microsoft/TRELLIS.2) | MIT (code); weights per Microsoft's model card |
| `hunyuan21` | [Tencent-Hunyuan/Hunyuan3D-2.1](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1) | **Tencent Hunyuan 3D 2.1 Community License** |
| `hunyuan2mv` | [Tencent-Hunyuan/Hunyuan3D-2](https://github.com/Tencent-Hunyuan/Hunyuan3D-2) | **Tencent Hunyuan 3D 2.0 Community License** |

The Tencent licences are **not** open-source licences. They:

- exclude the **European Union, the United Kingdom and South Korea**;
- require a separate licence above **1 million monthly active users**;
- impose an Acceptable Use Policy.

These restrictions are surfaced in the UI next to the model, and in
`.env.example`, rather than being flattened into "MIT-ish".

---

## Tests performed

| suite | what it covers | result |
|---|---|---|
| `scripts/test_step5.py` (in the trellis container) | registry, per-generator gate, cross-backend credit regression, multi-view contract, legacy assets, path traversal | **20/20** |
| `scripts/verify_hunyuan.py` (in the worker) | sm_121 cubin, CUDA execution, compiled extensions, every Hunyuan import, multi-view API, bpy-free OBJ→GLB, trimesh→open3d | **17/17** |
| `scripts/test_ui_step5.py` (Chromium) | model switching, view slots, Front required, generator-specific quality, hidden TRELLIS-only settings, licence text, library tags, viewer | **15/15** |
| real generations | TRELLIS 512 regression, 2.1 shape, 2mv shape from 4 views | 3 passed |

Bugs the tests caught, in this milestone alone: the cross-backend memory credit,
a `.tag.gen` class collision that rendered the library's generator tag as a
miniature Generate button (104×42 instead of 46×17), cards stretching to 430 px
because a flex item's default `min-width:auto` overrode its basis, and an
assertion of mine that passed only because the section it inspected was
collapsed.

---

## Known limitations

1. **Textured Hunyuan modes need both Ollama and TRELLIS out of the way.**
   `hunyuan21` / `shape_texture` is now verified end to end (2026-09-14, above),
   but only after unloading Ollama *and* restarting the TRELLIS container.
   Freeing Ollama alone leaves it 0.24 GiB short. In the normal steady state
   (Ollama resident) they remain unavailable with the reason shown, which is
   correct. Forge3D still never unloads Ollama automatically and must not learn
   to — that stays a human decision. `hunyuan2mv` / `shape_texture` (58 GiB,
   estimated) is still unverified.
2. **The textured path is fixed and now verified.** Three real defects were
   found and fixed along it (`pkg_resources`, `open3d`, and an integration bug
   where I passed a `.glb` path to `output_mesh_path`, which upstream requires
   to be `.obj` because it derives the GLB name by string replacement). It was
   previously unverified because confirming it would have breached the floor;
   the 2026-09-14 run confirmed it without breaching anything. Its exported
   material is albedo-only — see above.
3. **2mv's validation inputs are renders**, not photographs — four camera
   azimuths of a real mesh. The API path is identical for real photographs.
4. **The worker's `/status` is unresponsive during a generation**, so the UI
   shows the worker's last known footprint until the run ends.
5. `hunyuan21` / `hunyuan2mv` shape peaks are still conservative estimates; only
   the TRELLIS modes and 2.1's textured mode are measured.
6. The gate remains **pre-flight only** (see above).

---

## Known-good state

```
trellis image   3d-generator-trellis:latest    (unchanged CUDA stack, Step 4 pins)
hunyuan image   3d-generator-hunyuan:latest    21.3 GB, Python 3.10, cu129, sm_121 verified
weights         ./models/hunyuan   ~28 GiB     bind mount, never committed
TRELLIS 512     182.5 s, 740k verts, 36 MB textured GLB, 26.7 GiB drawdown
HY 2.1 sh+tex   134.6 s, 25.8k verts, 1.12 MB GLB, 56.85 GiB drawdown (needs Ollama + TRELLIS unloaded)
```

Start everything:

```bash
docker compose up -d trellis                      # TRELLIS only
docker compose --profile hunyuan up -d hunyuan    # add the Hunyuan worker
```

Verify:

```bash
docker compose exec -T trellis python - < scripts/test_step5.py
docker compose --profile hunyuan exec -T hunyuan python - < scripts/verify_hunyuan.py
```
