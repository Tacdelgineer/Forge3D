"""Forge3D API + dashboard.

Serves the static dashboard at / and the JSON API used by it. The synchronous
POST /generate from Step 2 is kept working unchanged; the dashboard uses the
non-blocking POST /jobs + GET /jobs/{id} pair instead.
"""
import io
import logging
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import assets, config, engine, jobs, memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trellis.api")

app = FastAPI(title="Forge3D — TRELLIS.2 backend", version="0.3.0")

STATIC_DIR = Path(__file__).parent / "static"

# Accepted reference-image formats, checked by decoding the bytes rather than
# trusting the filename or the client's content-type.
ALLOWED_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


@app.on_event("startup")
def _startup() -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Forge3D up. model=%s default_mode=%s min_available_gb=%s (lazy load: TRELLIS NOT resident)",
        config.MODEL_REPO,
        config.DEFAULT_PIPELINE_TYPE,
        config.MIN_AVAILABLE_GB,
    )


# --------------------------------------------------------------------------- #
# Step 2 endpoints, unchanged behaviour
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": config.MODEL_REPO.split("/")[-1],
        "gpu_available": engine.gpu_available(),
    }


@app.get("/system")
def system():
    avail = memory.available_gb()
    active = jobs.active_job()
    return {
        "memory": {
            "total_gb": memory.total_gb(),
            "used_gb": memory.used_gb(),
            "available_gb": avail,
            "min_required_gb": config.MIN_AVAILABLE_GB,
            "sufficient": avail >= config.MIN_AVAILABLE_GB,
        },
        "gpu": engine.gpu_info(),
        "model": {
            "repo": config.MODEL_REPO,
            "loaded": engine.is_loaded(),
            "load_error": engine.load_error(),
            "default_pipeline_type": config.DEFAULT_PIPELINE_TYPE,
        },
        "generation": {
            "active": engine.active_generation() is not None or active is not None,
            "current": engine.active_generation(),
            "job": active.as_dict() if active else None,
        },
    }


def _read_image(upload: UploadFile) -> tuple[bytes, str] | JSONResponse:
    """Return (bytes, normalised extension) or a JSONResponse describing why not."""
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        return JSONResponse(status_code=400, content={"error": "empty_image"})
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": "image_too_large", "max_mb": MAX_UPLOAD_BYTES // 1024**2},
        )
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            im.verify()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_image",
                "detail": "File is not a readable PNG, JPG/JPEG or WebP image.",
                "accepted": ["PNG", "JPG", "JPEG", "WebP"],
            },
        )
    if fmt not in ALLOWED_FORMATS:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_image",
                "detail": f"{fmt or 'Unknown'} images are not supported.",
                "accepted": ["PNG", "JPG", "JPEG", "WebP"],
            },
        )
    return data, ALLOWED_FORMATS[fmt]


@app.post("/generate")
def generate(
    image: UploadFile = File(...),
    seed: int = Form(42),
    pipeline_type: str = Form(None),
):
    ptype = pipeline_type or config.DEFAULT_PIPELINE_TYPE
    if ptype not in config.VALID_PIPELINE_TYPES:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_pipeline_type",
                "given": ptype,
                "valid": list(config.VALID_PIPELINE_TYPES),
            },
        )
    try:
        memory.require_headroom()
    except memory.InsufficientMemory as exc:
        return JSONResponse(status_code=503, content=exc.as_dict())

    read = _read_image(image)
    if isinstance(read, JSONResponse):
        return read
    data, ext = read

    try:
        return engine.generate(data, image.filename or f"reference{ext}", seed, ptype)
    except engine.Busy:
        return JSONResponse(
            status_code=409,
            content={"error": "generation_in_progress", "current": engine.active_generation()},
        )
    except memory.InsufficientMemory as exc:
        return JSONResponse(status_code=503, content=exc.as_dict())
    except Exception as exc:
        log.exception("Generation failed")
        return JSONResponse(
            status_code=500,
            content={"error": "generation_failed", "detail": f"{type(exc).__name__}: {exc}"},
        )


