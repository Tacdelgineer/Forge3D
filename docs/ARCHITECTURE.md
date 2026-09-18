# Current architecture

```text
Browser dashboard + Three.js GLB viewer
  → Forge3D FastAPI service (trellis, port 8189)
    → TRELLIS.2 in the service process
    OR → Hunyuan FastAPI worker (hunyuan, port 8190)
      → selected GPU pipeline → GLB + metadata → local asset library
```

`app/main.py` serves static UI files and the HTTP API. The dashboard submits
`POST /jobs` and polls job/system state; synchronous `POST /generate` remains
available. `app/jobs.py` tracks jobs in memory, while `app/engine.py` serializes
whole runs, saves references, validates exports with trimesh, and records output
metadata. `app/assets.py` manages completed assets on disk.

TRELLIS and Hunyuan have separate images/environments because their transformers
versions conflict. The optional Hunyuan worker serves both 2.1 single-image and
2mv shape inference, swapping its shape pipeline when necessary. Both Hunyuan
texture modes use **2.1's paint pipeline**; 2.0's texgen is deliberately omitted.
Models load lazily and can remain resident between jobs. Runtime/CUDA cleanup
releases temporary allocations without rebuilding the pipeline.

Host `data/` and `models/` are persistent bind mounts. GLBs and metadata live under
`data/outputs/<id>/`; references under `data/uploads/<id>/`. Job records reset on
restart, but completed assets and model caches survive. Three.js modules are
vendored into the TRELLIS image at build time; the app requires no Node build.

## Memory and other workloads

GB10 has unified CPU/GPU memory. `app/memory.py` reads `/proc/meminfo` rather than
relying on unsupported framebuffer totals. Required job headroom is the larger
of **65 GiB** and **configured mode peak + 40 GiB protected floor**. Only a
backend's reusable resident footprint is credited to its job; Hunyuan cannot
reuse TRELLIS's resident weights. Peaks are estimates or recorded measurements,
and the gate runs before inference rather than enforcing a continuous cap.
Recorded Standard and Hunyuan-texture peaks have slightly exceeded their stored
estimates; simultaneous unrelated allocations can also invalidate projections.

Normal jobs leave other workloads alone. TRELLIS's optional Exclusive / Max
Quality mode uses `app/resctl.py` to acquire a lease from the host helper over a
UID-checked Unix socket. The helper snapshots workloads, requests Ollama model
unloads, unloads Hunyuan pipelines, and stops the **fixed**
`pipeline-worker.service` if active. High-memory jobs may otherwise lack enough
unified memory or compete with that service's visual jobs.

Afterward it restores the prior service state and rewarms only previously pinned
Ollama models. Hunyuan pipelines remain unloaded until their next use. The helper
persists state and restores on lease expiry, daemon restart or manual recovery.
It exposes no arbitrary command interface and the containers have no Docker
socket. This integration is specific to the tested host; see [SETUP.md](SETUP.md).
