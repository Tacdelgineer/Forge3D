"""Generation engine: TRELLIS.2 in-process, Hunyuan3D through its worker.

TRELLIS is loaded lazily in this process and stays resident for the container's
lifetime; `docker compose down` is what releases it. Hunyuan runs in a separate
optional container (see hunyuan/) because its pinned dependency set conflicts
with TRELLIS's — this module only posts images to it and receives a GLB.

The scaffolding around a run is shared by both backends: the generation id,
the input/output directories, the memory gate, the 0.5 s memory sampler, the
metadata record, GLB validation and the post-run cleanup. Only the step that
actually produces a mesh differs.

Exclusive / Max Quality (Step 6) wraps that scaffolding and changes nothing
inside it. It takes a lease from the host helper before the gate, so the gate
then runs against a machine that really has the memory, and releases the lease
in the finally block so the workloads come back whether the run succeeded,
failed, raised or was abandoned. The pipeline itself, the sampler, the cleanup
and the output are byte-for-byte the normal path.
"""
import ctypes
import gc
import json
import logging
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, hunyuan_client, memory, resctl

log = logging.getLogger("trellis.engine")

# Only one generation at a time. There is exactly one GPU, so a plain
# in-process lock is the right size of solution here — and it covers the
# Hunyuan worker too, which has its own single-flight lock.
_gen_lock = threading.Lock()
_load_lock = threading.Lock()

_pipeline = None
_load_error: str | None = None
_active: dict | None = None


def is_loaded() -> bool:
    return _pipeline is not None


def load_error() -> str | None:
    return _load_error


def active_generation() -> dict | None:
    return _active


def gpu_available() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def gpu_info() -> dict:
    try:
        import torch

        if not torch.cuda.is_available():
            return {"available": False}
        return {
            "available": True,
            "name": torch.cuda.get_device_name(0),
            "capability": ".".join(map(str, torch.cuda.get_device_capability(0))),
            "torch": torch.__version__,
            "torch_allocated_gb": round(torch.cuda.memory_allocated() / 1024**3, 2),
            "torch_reserved_gb": round(torch.cuda.memory_reserved() / 1024**3, 2),
        }
    except Exception as exc:  # pragma: no cover - diagnostic path
        return {"available": False, "error": str(exc)}


def _empty_cuda_cache() -> None:
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available() and torch.cuda.is_initialized():
        torch.cuda.empty_cache()


def _malloc_trim() -> None:
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def release_memory() -> None:
    """Hand back everything a finished run used; keep only the model."""
    gc.collect()
    _empty_cuda_cache()
    _malloc_trim()


def get_pipeline():
    """Load the TRELLIS.2 pipeline, once, under a lock."""
    global _pipeline, _load_error
    if _pipeline is not None:
        return _pipeline

    with _load_lock:
        if _pipeline is not None:
            return _pipeline

        log.info("Loading TRELLIS.2 pipeline from %s ...", config.MODEL_REPO)
        t0 = time.time()
        try:
            from trellis2.pipelines import Trellis2ImageTo3DPipeline

            pipe = Trellis2ImageTo3DPipeline.from_pretrained(config.MODEL_REPO)
            pipe.cuda()
        except Exception as exc:
            _load_error = f"{type(exc).__name__}: {exc}"
            log.exception("TRELLIS.2 failed to load")
            raise
        _pipeline = pipe
        _load_error = None
        log.info("TRELLIS.2 loaded in %.1fs", time.time() - t0)
        return _pipeline


class Busy(Exception):
    """A generation is already running."""


class BackendUnavailable(Exception):
    """The backend for this generator is not reachable."""


def backend_state(gen_id: str) -> dict:
    """Is this generator's backend up, and what does it hold?"""
    g = config.generator(gen_id)
    if not g:
        return {"ok": False, "reason": f"unknown generator {gen_id!r}"}
    if g.get("backend") == "local":
        return {
            "ok": True,
            "loaded": is_loaded(),
            "load_error": load_error(),
            "footprint_gb": memory.forge3d_footprint_gb(),
        }
    s = hunyuan_client.status()
    if not s:
        return {
            "ok": False,
            "loaded": False,
            "footprint_gb": 0.0,
            "reason": (
                "The Hunyuan worker container is not running. Start it with "
                "`docker compose --profile hunyuan up -d`."
            ),
        }
    loaded = (s.get("loaded") or {}).get("shape")
    return {
        "ok": True,
        "loaded": loaded == gen_id,
        "worker_loaded": loaded,
        "footprint_gb": float((s.get("memory") or {}).get("footprint_gb") or 0.0),
        "gpu": s.get("gpu"),
    }