# --------------------------------------------------------------------------- #
# Jobs — non-blocking generation for the dashboard
# --------------------------------------------------------------------------- #
@app.post("/jobs")
def create_job(
    image: UploadFile = File(...),
    mode: str = Form(None),
    seed: int = Form(42),
):
    ptype = mode or config.DEFAULT_PIPELINE_TYPE
    if ptype not in config.VALID_PIPELINE_TYPES:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_pipeline_type",
                "given": ptype,
                "valid": list(config.VALID_PIPELINE_TYPES),
            },
        )

    read = _read_image(image)
    if isinstance(read, JSONResponse):
        return read
    data, ext = read

    # Fail fast so the dashboard can show the real numbers immediately rather
    # than queueing a job that is going to be refused a moment later.
    try:
        memory.require_headroom()
    except memory.InsufficientMemory as exc:
        return JSONResponse(status_code=503, content=exc.as_dict())

    existing = jobs.active_job()
    if existing is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "generation_in_progress", "job": existing.as_dict()},
        )

    job = jobs.submit(data, image.filename or f"reference{ext}", seed, ptype)
    return JSONResponse(status_code=202, content=job.as_dict())


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "job_not_found", "job_id": job_id})
    return job.as_dict()


# --------------------------------------------------------------------------- #
# Asset library
# --------------------------------------------------------------------------- #
def _asset_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, assets.BadAssetId):
        return JSONResponse(status_code=400, content={"error": "invalid_asset_id"})
    return JSONResponse(status_code=404, content={"error": "asset_not_found"})


@app.get("/assets")
def list_assets():
    items = assets.list_assets()
    return {"count": len(items), "assets": items}


@app.get("/assets/{asset_id}")
def get_asset(asset_id: str):
    try:
        return assets.get_asset(asset_id)
    except (assets.BadAssetId, assets.AssetNotFound) as exc:
        return _asset_error(exc)


@app.get("/assets/{asset_id}/metadata")
def get_asset_metadata(asset_id: str):
    try:
        return assets.get_metadata(asset_id)
    except (assets.BadAssetId, assets.AssetNotFound) as exc:
        return _asset_error(exc)


@app.get("/assets/{asset_id}/model.glb")
def get_asset_glb(asset_id: str):
    try:
        p = assets.glb_path(asset_id)
    except (assets.BadAssetId, assets.AssetNotFound) as exc:
        return _asset_error(exc)
    return FileResponse(p, media_type="model/gltf-binary")


@app.get("/assets/{asset_id}/download")
def download_asset(asset_id: str):
    try:
        p = assets.glb_path(asset_id)
        info = assets.get_asset(asset_id)
    except (assets.BadAssetId, assets.AssetNotFound) as exc:
        return _asset_error(exc)
    safe = "".join(c for c in info["name"] if c.isalnum() or c in " ._-").strip() or asset_id
    return FileResponse(p, media_type="model/gltf-binary", filename=f"{safe}.glb")


@app.get("/assets/{asset_id}/thumbnail")
def get_asset_thumbnail(asset_id: str):
    try:
        p = assets.thumbnail_path(asset_id)
    except assets.BadAssetId as exc:
        return _asset_error(exc)
    if p is None:
        return JSONResponse(status_code=404, content={"error": "thumbnail_not_found"})
    suffix = p.suffix.lower()
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp"}.get(suffix, "application/octet-stream")
    return FileResponse(p, media_type=media)


@app.patch("/assets/{asset_id}")
def rename_asset(asset_id: str, payload: dict = Body(...)):
    name = payload.get("name") if isinstance(payload, dict) else None
    try:
        return assets.rename_asset(asset_id, name or "")
    except (assets.BadAssetId, assets.AssetNotFound) as exc:
        return _asset_error(exc)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": "invalid_name", "detail": str(exc)})


@app.delete("/assets/{asset_id}")
def delete_asset(asset_id: str):
    try:
        return assets.delete_asset(asset_id)
    except (assets.BadAssetId, assets.AssetNotFound) as exc:
        return _asset_error(exc)


# --------------------------------------------------------------------------- #
# Dashboard (mounted last so it cannot shadow the API)
# --------------------------------------------------------------------------- #
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def dashboard():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return JSONResponse(status_code=404, content={"error": "dashboard_not_built"})
    return FileResponse(index, media_type="text/html")
