"""Minimal FastAPI wrapper around TRELLIS.2.

Deliberately small: health, system state, and one synchronous generate call.
The dashboard, job queue, and file management come in a later milestone.
"""
import logging

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from . import config, engine, memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trellis.api")

app = FastAPI(title="3D Generator — TRELLIS.2 backend", version="0.2.0")


@app.on_event("startup")
def _startup() -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "API up. Model=%s pipeline_type=%s min_available_gb=%s (lazy load: model NOT resident yet)",
        config.MODEL_REPO,
        config.DEFAULT_PIPELINE_TYPE,
        config.MIN_AVAILABLE_GB,
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": config.MODEL_REPO.split("/")[-1],
        "gpu_available": engine.gpu_available(),
    }


@app.get("/system")
def system():
    return {
        "memory": {
            "total_gb": memory.total_gb(),
            "used_gb": memory.used_gb(),
            "available_gb": memory.available_gb(),
            "min_required_gb": config.MIN_AVAILABLE_GB,
            "sufficient": memory.available_gb() >= config.MIN_AVAILABLE_GB,
        },
        "gpu": engine.gpu_info(),
        "model": {
            "repo": config.MODEL_REPO,
            "loaded": engine.is_loaded(),
            "load_error": engine.load_error(),
            "default_pipeline_type": config.DEFAULT_PIPELINE_TYPE,
        },
        "generation": {
            "active": engine.active_generation() is not None,
            "current": engine.active_generation(),
        },
    }


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

    data = image.file.read()
    if not data:
        return JSONResponse(status_code=400, content={"error": "empty_image"})

    try:
        result = engine.generate(data, image.filename or "reference.png", seed, ptype)
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
    return result
