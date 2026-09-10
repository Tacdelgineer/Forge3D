"""Unified-memory headroom checks.

/proc/meminfo inside the container reports the host's values, which is exactly
what we want: on GB10 there is no separate VRAM pool, so MemAvailable is the
single number that governs whether a generation can safely proceed.
"""
from pathlib import Path

from . import config

_MEMINFO = Path("/proc/meminfo")


def _read_meminfo() -> dict[str, int]:
    values = {}
    for line in _MEMINFO.read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            values[key] = int(parts[0])  # kB
    return values


def available_gb() -> float:
    return round(_read_meminfo()["MemAvailable"] / 1024**2, 2)


def total_gb() -> float:
    return round(_read_meminfo()["MemTotal"] / 1024**2, 2)


def used_gb() -> float:
    m = _read_meminfo()
    used = m["MemTotal"] - m["MemFree"] - m["Buffers"] - m["Cached"] - m.get("SReclaimable", 0)
    return round(used / 1024**2, 2)


class InsufficientMemory(Exception):
    """Raised instead of letting the allocator OOM the machine."""

    def __init__(self, available: float, required: float):
        self.available = available
        self.required = required
        super().__init__(f"{available} GiB available, {required} GiB required")

    def as_dict(self) -> dict:
        return {
            "error": "insufficient_memory",
            "available_gb": self.available,
            "required_gb": self.required,
        }


def require_headroom() -> float:
    """Check MemAvailable against the configured floor.

    Deliberately does NOT free anyone else's memory: Ollama and ComfyUI are
    other people's workloads and unloading them is a user decision, not ours.
    """
    avail = available_gb()
    if avail < config.MIN_AVAILABLE_GB:
        raise InsufficientMemory(avail, config.MIN_AVAILABLE_GB)
    return avail
