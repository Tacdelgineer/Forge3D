"""TRELLIS.2 lazy loading and generation.

The pipeline is loaded on first use rather than at container start, so that
`docker compose up -d` costs almost nothing until an actual generation is
requested. Once loaded it stays resident for the life of the container;
`docker compose down` is what releases it.
"""
import json
import logging
import os
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


def get_pipeline():
    """Load the TRELLIS.2 pipeline, once, under a lock."""
    global _pipeline, _load_error
    if _pipeline is not None:
        return _pipeline

    with _load_lock:
        if _pipeline is not None:
            return _pipeline

        memory.require_headroom()

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


def generate(image_bytes: bytes, filename: str, seed: int, pipeline_type: str) -> dict:
    """Run one image-to-3D generation end to end. Blocking."""
    global _active

    if not _gen_lock.acquire(blocking=False):
        raise Busy()

    gen_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    _active = {"id": gen_id, "started_at": datetime.now(timezone.utc).isoformat()}

    try:
        from PIL import Image

        upload_dir = config.UPLOAD_DIR / gen_id
        output_dir = config.OUTPUT_DIR / gen_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(filename).suffix.lower() or ".png"
        ref_path = upload_dir / f"reference{suffix}"
        ref_path.write_bytes(image_bytes)

        mem_before = memory.available_gb()

        # Load (may be the first call -> several minutes of weight download)
        t_load0 = time.time()
        pipeline = get_pipeline()
        load_s = time.time() - t_load0

        mem_after_load = memory.available_gb()

        image = Image.open(ref_path)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        t_gen0 = time.time()
        mesh = pipeline.run(image, seed=seed, pipeline_type=pipeline_type)[0]
        mesh.simplify(16777216)  # nvdiffrast index limit
        gen_s = time.time() - t_gen0

        mem_peak_available = min(mem_before, mem_after_load, memory.available_gb())

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
            texture_size=config.GLB_TEXTURE_SIZE,
            remesh=True,
            remesh_band=1,
            remesh_project=0,
            verbose=True,
        )
        glb_path = output_dir / "model.glb"
        glb.export(str(glb_path), extension_webp=True)
        export_s = time.time() - t_exp0

        size_bytes = glb_path.stat().st_size
        metadata = {
            "id": gen_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_filename": filename,
            "reference": str(ref_path),
            "glb": str(glb_path),
            "glb_bytes": size_bytes,
            "glb_mb": round(size_bytes / 1024**2, 2),
            "seed": seed,
            "pipeline_type": pipeline_type,
            "model": config.MODEL_REPO,
            "timings_s": {
                "pipeline_load": round(load_s, 1),
                "generation": round(gen_s, 1),
                "glb_export": round(export_s, 1),
                "total": round(time.time() - started, 1),
            },
            "memory_gb": {
                "available_before": mem_before,
                "available_after_load": mem_after_load,
                "available_min_observed": mem_peak_available,
                "available_after": memory.available_gb(),
            },
            "gpu": gpu_info(),
            "validation": _validate_glb(glb_path),
        }
        (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
        return metadata
    finally:
        _active = None
        _gen_lock.release()
