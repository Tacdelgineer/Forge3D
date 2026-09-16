"""Forge3D API + dashboard.

Serves the static dashboard at / and the JSON API used by it. The synchronous
POST /generate from Step 2 is kept working; the dashboard uses the
non-blocking POST /jobs + GET /jobs/{id} pair instead.
"""
import io
import logging
import secrets
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import assets, config, engine, hunyuan_client, jobs, memory, resctl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trellis.api")

app = FastAPI(title="Forge3D — TRELLIS.2 backend", version="0.5.0")

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
        "Forge3D up. model=%s default_mode=%s min_headroom_gb=%s floor_gb=%s "
        "(lazy load: TRELLIS NOT resident)",
        config.MODEL_REPO,
        config.DEFAULT_PIPELINE_TYPE,
        config.MIN_AVAILABLE_GB,
        config.PROTECTED_FLOOR_GB,
    )


# --------------------------------------------------------------------------- #
# Health and system state
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
    foot = memory.forge3d_footprint_gb()
    active = jobs.active_job()
    generating = engine.active_generation() is not None or active is not None

    # One read of the host helper for the whole response: it is a socket round
    # trip, and every mode below needs the same answer.
    rstate = resctl.state()
    exclusive_ready = bool(rstate) and not rstate.get("exclusive")
    releasable = float((rstate or {}).get("releasable", {}).get("total_est_gb") or 0.0)

    generators = []
    for g in config.GENERATORS:
        backend = engine.backend_state(g["id"])
        extra = 0.0 if g["backend"] == "local" else backend.get("footprint_gb", 0.0)
        modes = []
        for m in g["modes"]:
            a = memory.assess(m["id"], g["id"], available=avail, footprint=foot,
                              extra_footprint=extra)
            entry = {**m, **{k: a[k] for k in (
                "available", "peak_gb", "peak_measured", "projected_min_gb",
                "required_headroom_gb", "required_available_gb", "shortfall_gb", "reason")}}
            # What this mode would look like once the releasable workloads are
            # gone. Only offered where it can actually be delivered: without a
            # working helper there is nothing to free, so it mirrors `available`
            # rather than promising memory nobody can produce.
            if exclusive_ready and releasable > 0:
                x = memory.assess(m["id"], g["id"], available=avail, footprint=foot,
                                  extra_footprint=extra, exclusive=True,
                                  releasable=releasable)
                entry["exclusive_available"] = x["available"]
                entry["exclusive_projected_min_gb"] = x["projected_min_gb"]
                entry["exclusive_reason"] = x["reason"]
            else:
                entry["exclusive_available"] = entry["available"]
                entry["exclusive_projected_min_gb"] = entry["projected_min_gb"]
                entry["exclusive_reason"] = entry["reason"]
            if not backend.get("ok"):
                # A missing backend is not a memory problem; say which it is.
                entry["available"] = False
                entry["exclusive_available"] = False
                entry["reason"] = backend.get("reason")
                entry["exclusive_reason"] = backend.get("reason")
            modes.append(entry)
        up = config.UPSTREAM[g["id"]]
        generators.append({
            "id": g["id"],
            "name": g["name"],
            "blurb": g.get("blurb", ""),
            "backend": g["backend"],
            "backend_ok": bool(backend.get("ok")),
            "backend_reason": backend.get("reason"),
            "loaded": bool(backend.get("loaded")),
            "inputs": g["inputs"],
            "views": list(g.get("views") or []),
            "required_views": list(g.get("required_views") or []),
            "texture_control": bool(g.get("texture_control")),
            "default_mode": g["default_mode"],
            "modes": modes,
            "upstream": up,
        })

    default = memory.assess(config.DEFAULT_PIPELINE_TYPE, config.MODEL_ID,
                            available=avail, footprint=foot, extra_footprint=0.0)

    if generating:
        state = "generating"
    elif engine.is_loaded():
        state = "model_loaded"
    else:
        state = "ready"

    return {
        "state": state,
        "memory": {
            "total_gb": memory.total_gb(),
            "used_gb": memory.used_gb(),
            "available_gb": avail,
            "forge3d_footprint_gb": foot,
            "headroom_gb": round(avail + foot, 2),
            "floor_gb": config.PROTECTED_FLOOR_GB,
            "min_headroom_gb": config.MIN_AVAILABLE_GB,
            # Step 2/3 field names, kept for compatibility.
            "min_required_gb": config.MIN_AVAILABLE_GB,
            "sufficient": default["available"],
        },
        "gpu": engine.gpu_info(),
        "model": {
            "id": config.MODEL_ID,
            "name": config.MODEL_NAME,
            "repo": config.MODEL_REPO,
            "loaded": engine.is_loaded(),
            "load_error": engine.load_error(),
            "default_pipeline_type": config.DEFAULT_PIPELINE_TYPE,
        },
        "models": [{"id": g["id"], "name": g["name"]} for g in config.GENERATORS],
        "generators": generators,
        "default_generator": config.DEFAULT_GENERATOR,
        # Step 4 shape, kept so older clients keep working: TRELLIS's modes.
        "modes": next(x["modes"] for x in generators if x["id"] == config.MODEL_ID),
        "texture": {"sizes": list(config.TEXTURE_SIZES), "default": config.GLB_TEXTURE_SIZE},
        "exclusive": _exclusive_block(rstate, avail, releasable),
        "seed_max": config.SEED_MAX,
        "generation": {
            "active": generating,
            "current": engine.active_generation(),
            "job": active.as_dict() if active else None,
        },
    }


