"""Thin client for the optional Hunyuan worker container.

Stdlib only, on purpose: the TRELLIS image's dependency set is known-good and
this adds nothing to it. The worker is reached over the compose network by
service name, and everything about it is optional — when the container is not
running, `status()` returns None and the Hunyuan generators show as
unavailable with that reason.
"""
import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("trellis.hunyuan")

BASE_URL = os.environ.get("HUNYUAN_URL", "http://hunyuan:8190").rstrip("/")
STATUS_TTL = 4.0          # seconds; /system polls every 4 s
STATUS_TIMEOUT = 2.5
GENERATE_TIMEOUT = 3600   # a textured run can take several minutes

_cache: dict = {"at": 0.0, "value": None}


def _multipart(fields: dict[str, str], files: dict[str, bytes]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    for key, blob in files.items():
        ctype = mimetypes.guess_type(f"{key}.png")[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{key}.png"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
            + blob
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def status(force: bool = False) -> dict | None:
    """Worker status, or None when it is not reachable. Cached briefly."""
    now = time.time()
    if not force and now - _cache["at"] < STATUS_TTL:
        return _cache["value"]
    try:
        with urllib.request.urlopen(f"{BASE_URL}/status", timeout=STATUS_TIMEOUT) as r:
            value = json.load(r)
    except Exception:
        value = None
    _cache.update(at=now, value=value)
    return value


def footprint_gb() -> float:
    """What the worker already holds — memory a Hunyuan run would reuse."""
    s = status()
    if not s:
        return 0.0
    return float((s.get("memory") or {}).get("footprint_gb") or 0.0)


def is_available() -> bool:
    return status() is not None


class HunyuanError(RuntimeError):
    pass


def generate(
    generator: str,
    mode: str,
    seed: int,
    images: dict[str, bytes],
    settings: dict | None = None,
) -> tuple[bytes, dict]:
    """Run one generation on the worker. Returns (glb_bytes, stats)."""
    settings = settings or {}
    fields = {
        "generator": generator,
        "mode": mode,
        "seed": str(seed),
        "octree_resolution": str(settings.get("octree_resolution", 256)),
        "num_inference_steps": str(settings.get("num_inference_steps", 30)),
        "guidance_scale": str(settings.get("guidance_scale", 5.0)),
    }
    body, ctype = _multipart(fields, images)
    req = urllib.request.Request(
        f"{BASE_URL}/generate", data=body, method="POST", headers={"Content-Type": ctype}
    )
    try:
        with urllib.request.urlopen(req, timeout=GENERATE_TIMEOUT) as r:
            glb = r.read()
            raw = r.headers.get("X-Hunyuan-Stats") or "{}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail).get("detail") or detail
        except Exception:
            pass
        raise HunyuanError(f"worker HTTP {exc.code}: {detail}") from None
    except Exception as exc:
        raise HunyuanError(f"worker unreachable: {type(exc).__name__}: {exc}") from None

    try:
        stats = json.loads(raw)
    except Exception:
        stats = {}
    if not glb.startswith(b"glTF"):
        raise HunyuanError("worker returned something that is not a GLB")
    # The worker's model changed, so its footprint did too.
    _cache.update(at=0.0, value=None)
    return glb, stats


def unload() -> bool:
    try:
        req = urllib.request.Request(f"{BASE_URL}/unload", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=60):
            pass
    except Exception:
        return False
    _cache.update(at=0.0, value=None)
    return True
