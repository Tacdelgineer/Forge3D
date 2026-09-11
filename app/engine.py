"""TRELLIS.2 lazy loading and generation.

The pipeline is loaded on first use rather than at container start, so that
`docker compose up -d` costs almost nothing until an actual generation is
requested. Once loaded it stays resident for the life of the container;
`docker compose down` is what releases it.

After every run everything except the model is handed back (see
release_memory). Before Step 4 a finished run left 10-25 GiB of freeable
memory behind - PyTorch's CUDA cache plus glibc heap - which is what made an
idle Forge3D look like it was starving the machine.
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

from . import config, memory

log = logging.getLogger("trellis.engine")

# Only one generation at a time. TRELLIS.2 is not documented as safe to run
# concurrently, and there is exactly one GPU, so a plain in-process lock is
# the right size of solution here.
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
    """Hand back everything a finished run used; keep only the model.

    gc frees the mesh/latent tensors, empty_cache returns torch's cached CUDA
    blocks, and malloc_trim returns freed glibc heap pages. On GB10 all three
    land straight back in MemAvailable, because there is only one pool.
    """
    gc.collect()
    _empty_cuda_cache()
    _malloc_trim()


def get_pipeline():
    """Load the TRELLIS.2 pipeline, once, under a lock.

    The memory check happens in generate(), which knows the mode and holds the
    generation lock; loading on its own has nothing to decide.
    """
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


class _MemSampler:
    """Samples MemAvailable and our footprint every 0.5 s for a whole run.

    The Step 2/3 engine only looked at three instants and never during GLB
    export, where the minimum actually happens: it recorded 44.1 GiB for a run
    an external sampler had measured bottoming out at 40.1 GiB.
    """

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


def generate(
    image_bytes: bytes,
    filename: str,
    seed: int,
    pipeline_type: str,
    texture_size: int | None = None,
    on_state=None,
) -> dict:
    """Run one image-to-3D generation end to end. Blocking.

    on_state, if given, is called with "loading_model" / "generating" /
    "exporting" as the run progresses. These are real transitions - no
    synthetic percentage is invented.
    """
    global _active

    def _state(name):
        if on_state is not None:
            on_state(name)

    texture_size = int(texture_size or config.GLB_TEXTURE_SIZE)

    if not _gen_lock.acquire(blocking=False):
        raise Busy()

    gen_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    _active = {"id": gen_id, "started_at": datetime.now(timezone.utc).isoformat()}
    sampler: _MemSampler | None = None
    cleaned = False
    image = mesh = glb = None

    try:
        # Authoritative check, inside the lock, so nothing can start between the
        # decision and the allocation.
        gate = memory.require_mode(pipeline_type)

        from PIL import Image

        upload_dir = config.UPLOAD_DIR / gen_id
        output_dir = config.OUTPUT_DIR / gen_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(filename).suffix.lower() or ".png"
        ref_path = upload_dir / f"reference{suffix}"
        ref_path.write_bytes(image_bytes)

        sampler = _MemSampler().start()

        # Load (may be the first call -> several minutes of weight download)
        if not is_loaded():
            _state("loading_model")
        t_load0 = time.time()
        pipeline = get_pipeline()
        load_s = time.time() - t_load0
        mem_after_load = memory.available_gb()

        import torch

        torch.cuda.reset_peak_memory_stats()

        image = Image.open(ref_path)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        _state("generating")
        t_gen0 = time.time()
        mesh = pipeline.run(image, seed=seed, pipeline_type=pipeline_type)[0]
        mesh_raw = {"vertices": int(mesh.vertices.shape[0]), "faces": int(mesh.faces.shape[0])}
        mesh.simplify(16777216)  # nvdiffrast index limit
        gen_s = time.time() - t_gen0

        # Hand the diffusion stage's cached CUDA blocks back before the
        # CPU-heavy export instead of carrying them through it.
        gc.collect()
        _empty_cuda_cache()

        _state("exporting")
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
        glb_path = output_dir / "model.glb"
        glb.export(str(glb_path), extension_webp=True)
        export_s = time.time() - t_exp0
        image = mesh = glb = None

        validation = _validate_glb(glb_path)
        sampler.stop()
        gpu_reserved_peak = torch.cuda.max_memory_reserved() / 1024**3

        release_memory()
        cleaned = True
        available_after = memory.available_gb()
        footprint_after = memory.forge3d_footprint_gb()

        size_bytes = glb_path.stat().st_size
        metadata = {
            "id": gen_id,
            "name": Path(filename).stem or gen_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_filename": filename,
            "reference": str(ref_path),
            "glb": str(glb_path),
            "glb_bytes": size_bytes,
            "glb_mb": round(size_bytes / 1024**2, 2),
            "seed": seed,
            "pipeline_type": pipeline_type,
            "mode_label": config.MODE_LABELS.get(pipeline_type, pipeline_type),
            "texture_size": texture_size,
            "model": config.MODEL_REPO,
            "generator": config.MODEL_ID,
            "mesh_raw": mesh_raw,
            "timings_s": {
                "pipeline_load": round(load_s, 1),
                "generation": round(gen_s, 1),
                "glb_export": round(export_s, 1),
                "total": round(time.time() - started, 1),
            },
            "memory_gb": {
                "available_before": gate["available_gb"],
                "footprint_before": gate["footprint_gb"],
                "headroom_before": gate["headroom_gb"],
                "peak_estimate": gate["peak_gb"],
                "projected_min": gate["projected_min_gb"],
                "available_after_load": mem_after_load,
                # true minimum, sampled every 0.5 s through load, generation and export
                "available_min_observed": round(sampler.min_available, 2),
                # what this run actually took out of MemAvailable: the number that
                # calibrates config.MODE_PEAK_GB
                "peak_drawdown": round(gate["headroom_gb"] - sampler.min_available, 2),
                "anon_peak": round(sampler.max_anon, 2),
                "gpu_reserved_peak": round(gpu_reserved_peak, 2),
                "available_after": available_after,
                "footprint_after_cleanup": footprint_after,
            },
            "gpu": gpu_info(),
            "validation": validation,
        }
        (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
        return metadata
    finally:
        if sampler is not None:
            sampler.stop()
        image = mesh = glb = None
        if not cleaned:
            release_memory()
        _active = None
        _gen_lock.release()