def _exclusive_block(rstate: dict | None, avail: float, releasable: float) -> dict:
    """Honest state of Exclusive / Max Quality.

    Reports what the helper actually says, including "we do not know": if it is
    not installed or not answering, `supported` is False and the reason says so
    rather than the UI offering memory that nothing can free.
    """
    if rstate is None:
        return {
            "supported": False,
            "reason": (
                "The Forge3D host helper is not installed or not running, so no "
                "workloads can be freed. Install it with `sudo host/install.sh`."
            ),
            "socket": resctl.SOCKET_PATH,
            "releasable_gb": 0.0,
            "projected_available_gb": avail,
            "would_pause": [],
            "held": False,
            "last_restore": None,
        }
    held = bool(rstate.get("exclusive"))
    last = rstate.get("last_restore") or {}
    return {
        "supported": True,
        "reason": None if not held else "Exclusive mode is currently held.",
        "socket": resctl.SOCKET_PATH,
        "releasable_gb": round(releasable, 2),
        # An estimate, and labelled as one everywhere it is shown. The number a
        # run is actually gated on is measured after the release, not this.
        "projected_available_gb": round(avail + releasable, 2),
        "would_pause": resctl.describe_pause(rstate),
        "held": held,
        "lease": rstate.get("lease"),
        # Surfaced prominently and never cleared silently: if a previous run
        # failed to put something back, it shows here until it is fixed.
        "last_restore": {
            "trigger": last.get("trigger"),
            "errors": last.get("errors") or [],
            "ok": not (last.get("errors") or []),
        } if last else None,
    }


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #
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


