"""Unified-memory headroom checks.

GB10 has no separate VRAM: CPU and GPU draw from one 128 GB pool, and
/proc/meminfo inside the container reports the host's values. Three quantities
decide whether a generation may start:

  MemAvailable       what the kernel could hand out right now.
  Forge3D footprint  what this process already holds and would reuse or give
                     back: anonymous RSS (RssAnon) plus CUDA memory (torch's
                     reserved pool and the context).
  worker footprint   the same, for the optional Hunyuan worker container, when
                     assessing a Hunyuan generator.

Their sum is the backend's *headroom*. A resident model is part of the
footprint, so it no longer makes the gate refuse a generation that could
safely reuse it.

Deliberately never frees anyone else's memory: unloading Ollama or ComfyUI is
a user decision, not ours.
"""
import sys
from pathlib import Path

from . import config

_MEMINFO = Path("/proc/meminfo")
_STATUS = Path("/proc/self/status")

# nvidia-smi per-process usage minus torch.cuda.memory_reserved() on this host.
CUDA_CONTEXT_GB = 0.4


def _read_kb(path: Path) -> dict[str, int]:
    values = {}
    for line in path.read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0])  # kB
    return values


def _read_meminfo() -> dict[str, int]:
    return _read_kb(_MEMINFO)


def available_gb() -> float:
    return round(_read_meminfo()["MemAvailable"] / 1024**2, 2)


def total_gb() -> float:
    return round(_read_meminfo()["MemTotal"] / 1024**2, 2)


def used_gb() -> float:
    m = _read_meminfo()
    used = m["MemTotal"] - m["MemFree"] - m["Buffers"] - m["Cached"] - m.get("SReclaimable", 0)
    return round(used / 1024**2, 2)


def process_anon_gb() -> float:
    """RssAnon, not VmRSS.

    File-backed resident pages (mmapped weights, shared libraries) are
    reclaimable and already counted inside MemAvailable; adding them would
    double-count. `docker stats` makes exactly that mistake — it reported
    39 GiB for a process whose RssAnon was 23.9 GiB.
    """
    return _read_kb(_STATUS).get("RssAnon", 0) / 1024**2


def gpu_footprint_gb() -> float:
    """torch's reserved CUDA pool plus the context, if CUDA is in use.

    Never imports torch or initialises CUDA just to answer this. When the pool
    has been emptied the context is not counted, which under-states the
    footprint and therefore errs toward refusing, never toward allowing.
    """
    torch = sys.modules.get("torch")
    if torch is None:
        return 0.0
    try:
        if not torch.cuda.is_initialized():
            return 0.0
        reserved = torch.cuda.memory_reserved() / 1024**3
    except Exception:
        return 0.0
    return reserved + (CUDA_CONTEXT_GB if reserved > 0 else 0.0)


def forge3d_footprint_gb() -> float:
    return round(process_anon_gb() + gpu_footprint_gb(), 2)


def backend_footprint_gb(gen_id: str) -> float:
    """Memory the backend that will run `gen_id` already holds."""
    g = config.generator(gen_id)
    if g and g.get("backend") == "hunyuan":
        from . import hunyuan_client

        return round(hunyuan_client.footprint_gb(), 2)
    return forge3d_footprint_gb()


def headroom_gb() -> float:
    return round(available_gb() + forge3d_footprint_gb(), 2)


def assess(
    mode: str,
    generator: str = config.MODEL_ID,
    *,
    available: float | None = None,
    footprint: float | None = None,
    extra_footprint: float | None = None,
) -> dict:
    """Can `generator`/`mode` start now without taking the machine below the floor?"""
    avail = available_gb() if available is None else available
    foot = forge3d_footprint_gb() if footprint is None else footprint
    g = config.generator(generator)
    is_local = (g or {}).get("backend", "local") == "local"
    if extra_footprint is None:
        extra = backend_footprint_gb(generator) if not is_local else 0.0
    else:
        extra = extra_footprint

    # Only memory the *run itself* could reuse counts as headroom. A resident
    # TRELLIS model is reusable by a TRELLIS run, so it is credited there. It is
    # NOT reusable by a Hunyuan run: TRELLIS keeps holding it while the worker
    # allocates on top, so crediting it would let a Hunyuan run start with far
    # less real memory than the gate believed. That mistake let a multi-view run
    # take MemAvailable to 36.0 GiB against a 40 GiB floor (docs/step-05).
    credited = foot if is_local else 0.0
    head = round(avail + credited + extra, 2)

    peak = config.peak_gb(generator, mode)
    measured = config.peak_measured(generator, mode)
    floor = config.PROTECTED_FLOOR_GB
    label = config.MODE_LABELS.get(mode, mode)

    required_head = max(config.MIN_AVAILABLE_GB, floor + peak)
    projected_min = round(head - peak, 1)
    ok = head >= required_head
    shortfall = round(max(0.0, required_head - head), 1)

    reason = None
    if not ok:
        kind = "measured" if measured else "estimated"
        if projected_min < floor:
            reason = (
                f"{label} peaks at ~{peak:.0f} GiB ({kind}). Starting now would take "
                f"MemAvailable down to ~{projected_min:.1f} GiB, under the {floor:.0f} GiB "
                f"floor that keeps content-factory's jobs able to run. "
                f"~{shortfall:.1f} GiB more free memory is needed."
            )
        else:
            reason = (
                f"This backend needs {required_head:.0f} GiB of headroom; {head:.1f} GiB is "
                f"available. ~{shortfall:.1f} GiB more free memory is needed."
            )

    return {
        "generator": generator,
        "mode": mode,
        "mode_label": label,
        "available": ok,
        "available_gb": round(avail, 1),
        "footprint_gb": round(foot, 1),
        # What of that footprint actually counted toward headroom (0 for a
        # Hunyuan run: Forge3D's own resident model is not reusable by it).
        "footprint_credited_gb": round(credited, 1),
        "backend_footprint_gb": round(extra, 1),
        "headroom_gb": round(head, 1),
        "peak_gb": peak,
        "peak_measured": measured,
        "floor_gb": floor,
        "min_headroom_gb": config.MIN_AVAILABLE_GB,
        "required_headroom_gb": round(required_head, 1),
        # The MemAvailable figure that would satisfy the gate right now, so the
        # UI can compare like with like.
        "required_available_gb": round(required_head - credited - extra, 1),
        "projected_min_gb": projected_min,
        "shortfall_gb": shortfall,
        "reason": reason,
    }


class InsufficientMemory(Exception):
    """Raised instead of letting a run take the machine below the floor."""

    def __init__(self, info: dict):
        self.info = info
        self.available = info["available_gb"]
        self.required = info["required_available_gb"]
        self.reason = info["reason"]
        super().__init__(self.reason)

    def as_dict(self) -> dict:
        i = self.info
        return {
            "error": "insufficient_memory",
            "generator": i["generator"],
            "mode": i["mode"],
            "mode_label": i["mode_label"],
            "available_gb": i["available_gb"],
            "required_gb": i["required_available_gb"],
            "headroom_gb": i["headroom_gb"],
            "required_headroom_gb": i["required_headroom_gb"],
            "projected_min_gb": i["projected_min_gb"],
            "floor_gb": i["floor_gb"],
            "peak_gb": i["peak_gb"],
            "peak_measured": i["peak_measured"],
            "shortfall_gb": i["shortfall_gb"],
            "reason": i["reason"],
        }


def require_mode(mode: str, generator: str = config.MODEL_ID) -> dict:
    info = assess(mode, generator)
    if not info["available"]:
        raise InsufficientMemory(info)
    return info