class _MemSampler:
    """Samples MemAvailable and our footprint every 0.5 s for a whole run."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.min_available: float | None = None
        self.max_anon = 0.0
        self.max_gpu = 0.0
        self._stop = threading.Event()
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name="forge3d-mem", daemon=True)

    def _sample(self) -> None:
        avail = memory.available_gb()
        self.min_available = avail if self.min_available is None else min(self.min_available, avail)
        self.max_anon = max(self.max_anon, memory.process_anon_gb())
        self.max_gpu = max(self.max_gpu, memory.gpu_footprint_gb())

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except Exception:
                pass

    def start(self) -> "_MemSampler":
        self._sample()
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._sample()
        except Exception:
            pass


def _validate_glb(path: Path) -> dict:
    """Cheap structural check: does the GLB actually contain geometry?"""
    result: dict = {"readable": False}
    try:
        import trimesh

        scene = trimesh.load(str(path), force="scene")
        meshes = [g for g in scene.geometry.values() if hasattr(g, "faces")]
        result["readable"] = True
        result["mesh_count"] = len(meshes)
        result["vertices"] = int(sum(len(m.vertices) for m in meshes))
        result["faces"] = int(sum(len(m.faces) for m in meshes))
        materials = []
        for m in meshes:
            visual = getattr(m, "visual", None)
            mat = getattr(visual, "material", None)
            if mat is None:
                continue
            entry = {"name": getattr(mat, "name", None) or type(mat).__name__}
            for attr in ("baseColorTexture", "metallicRoughnessTexture", "normalTexture"):
                tex = getattr(mat, attr, None)
                if tex is not None:
                    entry[attr] = list(getattr(tex, "size", []) or []) or True
            materials.append(entry)
        result["materials"] = materials
        result["has_texture"] = any(len(m) > 1 for m in materials)
        result["has_geometry"] = result["faces"] > 0
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# --------------------------------------------------------------------------- #
# TRELLIS.2, in this process
# --------------------------------------------------------------------------- #
def _run_trellis(ref_path: Path, output_dir: Path, seed: int, mode: str,
                 texture_size: int, state) -> dict:
    from PIL import Image

    image = mesh = glb = None
    try:
        if not is_loaded():
            state("loading_model")
        t_load0 = time.time()
        pipeline = get_pipeline()
        load_s = time.time() - t_load0
        mem_after_load = memory.available_gb()

        import torch

        torch.cuda.reset_peak_memory_stats()

        image = Image.open(ref_path)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        state("generating")
        t_gen0 = time.time()
        mesh = pipeline.run(image, seed=seed, pipeline_type=mode)[0]
        mesh_raw = {"vertices": int(mesh.vertices.shape[0]), "faces": int(mesh.faces.shape[0])}
        mesh.simplify(16777216)  # nvdiffrast index limit
        gen_s = time.time() - t_gen0

        # Hand the diffusion stage's cached CUDA blocks back before the
        # CPU-heavy export instead of carrying them through it.
        gc.collect()
        _empty_cuda_cache()

        state("exporting")
        t_exp0 = time.time()
        import o_voxel

        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices,
            faces=mesh.faces,
            attr_volume=mesh.attrs,
            coords=mesh.coords,
            attr_layout=mesh.layout,
            voxel_size=mesh.voxel_size,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=config.GLB_DECIMATION_TARGET,
            texture_size=texture_size,
            remesh=True,
            remesh_band=1,
            remesh_project=0,
            verbose=True,
        )
        glb.export(str(output_dir / "model.glb"), extension_webp=True)
        export_s = time.time() - t_exp0

        gpu_peak = 0.0
        try:
            gpu_peak = torch.cuda.max_memory_reserved() / 1024**3
        except Exception:
            pass
        return {
            "mesh_raw": mesh_raw,
            "available_after_load": mem_after_load,
            "gpu_reserved_peak": round(gpu_peak, 2),
            "settings": {"texture_size": texture_size},
            "timings": {
                "pipeline_load": round(load_s, 1),
                "generation": round(gen_s, 1),
                "glb_export": round(export_s, 1),
            },
        }
    finally:
        image = mesh = glb = None


# --------------------------------------------------------------------------- #
# Hunyuan3D, through the worker container
# --------------------------------------------------------------------------- #
def _run_hunyuan(gen_id: str, images: dict[str, bytes], output_dir: Path,
                 seed: int, mode: str, settings: dict, state) -> dict:
    state("generating")
    glb_bytes, stats = hunyuan_client.generate(gen_id, mode, seed, images, settings)
    state("exporting")
    (output_dir / "model.glb").write_bytes(glb_bytes)
    t = stats.get("timings_s") or {}
    wm = stats.get("memory_gb") or {}
    return {
        "mesh_raw": stats.get("mesh_raw"),
        "available_after_load": wm.get("available_before"),
        "gpu_reserved_peak": wm.get("gpu_reserved_peak"),
        "settings": stats.get("settings") or settings,
        "textured": stats.get("textured"),
        "worker_memory_gb": wm,
        "timings": {
            "pipeline_load": t.get("pipeline_load", 0.0),
            "generation": t.get("shape", 0.0),
            "texture": t.get("texture", 0.0),
            "glb_export": 0.0,
        },
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def generate(
    images: dict[str, bytes] | bytes,
    filename: str,
    seed: int,
    mode: str,
    generator: str = config.MODEL_ID,
    texture_size: int | None = None,
    settings: dict | None = None,
    on_state=None,
    exclusive: bool = False,
    on_exclusive=None,
) -> dict:
    """Run one image-to-3D generation end to end. Blocking.

    `images` is a mapping of view name -> bytes. Single-reference generators use
    {"front": ...}; plain bytes are accepted for backward compatibility.
    on_state, if given, is called with "preparing" / "loading_model" /
    "generating" / "exporting" / "restoring" as the run progresses — real
    transitions, no synthetic percentage.

    exclusive=True asks the host helper to free the releasable workloads first
    and restore them afterwards. on_exclusive, if given, is called once with the
    lease report (what was paused, what came back, what did not) so a caller can
    put it on a job record; it is called even when the run fails.
    """
    global _active

    def state(name):
        if on_state is not None:
            on_state(name)

    if isinstance(images, (bytes, bytearray)):
        images = {"front": bytes(images)}
    gen = config.generator(generator)
    if gen is None:
        raise ValueError(f"unknown generator {generator!r}")
    texture_size = int(texture_size or config.GLB_TEXTURE_SIZE)
    settings = dict(settings or {})

    if not _gen_lock.acquire(blocking=False):
        raise Busy()

    gen_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    _active = {
        "id": gen_id,
        "generator": generator,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    sampler: _MemSampler | None = None
    cleaned = False
    lease: resctl.Lease | None = None
    output_dir: Path | None = None
    metadata: dict | None = None

    try:
        backend = backend_state(generator)
        if not backend.get("ok"):
            raise BackendUnavailable(backend.get("reason") or "backend unavailable")

        if exclusive:
            # Release BEFORE the gate, so the gate below judges the machine as
            # it will actually be during the run. If the helper refuses, this
            # raises and nothing has been touched.
            state("preparing")
            lease = resctl.Lease()
            lease.acquire()

        # Authoritative check, inside the lock, so nothing can start between the
        # decision and the allocation. On an exclusive run this is the measured
        # post-release figure, never the helper's projection: if the memory did
        # not actually arrive, the run is refused here and the finally block
        # restores everything.
        gate = memory.require_mode(mode, generator)

        upload_dir = config.UPLOAD_DIR / gen_id
        output_dir = config.OUTPUT_DIR / gen_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(filename).suffix.lower() or ".png"
        single = gen.get("inputs") == "single"
        saved: dict[str, str] = {}
        for view, blob in images.items():
            name = f"reference{suffix}" if single else f"{view}{suffix}"
            path = upload_dir / name
            path.write_bytes(blob)
            saved[view] = path.name
        ref_path = upload_dir / saved.get("front", next(iter(saved.values())))

        sampler = _MemSampler().start()

        if gen.get("backend") == "local":
            out = _run_trellis(ref_path, output_dir, seed, mode, texture_size, state)
        else:
            out = _run_hunyuan(generator, images, output_dir, seed, mode, settings, state)

        glb_path = output_dir / "model.glb"
        validation = _validate_glb(glb_path)
        sampler.stop()
        release_memory()
        cleaned = True

        size_bytes = glb_path.stat().st_size
        timings = {"total": round(time.time() - started, 1), **(out.get("timings") or {})}
        metadata = {
            "id": gen_id,
            "name": Path(filename).stem or gen_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_filename": filename,
            "reference": str(ref_path),
            "input_views": saved,
            "views": sorted(saved.keys()),
            "glb": str(glb_path),
            "glb_bytes": size_bytes,
            "glb_mb": round(size_bytes / 1024**2, 2),
            "seed": seed,
            # Step 2-4 assets carry pipeline_type; keep writing it so nothing
            # that reads older metadata has to special-case the new field.
            "pipeline_type": mode,
            "mode": mode,
            "mode_label": config.MODE_LABELS.get(mode, mode),
            "generator": generator,
            "generator_name": gen["name"],
            "model": config.UPSTREAM[generator]["weights"],
            "texture_size": texture_size if generator == "trellis" else None,
            "settings": out.get("settings") or settings,
            "mesh_raw": out.get("mesh_raw"),
            "timings_s": timings,
            "memory_gb": {
                "available_before": gate["available_gb"],
                "footprint_before": gate["footprint_gb"],
                "backend_footprint_before": gate["backend_footprint_gb"],
                "headroom_before": gate["headroom_gb"],
                "peak_estimate": gate["peak_gb"],
                "projected_min": gate["projected_min_gb"],
                "available_after_load": out.get("available_after_load"),
                # true minimum, sampled every 0.5 s through the whole run
                "available_min_observed": round(sampler.min_available, 2),
                # How far MemAvailable actually fell. This is the honest figure
                # for calibrating config.MODE_PEAK_GB on a worker-backed run,
                # where peak_drawdown below also counts memory this process
                # holds and never released.
                "available_drop": round(gate["available_gb"] - sampler.min_available, 2),
                # Headroom-relative draw: correct for TRELLIS (same process),
                # inflated for Hunyuan (includes TRELLIS's resident model).
                "peak_drawdown": round(gate["headroom_gb"] - sampler.min_available, 2),
                "anon_peak": round(sampler.max_anon, 2),
                "gpu_reserved_peak": out.get("gpu_reserved_peak"),
                "available_after": memory.available_gb(),
                "footprint_after_cleanup": memory.forge3d_footprint_gb(),
                "worker": out.get("worker_memory_gb"),
            },
            "gpu": gpu_info() if generator == "trellis" else (backend.get("gpu") or {}),
            "validation": validation,
        }
        if lease is not None:
            # The release half is known now; the restore half is filled in by
            # the finally block, which rewrites this file.
            metadata["exclusive"] = lease.report()
        (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
        return metadata
    finally:
        if sampler is not None:
            sampler.stop()
        if not cleaned:
            release_memory()
        if lease is not None:
            # Runs for every exit: success, refusal, backend error, CUDA error,
            # an exception from anywhere above, or a caller that walked away.
            state("restoring")
            lease.release()
            report = lease.report()
            if not report["restore_ok"]:
                log.error(
                    "EXCLUSIVE RESTORE DID NOT COMPLETE for %s: %s", gen_id,
                    "; ".join(report["restore_errors"]) or "unknown",
                )
            if metadata is not None and output_dir is not None:
                # `metadata` is the object already returned to the caller, so
                # mutating it here updates what they receive as well as the file.
                metadata["exclusive"] = report
                try:
                    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
                except Exception:
                    log.exception("could not rewrite metadata with the restore report")
            if on_exclusive is not None:
                try:
                    on_exclusive(report)
                except Exception:
                    log.exception("on_exclusive callback failed")
        _active = None
        _gen_lock.release()
