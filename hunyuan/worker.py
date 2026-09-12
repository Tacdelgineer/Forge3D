"""Hunyuan3D worker — one process, two generators, lazy loading.

Serves:
  hunyuan21   Hunyuan3D-2.1   single image -> shape, optional PBR texture
  hunyuan2mv  Hunyuan3D-2mv   1-4 views (front/left/back/right) -> shape,
                              optional PBR texture via 2.1's paint pipeline

Deliberately small: two endpoints and a status probe. Forge3D owns the asset
library, job queue and memory gate; this worker only turns images into a GLB
and reports what it used.

Runs with CWD=/opt/Hunyuan3D-2.1 because 2.1's configs use paths relative to
its repo root.
"""
import ctypes
import gc
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hunyuan.worker")

app = FastAPI(title="Forge3D Hunyuan3D worker", version="0.5.0")

HY21_REPO = os.environ.get("HY21_REPO", "tencent/Hunyuan3D-2.1")
HY2MV_REPO = os.environ.get("HY2MV_REPO", "tencent/Hunyuan3D-2mv")
HY2MV_SUBFOLDER = os.environ.get("HY2MV_SUBFOLDER", "hunyuan3d-dit-v2-mv")
VIEWS = ("front", "back", "left", "right")

_lock = threading.Lock()          # one generation at a time; one GPU
_state = {"shape": None, "shape_repo": None, "paint": None}

_MEMINFO = Path("/proc/meminfo")
_STATUS = Path("/proc/self/status")


# --------------------------------------------------------------------------- #
# memory helpers (same accounting as Forge3D: RssAnon + torch CUDA reserve)
# --------------------------------------------------------------------------- #
def _kv(path: Path, key: str) -> int:
    for line in path.read_text().splitlines():
        if line.startswith(key + ":"):
            parts = line.split()
            if parts[1].isdigit():
                return int(parts[1])
    return 0


def available_gb() -> float:
    return round(_kv(_MEMINFO, "MemAvailable") / 1024**2, 2)


def anon_gb() -> float:
    return round(_kv(_STATUS, "RssAnon") / 1024**2, 2)


def gpu_gb() -> float:
    torch = sys.modules.get("torch")
    if torch is None:
        return 0.0
    try:
        if not torch.cuda.is_initialized():
            return 0.0
        reserved = torch.cuda.memory_reserved() / 1024**3
    except Exception:
        return 0.0
    return round(reserved + (0.4 if reserved > 0 else 0.0), 2)


def footprint_gb() -> float:
    return round(anon_gb() + gpu_gb(), 2)


def _malloc_trim() -> None:
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def release_memory() -> None:
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available() and torch.cuda.is_initialized():
                torch.cuda.empty_cache()
        except Exception:
            pass
    _malloc_trim()


