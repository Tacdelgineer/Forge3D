# Step 6 — Exclusive / Max Quality

> Historical milestone record. Use [the current README](../README.md) and [SETUP.md](SETUP.md) for installation and current behavior.

High Quality (`1024_cascade`) and Ultra (`1536_cascade`) have never been able to
run on this DGX. Not because TRELLIS cannot do them, but because Ollama and the
Hunyuan worker between them hold ~53 GiB of the 128 GB unified pool, and the
memory gate correctly refuses anything that would take MemAvailable below the
40 GiB floor that content-factory's visual jobs need.

Step 6 adds one checkbox that lets the user say: *for this one run, pause those
workloads, then put them back.*

    [ ] Exclusive / Max Quality
        Temporarily pauses selected AI workloads to free memory, then
        restores them after generation.

With the box clear, nothing about Forge3D changes — same gate, same floor, same
numbers, nothing else on the host is touched.

---

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Normal mode behaves exactly as before | ✅ 20/20 Step 5 checks pass; a normal Standard run paused nothing |
| 2 | No global reduction of `TRELLIS_PROTECTED_FLOOR_GB` | ✅ still 40.0, asserted by a test |
| 3 | No Docker socket in the Forge3D web container | ✅ one Unix socket, nothing else |
| 4 | Exclusive mode unloads Ollama's ~33 GiB and proves it returns | ✅ measured +33.12 GiB into MemAvailable |
| 5 | HQ becomes available when measured memory allows | ✅ ran end to end |
| 6 | Ultra becomes available only when measured memory allows | ✅ see the Ultra section |
| 7 | External workloads restored after success or failure | ✅ both paths measured |
| 8 | Crash recovery exists | ✅ three independent mechanisms, all tested live |
| 9 | `/system` and the UI report real state | ✅ projections are labelled as projections |
| 10 | Existing tests pass | ✅ 20/20 Step 5, 15/15 Step 5 UI |
| 11 | New lifecycle/memory tests pass | ✅ 19/19 Step 6, 10/10 Step 6 UI |
| 12 | Real DGX generation test passes | ✅ see Measurements |

---

## The simplest thing that works

Before designing anything, it is worth being precise about what actually needs
privilege, because it is much less than it first appears.

| Release | Needs root? | How |
|---|---|---|
| Ollama's resident model | **no** | `POST /api/generate {"keep_alive": 0}` — the host helper reaches the configured Ollama endpoint |
| Hunyuan worker's pipelines | **no** | `POST /unload` — the worker has had this endpoint since Step 5 |
| `pipeline-worker.service` | **yes** | `systemctl stop` |

Two of the three need nothing at all. The whole reason a privileged helper
exists is the third — and that one frees no memory. `pipeline-worker` holds
16 MB. It is paused because it polls a remote queue every 5 seconds and will
happily claim a visual job in the middle of an Ultra run, and its own gate
(`VISUAL_MIN_AVAILABLE_GB=40`) would not stop it: an exclusive Ultra run bottoms
out above 40 GiB, so from `pipeline-worker`'s point of view there is plenty of
room, and ComfyUI would then load models on top of TRELLIS.

So: **HQ would be safe with no helper at all. The helper exists to make Ultra
safe.** It is scoped accordingly — one systemd unit, hardcoded.

### What is deliberately NOT done

**ComfyUI is not stopped.** It was measured cold: 170 MiB of GPU and 0.83 GiB
RssAnon, about 1.0 GiB in total. Stopping `comfyui.service` would disrupt
content-factory for a rounding error. With `pipeline-worker` paused, nothing
dispatches to ComfyUI anyway, so it simply idles.

**No process is ever killed.** Ollama and the Hunyuan worker are asked over
their own APIs. `pipeline-worker` is stopped through systemd so it shuts down
cleanly, and only when it reports no job in flight.

**The Hunyuan container is not stopped.** Only its models are unloaded. This
costs ~7.3 GiB (below), but stopping the container would mean giving the helper
Docker control for one more workload, and that trade was not worth it.

---

## Architecture

```
  browser ──► Forge3D container (uid 1000, no root, no docker.sock)
                   │
                   │  app/resctl.py — AF_UNIX, four verbs, no shell
                   ▼
            /run/forge3d-resctl/resctl.sock   (root:1000, mode 0660)
                   │
                   ▼
            forge3d-resctl (root, systemd)
                   │
       ┌───────────┼────────────────────┐
       ▼           ▼                    ▼
  Ollama API   Hunyuan /unload   systemctl stop/start
  keep_alive=0                   pipeline-worker.service
```

