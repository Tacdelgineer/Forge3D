# Setup notes and recovery

Use the [root README](../README.md) for installation. This is the current setup
reference; `step-*` documents preserve earlier experiments and measurements.

## Clone-to-first-run audit

- Supported target: GB10 / Linux ARM64, Ubuntu 24.04 host, NVIDIA driver 580.173.02
  and registered NVIDIA Docker runtime. Other hardware needs testing/rebuilds.
- Host dependencies: Git, Docker Engine + Compose and NVIDIA Container Toolkit.
  Shell helpers also use bash, grep, awk, curl and Python 3. The optional host
  resource helper uses Python 3's standard library, sudo and systemd.
- Python 3.12 (TRELLIS) and 3.10 (Hunyuan), CUDA 12.9.1, compilers, ffmpeg,
  EGL/GL development libraries and Hunyuan's GL/font libraries are installed
  **inside images**, not in a host virtual environment.
- Create writable `data/uploads`, `data/outputs` and `models/hunyuan` before
  launch. Match `.env` UID/GID to their owner; Compose otherwise defaults to
  1000:1000. The app also creates upload/output directories at startup.
- TRELLIS source and Hunyuan source are pinned in their Dockerfiles. Some Git/pip
  dependencies and HF model revisions are not pinned; this is a tested recipe,
  not a fully locked/offline distribution.
- Main weights download on first use into ignored bind mounts. Real-ESRGAN is
  downloaded at Hunyuan image build time. Allow internet access to NVIDIA's
  registry, GitHub, PyTorch's package index, PyPI, Hugging Face and jsDelivr.
- TRELLIS needs an HF read token **and account approvals** for DINOv3 and RMBG.
  Starting the dashboard successfully does not prove gated-model access.
- Ports: 8189 UI/API (loopback by default); 8190 optional worker (host loopback,
  internal `hunyuan:8190`). No separate frontend process, npm install or Blender
  installation is required.

## App-only environment overrides

Only variables explicitly listed in `docker-compose.yml` are forwarded from
`.env`. The images set `HF_HOME`, `TORCH_HOME`, compilation caches, and writable
Hunyuan temporary/background-removal caches. Keep these settings unless moving
the mounts deliberately.

`app/config.py` also reads `FORGE3D_GENERATOR`, `TRELLIS_DATA_DIR`,
`TRELLIS_GLB_TEXTURE_SIZE` and `TRELLIS_GLB_DECIMATION_TARGET`. Compose does not
currently forward these from `.env`; use a local Compose override if needed.
Defaults are `trellis`, `/app/data`, 4096 and 1,000,000 faces respectively.
`HY21_REPO`, `HY2MV_REPO` and `HY2MV_SUBFOLDER` are worker-level overrides, also
not forwarded by the supplied Compose file. The regular UI already exposes
backend, quality and TRELLIS texture-size choices per job.

## Optional Exclusive / Max Quality helper

This helper is **optional and host-specific**, not a generic resource manager.
It can unload Ollama models, unload Hunyuan pipelines, and stop/start only
`pipeline-worker.service`. If you run that service, verify its health endpoint
and the intended interaction before enabling the helper. Do not install it
merely to make an unrelated machine's quality options appear enabled.

The checked-in unit uses loopback endpoints. Configure private host-specific
values locally with `sudo systemctl edit forge3d-resctl.service` after install:

```ini
[Service]
Environment="FORGE3D_RESCTL_OLLAMA_URL=http://127.0.0.1:11434"
Environment="FORGE3D_RESCTL_HUNYUAN_URL=http://127.0.0.1:8190"
Environment="FORGE3D_RESCTL_WORKER_HEALTH_URL=http://127.0.0.1:8788/health"
```

Replace addresses locally if your services listen on another trusted interface.
These are **host daemon** variables; putting them in the app's `.env` has no
effect. Never commit actual private endpoints.

The supplied unit and tmpfiles rule assume UID/GID **1000**. For other IDs,
adjust `FORGE3D_RESCTL_UID` / `FORGE3D_RESCTL_GID` in the service and the directory
group in `host/forge3d-resctl.tmpfiles.conf` before installation. The socket
must be accessible to the container's UID/GID. The daemon checks peer identity;
an inaccessible socket leaves Exclusive unavailable rather than bypassing it.

```bash
sudo host/install.sh
# After applying any systemd drop-in:
sudo systemctl restart forge3d-resctl.service
docker compose up -d trellis
```

The installer creates a persistent socket-directory inode via tmpfiles, installs
`/usr/local/sbin/forge3d-resctl`, and enables the systemd daemon. It runs as root
to control the fixed worker service; the app gains only the Unix socket mount.
Without the helper, Docker may create the empty bind directory and the app keeps
Exclusive disabled. The already working DGX service configuration is not changed
by editing these repository defaults; preserve its local endpoints in a drop-in
before reinstalling the helper.

The lease defaults to 1800 seconds and is clamped to 60–3600 seconds. Recovery:

```bash
sudo forge3d-resctl recover
sudo journalctl -u forge3d-resctl.service -n 50 --no-pager
# Uninstall restores paused workloads first; saved state is retained.
sudo host/install.sh --uninstall
```

Pinned Ollama models are rewarmed; ordinary keep-alive models and Hunyuan pipelines
are left unloaded. Hunyuan reloads on its next generation. A worker can retain
allocator memory even after unload; restart it only when idle:

```bash
docker compose --profile hunyuan restart hunyuan
```

## Existing checks, without a full generation

Run against an already started app. Pass tests via stdin: only `verify_stack.py`
is copied into the TRELLIS image; tests need no temporary in-container copy.

```bash
docker compose exec -T trellis python - < scripts/test_step5.py
# Host integration expectations: helper installed + releasable workloads present.
docker compose exec -T trellis python - < scripts/test_step6.py
# Optional diagnostics for a freshly built CUDA image; small kernels/imports only.
docker compose exec -T trellis python /app/scripts/verify_stack.py
docker compose --profile hunyuan exec -T hunyuan python - < scripts/verify_hunyuan.py
```

Browser test scripts need a separately installed Playwright environment and are
development tools, not installation dependencies. A release documentation check
does not need browser automation or another expensive asset generation.