class _Sampler:
    """0.5 s sampling of MemAvailable and our footprint, for the whole run."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.min_available = None
        self.max_anon = 0.0
        self.max_gpu = 0.0
        self._stop = threading.Event()
        self._stopped = False
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _sample(self):
        a = available_gb()
        self.min_available = a if self.min_available is None else min(self.min_available, a)
        self.max_anon = max(self.max_anon, anon_gb())
        self.max_gpu = max(self.max_gpu, gpu_gb())

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except Exception:
                pass

    def start(self):
        self._sample()
        self._t.start()
        return self

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        self._t.join(timeout=2)
        try:
            self._sample()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# model loading
# --------------------------------------------------------------------------- #
def _apply_torchvision_fix() -> None:
    # 2.1 ships this: basicsr/realesrgan import torchvision.transforms.functional_tensor,
    # which modern torchvision removed.
    try:
        from torchvision_fix import apply_fix

        apply_fix()
    except Exception as exc:  # pragma: no cover
        log.warning("torchvision fix not applied: %s", exc)


def load_shape(generator: str):
    """Load the shape pipeline for `generator`, unloading the other one first.

    Both model families are multi-GB; keeping only one resident is what makes
    a single worker viable on a shared-memory box.
    """
    if _state["shape_repo"] == generator and _state["shape"] is not None:
        return _state["shape"]

    if _state["shape"] is not None:
        log.info("unloading shape pipeline for %s", _state["shape_repo"])
        _state["shape"] = None
        _state["shape_repo"] = None
        release_memory()

    _apply_torchvision_fix()
    t0 = time.time()
    if generator == "hunyuan21":
        from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline

        pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(HY21_REPO)
    elif generator == "hunyuan2mv":
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline as MVPipeline

        pipe = MVPipeline.from_pretrained(
            HY2MV_REPO, subfolder=HY2MV_SUBFOLDER, use_safetensors=True
        )
    else:
        raise ValueError(f"unknown generator {generator!r}")
    _state["shape"] = pipe
    _state["shape_repo"] = generator
    log.info("%s shape pipeline loaded in %.1fs", generator, time.time() - t0)
    return pipe


def load_paint():
    if _state["paint"] is not None:
        return _state["paint"]
    _apply_torchvision_fix()
    t0 = time.time()
    from textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline

    conf = Hunyuan3DPaintConfig(max_num_view=6, resolution=512)
    conf.realesrgan_ckpt_path = "hy3dpaint/ckpt/RealESRGAN_x4plus.pth"
    conf.multiview_cfg_path = "hy3dpaint/cfgs/hunyuan-paint-pbr.yaml"
    conf.custom_pipeline = "hy3dpaint/hunyuanpaintpbr"
    _state["paint"] = Hunyuan3DPaintPipeline(conf)
    log.info("paint pipeline loaded in %.1fs", time.time() - t0)
    return _state["paint"]


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
@app.get("/status")
def status():
    info = {
        "ok": True,
        "generators": ["hunyuan21", "hunyuan2mv"],
        "views": list(VIEWS),
        "loaded": {"shape": _state["shape_repo"], "paint": _state["paint"] is not None},
        "busy": _lock.locked(),
        "memory": {
            "available_gb": available_gb(),
            "footprint_gb": footprint_gb(),
            "anon_gb": anon_gb(),
            "gpu_gb": gpu_gb(),
        },
    }
    try:
        import torch

        info["gpu"] = {
            "available": torch.cuda.is_available(),
            "name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "capability": ".".join(map(str, torch.cuda.get_device_capability(0)))
            if torch.cuda.is_available()
            else None,
            "torch": torch.__version__,
        }
    except Exception as exc:
        info["gpu"] = {"available": False, "error": str(exc)}
    return info


@app.post("/unload")
def unload():
    with _lock:
        _state["shape"] = None
        _state["shape_repo"] = None
        _state["paint"] = None
        release_memory()
    return {"unloaded": True, "footprint_gb": footprint_gb()}


@app.post("/generate")
async def generate(
    generator: str = Form(...),
    mode: str = Form("shape"),
    seed: int = Form(42),
    octree_resolution: int = Form(256),
    num_inference_steps: int = Form(30),
    guidance_scale: float = Form(5.0),
    image: UploadFile = File(None),
    front: UploadFile = File(None),
    back: UploadFile = File(None),
    left: UploadFile = File(None),
    right: UploadFile = File(None),
):
    if generator not in ("hunyuan21", "hunyuan2mv"):
        return JSONResponse(status_code=400, content={"error": "unknown_generator", "given": generator})
    if mode not in ("shape", "shape_texture"):
        return JSONResponse(status_code=400, content={"error": "unknown_mode", "given": mode})

    uploads = {"front": front, "back": back, "left": left, "right": right}
    if generator == "hunyuan21":
        blob = image or front
        if blob is None:
            return JSONResponse(status_code=400, content={"error": "image_required"})
        views = {"front": await blob.read()}
    else:
        views = {k: await v.read() for k, v in uploads.items() if v is not None}
        if "front" not in views or not views["front"]:
            return JSONResponse(status_code=400, content={"error": "front_view_required"})

    if not _lock.acquire(blocking=False):
        return JSONResponse(status_code=409, content={"error": "busy"})

    sampler = None
    try:
        import torch
        from PIL import Image as PILImage

        sampler = _Sampler().start()
        t_start = time.time()
        avail_before = available_gb()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            paths = {}
            for tag, data in views.items():
                p = tmp / f"{tag}.png"
                img = PILImage.open(io.BytesIO(data))
                img.save(p)
                paths[tag] = p

            # Background removal: Hunyuan needs RGBA with the subject cut out.
            from hy3dshape.rembg import BackgroundRemover

            rembg = None
            pil = {}
            for tag, p in paths.items():
                img = PILImage.open(p)
                if img.mode == "RGB":
                    rembg = rembg or BackgroundRemover()
                    img = rembg(img)
                else:
                    img = img.convert("RGBA")
                pil[tag] = img
                img.save(p)

            t_load0 = time.time()
            pipe = load_shape(generator)
            load_s = time.time() - t_load0

            torch.manual_seed(seed)
            gen = torch.Generator(device="cuda").manual_seed(seed)
            kwargs = dict(
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
                octree_resolution=octree_resolution,
                generator=gen,
                output_type="trimesh",
            )

            t_shape0 = time.time()
            if generator == "hunyuan21":
                out = pipe(image=pil["front"], **kwargs)
            else:
                # The real 2mv API: a dict keyed by view name. MVImageProcessorV2
                # maps front/left/back/right -> view indices and accepts 1-4 of them.
                out = pipe(image={k: pil[k] for k in VIEWS if k in pil}, **kwargs)
            mesh = out[0]
            shape_s = time.time() - t_shape0

            raw = {
                "vertices": int(len(mesh.vertices)),
                "faces": int(len(mesh.faces)),
            }

            shape_glb = tmp / "shape.glb"
            mesh.export(str(shape_glb))
            del mesh, out
            gc.collect()
            if torch.cuda.is_available() and torch.cuda.is_initialized():
                torch.cuda.empty_cache()

            paint_s = 0.0
            result = shape_glb
            textured = False
            if mode == "shape_texture":
                t_paint0 = time.time()
                paint = load_paint()
                # output_mesh_path MUST end in .obj. The pipeline writes an OBJ
                # there and derives the GLB name itself with
                #     output_mesh_path.replace(".obj", ".glb")
                # so handing it a .glb path makes that replace a no-op: it then
                # passes an OBJ file named .glb to trimesh, which dispatches on
                # the extension and fails with "incorrect header on GLB file".
                out_obj = tmp / "textured.obj"
                returned = paint(
                    mesh_path=str(shape_glb),
                    image_path=str(paths["front"]),
                    output_mesh_path=str(out_obj),
                )
                paint_s = time.time() - t_paint0

                out_glb = out_obj.with_suffix(".glb")
                cand = Path(returned) if returned else out_glb
                if not cand.is_file() or cand.suffix.lower() != ".glb":
                    cand = out_glb
                # Fall back to the untextured shape rather than returning a
                # file that is not a GLB; `textured` then reports the truth.
                result = cand if cand.is_file() else shape_glb
                textured = result != shape_glb

            glb_bytes = result.read_bytes()

        sampler.stop()
        gpu_peak = 0.0
        try:
            gpu_peak = round(torch.cuda.max_memory_reserved() / 1024**3, 2)
        except Exception:
            pass
        release_memory()

        stats = {
            "generator": generator,
            "mode": mode,
            "seed": seed,
            "views": sorted(views.keys()),
            "settings": {
                "octree_resolution": octree_resolution,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
            },
            "textured": textured,
            "mesh_raw": raw,
            "glb_bytes": len(glb_bytes),
            "timings_s": {
                "pipeline_load": round(load_s, 1),
                "shape": round(shape_s, 1),
                "texture": round(paint_s, 1),
                "total": round(time.time() - t_start, 1),
            },
            "memory_gb": {
                "available_before": avail_before,
                "available_min_observed": round(sampler.min_available, 2),
                "peak_drawdown": round(avail_before - sampler.min_available, 2),
                "anon_peak": round(sampler.max_anon, 2),
                "gpu_reserved_peak": gpu_peak,
                "available_after": available_gb(),
                "footprint_after": footprint_gb(),
            },
        }
        log.info("done: %s", json.dumps(stats["timings_s"]))
        return Response(
            content=glb_bytes,
            media_type="model/gltf-binary",
            headers={"X-Hunyuan-Stats": json.dumps(stats)},
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to Forge3D verbatim
        log.exception("generation failed")
        return JSONResponse(
            status_code=500,
            content={"error": "generation_failed", "detail": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        if sampler is not None:
            sampler.stop()
        release_memory()
        _lock.release()