The helper, not Forge3D, owns the lifecycle. It takes the snapshot, it
releases, and it restores — on a deadline, whether or not Forge3D is still
alive. That is the whole point: a client that has just died cannot run its own
`finally` block.

### Files

| File | Role |
|---|---|
| `host/forge3d-resctl` | the daemon (stdlib only, ~600 lines) |
| `host/forge3d-resctl.service` | systemd unit, hardened |
| `host/forge3d-resctl.tmpfiles.conf` | creates the socket directory |
| `host/install.sh` | install / uninstall |
| `app/resctl.py` | the only thing in Forge3D that talks to it |

### API

    GET  /state      what is running, what is releasable, lease status
    POST /acquire    {lease_seconds}  → snapshot, release, return a token
    POST /renew      {token, lease_seconds}
    POST /release    {token}  → restore exactly the snapshot

There is no fifth verb and no free-form parameter.

---

## Exact workloads that may be paused

This is the complete list. It is a module constant in the daemon, not
configuration.

1. **Ollama resident models** — whatever `/api/ps` reports at acquire time
   (currently `qwen3.6:35b-a3b`, 32.19 GiB, pinned `keep_alive=-1`).
   Released with `keep_alive=0`; re-warmed with `keep_alive=-1` afterwards, by
   name, **only if it was pinned before**. A model on an ordinary keep-alive
   would have expired by itself during the run, so re-warming it would leave the
   host holding more than it did before.
2. **`3d-generator-hunyuan` model pipelines** — via the worker's own
   `POST /unload`. Not re-warmed: the worker loads a pipeline on its next
   generation, so eagerly reloading costs ~13 GiB and a minute for something
   nothing is waiting on. The restore report says so explicitly rather than
   quietly implying it was put back.
3. **`pipeline-worker.service`** — `systemctl stop`, restarted afterwards only
   if it was active before.

**Never touched:** `comfyui.service`, `content-factory-comfyui`, `tailscaled`,
`sshd`, `dockerd`, NVIDIA services, `spark-status`, `portainer`, `ai-redis`,
`ai-supervisor`, or any other process on the box.

---

## Security decisions

The dashboard is reachable over Tailscale, so the container is treated as
untrusted. The threat model is "someone who can make arbitrary HTTP requests to
Forge3D should not thereby get anything on the host."