def _bad(error: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": error, **extra})


def _parse_generator(raw: str | None) -> str | JSONResponse:
    gen = raw or config.DEFAULT_GENERATOR
    if gen not in config.GENERATOR_IDS:
        return _bad("invalid_generator", given=gen, valid=list(config.GENERATOR_IDS))
    return gen


def _parse_mode(raw: str | None, generator: str = config.MODEL_ID) -> str | JSONResponse:
    g = config.generator(generator) or {}
    mode = raw or g.get("default_mode") or config.DEFAULT_PIPELINE_TYPE
    valid = config.valid_modes(generator)
    if mode not in valid:
        return _bad("invalid_pipeline_type", given=mode, generator=generator, valid=list(valid))
    return mode


def _parse_seed(raw: str | None) -> int | JSONResponse:
    """Omitted -> a fresh random seed. Given -> must be 0..SEED_MAX."""
    if raw is None or str(raw).strip() == "":
        return secrets.randbelow(config.SEED_MAX + 1)
    try:
        seed = int(str(raw).strip())
    except ValueError:
        return _bad("invalid_seed", given=raw, min=0, max=config.SEED_MAX)
    if not 0 <= seed <= config.SEED_MAX:
        return _bad("invalid_seed", given=raw, min=0, max=config.SEED_MAX)
    return seed


def _parse_exclusive(raw: str | None) -> bool:
    """Exclusive / Max Quality is opt-in, so anything that is not an explicit
    yes is a no. An unrecognised value never silently enables it."""
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_texture(raw: str | None) -> int | JSONResponse:
    if raw is None or str(raw).strip() == "":
        return config.GLB_TEXTURE_SIZE
    try:
        size = int(str(raw).strip())
    except ValueError:
        size = -1
    if size not in config.TEXTURE_SIZES:
        return _bad("invalid_texture_size", given=raw, valid=list(config.TEXTURE_SIZES))
    return size


def _gate(mode: str, generator: str = config.MODEL_ID, *,
          exclusive: bool = False) -> JSONResponse | None:
    backend = engine.backend_state(generator)
    if not backend.get("ok"):
        return JSONResponse(status_code=503, content={
            "error": "backend_unavailable", "generator": generator,
            "reason": backend.get("reason")})
    if exclusive and not resctl.available():
        return JSONResponse(status_code=503, content={
            "error": "exclusive_unavailable", "generator": generator,
            "reason": (
                "Exclusive / Max Quality needs the Forge3D host helper, which is "
                "not installed or not running. Install it with "
                "`sudo host/install.sh`."
            )})
    info = memory.assess(mode, generator, exclusive=exclusive)
    if not info["available"]:
        return JSONResponse(status_code=503, content=memory.InsufficientMemory(info).as_dict())
    return None


# --------------------------------------------------------------------------- #
# Step 2 synchronous endpoint
# --------------------------------------------------------------------------- #
@app.post("/generate")
def generate(
    image: UploadFile = File(...),
    seed: int = Form(42),
    pipeline_type: str = Form(None),
    texture_size: str = Form(None),
):
    ptype = _parse_mode(pipeline_type)
    if isinstance(ptype, JSONResponse):
        return ptype
    tex = _parse_texture(texture_size)
    if isinstance(tex, JSONResponse):
        return tex
    refused = _gate(ptype, config.MODEL_ID)
    if refused is not None:
        return refused

    read = _read_image(image)
    if isinstance(read, JSONResponse):
        return read
    data, ext = read

    try:
        return engine.generate({"front": data}, image.filename or f"reference{ext}", seed, ptype,
                               generator=config.MODEL_ID, texture_size=tex)
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
    image: UploadFile = File(None),
    front: UploadFile = File(None),
    back: UploadFile = File(None),
    left: UploadFile = File(None),
    right: UploadFile = File(None),
    generator: str = Form(None),
    mode: str = Form(None),
    seed: str = Form(None),
    texture_size: str = Form(None),
    exclusive: str = Form(None),
):
    gen_id = _parse_generator(generator)
    if isinstance(gen_id, JSONResponse):
        return gen_id
    gen = config.generator(gen_id)

    ptype = _parse_mode(mode, gen_id)
    if isinstance(ptype, JSONResponse):
        return ptype
    seed_v = _parse_seed(seed)
    if isinstance(seed_v, JSONResponse):
        return seed_v
    tex = _parse_texture(texture_size)
    if isinstance(tex, JSONResponse):
        return tex

    # Single-reference generators accept `image` (or `front`); the multi-view
    # generator accepts up to four named views, of which front is required.
    uploads = {"front": front, "back": back, "left": left, "right": right}
    if gen["inputs"] == "single":
        blob = image or front
        if blob is None:
            return _bad("image_required")
        uploads = {"front": blob}
    else:
        if image is not None and front is None:
            uploads["front"] = image
        uploads = {k: v for k, v in uploads.items() if v is not None}
        missing = [v for v in gen.get("required_views", ()) if v not in uploads]
        if missing:
            return _bad("view_required", missing=missing,
                        views=list(gen.get("views") or []))
        if not uploads:
            return _bad("image_required")

    images: dict[str, bytes] = {}
    first_name = None
    for view, upload in uploads.items():
        read = _read_image(upload)
        if isinstance(read, JSONResponse):
            return read
        data, ext = read
        images[view] = data
        if view == "front" or first_name is None:
            first_name = upload.filename or f"{view}{ext}"

    want_exclusive = _parse_exclusive(exclusive)

    # Fail fast so the dashboard can show the real numbers immediately rather
    # than queueing a job that is going to be refused a moment later. An
    # exclusive request is pre-flighted against the projection; the binding
    # check is still the measured one the engine runs after the release.
    refused = _gate(ptype, gen_id, exclusive=want_exclusive)
    if refused is not None:
        return refused

    existing = jobs.active_job()
    if existing is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "generation_in_progress", "job": existing.as_dict()},
        )

    job = jobs.submit(images, first_name or "reference.png", seed_v, ptype, gen_id, tex,
                      exclusive=want_exclusive)
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