| Decision | Why |
|---|---|
| **Unix socket only** | No TCP listener exists, so nothing here is reachable over Tailscale or the LAN — the attack surface is limited to processes already on the box. |
| **Socket `root:1000`, mode 0660** | Only root and uid 1000 (the container's user) can connect. Verified: `sudo -u nobody curl` fails to connect at all. |
| **`SO_PEERCRED` check** | Defence in depth if the permissions are ever wrong: the peer's uid must be 0 or 1000. |
| **Four fixed verbs** | The only caller-supplied values are `lease_seconds` (int, clamped to 60–3600) and an opaque token. There is no command, path, unit name or model name parameter. |
| **Unit name is a constant** | `MANAGED_UNIT = "pipeline-worker.service"`. A request can never name a unit. |
| **Model names come from Ollama** | The daemon reads `/api/ps` itself. A caller cannot ask it to unload or load an arbitrary model. |
| **`subprocess` with arg lists, never `shell=True`** | No request value can reach a shell. A test asserts the client module contains no `subprocess`, `os.system`, `shell=True` or `popen` at all. |
| **No Docker socket** | Not mounted, not used. The compose file says so at the mount point. |
| **Systemd hardening** | `ProtectSystem=strict`, `ProtectHome=read-only`, `RestrictAddressFamilies=AF_UNIX AF_INET`, `SystemCallFilter=@system-service` minus `@privileged`/`@resources` (plus `@chown`, needed to set the socket's group), `MemoryDenyWriteExecute`, `LockPersonality`, `RestrictNamespaces`. |
| **Helper is optional** | If it is not installed the checkbox is disabled with the reason, and nothing else changes. An absent helper can only make the gate stricter, never looser. |

**Residual risk, stated plainly.** Anyone who can reach the dashboard can cause
`pipeline-worker` to stop and Ollama's model to unload for up to the lease
length, which is a denial of service against content-factory. That is inherent
in the feature — it is exactly what the button does — and it is bounded: the
lease expires, everything comes back automatically, and every acquire is logged
with the peer uid. It is not a privilege escalation: nothing the caller sends
influences *what* is paused.

---

## Crash recovery

Three mechanisms, because they fail in different ways. All three were tested
against the live host.

**1. Lease expiry (client dies or hangs).** `/acquire` sets a deadline;
Forge3D renews every 60 s while it generates. A watchdog thread in the daemon
restores when the deadline passes. This covers the container being killed, the
app hanging, the network dropping — anything where the client stops asking.

**2. Daemon restart (helper or host crashes).** The snapshot is persisted to
`/var/lib/forge3d-resctl/state.json` **before** anything is released. On
startup, a state file that says `exclusive` means the previous instance died
mid-run, and the daemon restores immediately. `Restart=always` makes this
automatic.

**3. Manual.** `sudo forge3d-resctl recover` restores from the persisted state
at any time. `host/install.sh --uninstall` runs it first, so removing the helper
can never strand a paused service.

Why a lease rather than "restore when the client's socket closes": a *hung*
container never closes its socket. The lease covers hangs and crashes alike, and
the persisted state covers the daemon itself dying, which a connection watch
cannot.

---

## Manual recovery command

    sudo forge3d-resctl recover     # restore whatever is still paused
    sudo forge3d-resctl state       # what the daemon thinks is happening
    sudo systemctl status forge3d-resctl
    sudo journalctl -u forge3d-resctl -n 50

A restore failure is never silent: it is logged at ERROR, kept in
`/var/lib/forge3d-resctl/state.json`, surfaced in `/system` under
`exclusive.last_restore`, attached to the job record as `exclusive_report`, and
shown in the dashboard as a red banner naming the recovery command.

---

## Installation

    sudo host/install.sh
    docker compose up -d trellis     # picks up the socket bind mount

Uninstall:

    sudo host/install.sh --uninstall

---

## Measurements

All figures from `/proc/meminfo` on the live DGX, 2026-09-15/16. `nvidia-smi`
reports `Not Supported` for device memory on GB10, so per-process rows are used
to attribute it and `/proc/meminfo` is the authority on what is actually free.

### Idle baseline

| | GiB |
|---|---|
| MemTotal | 121.69 |
| MemAvailable (idle, normal services) | 60.40 |
| Ollama `qwen3.6:35b-a3b` (`size_vram`, `keep_alive=-1`) | 32.19 |
| `3d-generator-hunyuan` (20.03 GPU + 0.78 anon) | 20.81 |
| `content-factory-comfyui` (0.17 GPU + 0.83 anon, **cold**) | ~1.00 |
| `pipeline-worker.service` | 0.02 |
| Forge3D idle (model not resident) | 0.14 |

### Release, measured without generating

| Step | MemAvailable | Δ |
|---|---|---|
| before | 60.40 | |
| after Ollama `keep_alive=0` | 93.52 | **+33.12** |
| after Hunyuan `POST /unload` | 106.67 | **+13.13** |
| **total** | | **+46.25** |

Ollama's release completes in **2–4 s**; the API call returns in 13 ms with
`"done_reason":"unload"`, so the helper polls MemAvailable rather than trusting
the response. The `ollama runner` process exits entirely, which is why the gain
(33.12) slightly exceeds the model's own 32.19. Re-warming takes **9.9 s** and
the model comes back pinned (`expires_at` 2318, i.e. `keep_alive=-1`).

The Hunyuan worker gives back only 13.13 of its 20.81 GiB: `release_memory()`
calls `empty_cache()`, which returns unreserved blocks only, and **~7.3 GiB of
CUDA reservation stays held**. See Known limitations.

### The runs

Same reference image (1448×1086), same seed 42, same 4096² texture, throughout.

| | 1 · Standard | 2 · High Quality | 3 · Ultra |
|---|---|---|---|
| mode | `512` | `1024_cascade` | `1536_cascade` |
| exclusive | **no** | yes | yes |
| MemAvailable at start | 73.00 | 54.07 → **87.64** | 57.57 → **95.47** |
| freed by exclusive mode | — | 33.57 | 37.90 |
| headroom at start | 73.30 | 106.80 | 115.00 |
| **min MemAvailable during run** | **45.61** | **74.20** | **84.45** |
| margin over the 40 GiB floor | +5.61 | +34.20 | +44.45 |
| **peak drawdown** | **27.69** | **32.60** | **30.55** |
| stored estimate at the time | 27.00 | 44.00 | 64.00 |
| wall time | 209.4 s | 199.8 s | 211.4 s |
| — generate | 92.8 s | 84.7 s | 89.3 s |
| — export | 43.7 s | 113.1 s | 119.8 s |
| raw verts / faces | 420,175 / 866,830 | 1,765,499 / 3,595,686 | **4,037,189 / 8,196,962** |
| GLB verts / faces | 623,796 / 952,470 | 691,840 / 982,211 | 719,984 / 971,402 |
| GLB size | 32.92 MB | 36.44 MB | 37.33 MB |
| texture | 4096² | 4096² | 4096² |
| restore succeeded | n/a | ✅ | ✅ |
| MemAvailable after restore | 53.78 | 49.80 | 57.27 |
| Ollama after restore | untouched | `qwen3.6:35b-a3b`, pinned | `qwen3.6:35b-a3b`, pinned |
| content-factory after restore | untouched | `comfyui` active, `pipeline-worker` active | same |

### Did Ultra actually run at 1536?

TRELLIS does not log its internal resolution, and steps 1536 down by 128 when an
object exceeds the 49,152-token budget. The mesh arithmetic answers it — surface
voxel count scales with the square of the resolution, and HQ and Ultra ran on
the same image:

| cascade resolution | predicted raw verts | |
|---|---|---|
| 1024 (measured HQ) | 1,765,499 | |
| 1280 | 2,758,592 | |
| 1408 | 3,337,896 | |
| **1536** | **3,972,372** | measured **4,037,189**, within 1.6% |

**Ultra ran at the full 1536³ and did not step down.**

### The Ultra estimate, replaced

`1536_cascade` carried a 64.0 GiB guess. The first real run drew **30.55 GiB**.

It has been set to **48.0 GiB, not 31**, deliberately:

* One image is one data point. Peak scales with mesh complexity.
* The same reference produced a 32.6 GiB HQ run, while `1024_cascade`'s stored
  44.0 came from a 43.3 GiB run on a heavier image — **a third more, from the
  image alone**.
* Ultra cannot draw less than HQ in general; measuring 30.55 against HQ's 32.6
  here is export-phase noise, not a real inversion. Putting Ultra below HQ would
  be incoherent.

48.0 sits above HQ's 44.0, leaves ~17 GiB over the one measurement, and takes
Ultra's required headroom from 104.0 to **88.0** — which is what makes it
reachable with real margin instead of by 2.6 GiB.

---

## Restoration behaviour

Restoration runs in `engine.generate`'s `finally` block, so it covers every
exit: success, a refusal by the gate, a backend error, a CUDA error, any Python
exception, and a caller that walks away. It restores **only** what the snapshot
recorded as running.

Proved on the failure path (Test 3), an exclusive run made to raise *during*
generation, after the workloads were already paused:

```
23:40:46 preparing   pipeline-worker=active     ollama=qwen3.6:35b-a3b
23:40:50 generating  pipeline-worker=inactive   ollama=UNLOADED      <- paused
23:40:58 generating  pipeline-worker=inactive   ollama=UNLOADED
23:41:02 restoring   pipeline-worker=active     ollama=UNLOADED      <- restoring
23:41:06 restoring   pipeline-worker=active     ollama=qwen3.6:35b-a3b
23:41:10 error       pipeline-worker=active     ollama=qwen3.6:35b-a3b
```

`restore_ok=True`, no errors, model back and pinned to 2318, MemAvailable back
to 49.87 from 49.79. The job record carries the full `exclusive_report` on the
error path exactly as it does on success.

Restoration failures are never swallowed: logged at ERROR, persisted, exposed at
`/system` → `exclusive.last_restore`, attached to the job as `exclusive_report`,
and shown in the dashboard as a red banner naming the recovery command.

---

## Crash recovery, tested

**Lease expiry** — acquired a 60 s lease and never renewed:

```
23:20:53  acquired, freed 37.92 GiB, pipeline-worker inactive
23:21:55  t+60s  exclusive=True   pipeline-worker=activating  ollama=UNLOADED
23:22:05  t+70s  exclusive=False  pipeline-worker=active      ollama=qwen3.6:35b-a3b
          trigger: lease_expired, errors: []
```

**Daemon killed mid-run** — `systemctl kill -s KILL` while holding a 3600 s
lease:

```
forge3d-resctl.service: Main process exited, code=killed, status=9/KILL
Started forge3d-resctl.service
ERROR STARTUP RECOVERY: state file says exclusive mode was active;
      restoring the workloads it recorded
  -> pipeline-worker.service started
  -> qwen3.6:35b-a3b re-warmed with keep_alive=-1
```

Both restored with no manual action.

---

## Known limitations

**1. The Hunyuan worker strands ~7.3 GiB.** `POST /unload` drops the pipelines
and calls `empty_cache()`, but PyTorch's caching allocator keeps ~7.3 GiB of
reservation that only process exit returns. Measured: a worker with **no models
loaded at all** still held 7.9 GiB; restarting it recovered 7.75 GiB.

This is what decides whether Ultra is available. With that reservation stranded,
exclusive headroom was 101.4 GiB and **Ultra was correctly refused by 2.6 GiB**.
After `docker restart 3d-generator-hunyuan` it was 109.3 GiB and Ultra ran.

Exclusive mode as scoped cannot clear it — only a container restart can, and
that would mean giving the helper Docker control. If Ultra is refused and the
worker is idle:

    docker restart 3d-generator-hunyuan     # ~8 s, reloads models lazily

**2. `512`'s stored peak is 0.69 GiB optimistic.** The normal-mode Standard run
drew **27.69 GiB** against the stored 27.0. Deliberately **not changed**: raising
it to 28.0 would take Standard's required headroom from 67 to 68 GiB and make it
harder to start in normal mode, which this milestone must not do. Flagged for a
separate decision. In practice the run still bottomed out at 45.61 GiB, 5.6 GiB
clear of the floor.

**3. Ultra's 48.0 GiB rests on one run.** See above. It should be revised as
more Ultra runs happen, especially on denser subjects.

**4. Exclusive mode is TRELLIS-only in the UI.** The Hunyuan worker is itself one
of the things being paused, so offering the checkbox there would be incoherent.
The API does not forbid it; the UI simply does not present it.

**5. Hunyuan models are not re-warmed.** The worker reloads on its next
generation. Stated in the restore report rather than implied.

**6. Denial of service is inherent.** Anyone who can reach the dashboard can
pause `pipeline-worker` and unload Ollama for up to the lease length. Bounded by
the lease, logged with the peer uid, and not a privilege escalation.

---

## Manual browser test

Dashboard: `http://<configured-host>:8189`

1. Select **TRELLIS.2**. The **Exclusive / Max Quality** checkbox appears under
   Quality, with the pause-and-restore copy.
2. With it **clear**: High Quality and Ultra read *Unavailable now*, and hovering
   gives the real numbers against the 40 GiB floor. This is unchanged Step 5
   behaviour.
3. **Tick it.** High Quality and Ultra become selectable, marked with a dashed
   border and `· exclusive` — not shown as ordinary availability. A panel lists
   exactly what will pause (`qwen3.6:35b-a3b ~32.19 GiB`,
   `pipeline-worker.service`) and the projected MemAvailable, labelled as
   estimated and noting the run is gated on the measured figure.
4. Switch the Model picker to a Hunyuan generator: the checkbox disappears.
   Switch back: it returns.
5. Drop in a reference image, choose **High Quality**, Generate. The run panel
   shows six steps: **Free memory → Load model → Generate → Export GLB →
   Restore services → Done**.
6. While it runs, on the DGX: `systemctl is-active pipeline-worker` is
   `inactive`, `ollama ps` is empty, and `systemctl is-active comfyui` is still
   `active`.
7. When it finishes: a *Services restored* toast, and both are back.
8. Untick the box: High Quality and Ultra return to *Unavailable now*.

---

## Known-good final state

    forge3d-resctl.service   active (running), enabled
    socket                   /run/forge3d-resctl/resctl.sock  srw-rw---- root:1000
    pipeline-worker.service  active
    comfyui.service          active  (never touched)
    ollama.service           active, qwen3.6:35b-a3b resident 32.19 GiB, pinned
    3d-generator-trellis     up, exclusive mode available
    3d-generator-hunyuan     up, 0.29 GiB, models load lazily

    TRELLIS_PROTECTED_FLOOR_GB = 40   (unchanged)
    TRELLIS_MIN_AVAILABLE_GB   = 65   (unchanged)

Tests: 19/19 Step 6 · 20/20 Step 5 · 10/10 Step 6 UI · 15/15 Step 5 UI.
