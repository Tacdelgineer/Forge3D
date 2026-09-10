"""Filesystem-backed asset library.

The generation directories under data/ are the source of truth — there is no
database. Each asset is one generation id, with:

    data/uploads/<id>/reference.<ext>     the reference image
    data/outputs/<id>/model.glb           the generated mesh
    data/outputs/<id>/metadata.json       everything else, including the
                                          user-facing name once renamed

Renaming only ever touches the "name" key in metadata.json; the id and the
directory names stay fixed so nothing that points at them can break.
"""
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from . import config

# Generation ids are minted by engine.py as "<YYYYmmdd>-<HHMMSS>-<8 hex>".
# Anything that does not match exactly is rejected before it ever reaches the
# filesystem — this is the primary defence against path traversal.
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")

MODE_LABELS = {
    "512": "Standard",
    "1024": "1024",
    "1024_cascade": "High Quality",
    "1536_cascade": "1536 Cascade",
}

THUMBNAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class BadAssetId(ValueError):
    """The id is not a well-formed generation id."""


class AssetNotFound(LookupError):
    """No such asset on disk."""


def _resolved_dir(base: Path, asset_id: str) -> Path:
    """Validate the id, then confirm the resolved path is really under base.

    Two independent checks on purpose: the regex rejects anything exotic, and
    the containment check catches symlinks or anything else that might resolve
    outside the data directory.
    """
    if not isinstance(asset_id, str) or not ID_RE.match(asset_id):
        raise BadAssetId(f"invalid asset id: {asset_id!r}")
    base = base.resolve()
    candidate = (base / asset_id).resolve()
    if candidate != base and base not in candidate.parents:
        raise BadAssetId(f"path escapes the data directory: {asset_id!r}")
    return candidate


def output_dir(asset_id: str) -> Path:
    return _resolved_dir(config.OUTPUT_DIR, asset_id)


def upload_dir(asset_id: str) -> Path:
    return _resolved_dir(config.UPLOAD_DIR, asset_id)


def glb_path(asset_id: str) -> Path:
    p = output_dir(asset_id) / "model.glb"
    if not p.is_file():
        raise AssetNotFound(asset_id)
    return p


def thumbnail_path(asset_id: str) -> Path | None:
    d = upload_dir(asset_id)
    if not d.is_dir():
        return None
    for suffix in THUMBNAIL_SUFFIXES:
        p = d / f"reference{suffix}"
        if p.is_file():
            return p
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in THUMBNAIL_SUFFIXES:
            return p
    return None


def _metadata_path(asset_id: str) -> Path:
    return output_dir(asset_id) / "metadata.json"


def _read_metadata(asset_id: str) -> dict:
    p = _metadata_path(asset_id)
    if not p.is_file():
        raise AssetNotFound(asset_id)
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        raise AssetNotFound(asset_id)


def _write_metadata(asset_id: str, meta: dict) -> None:
    """Atomic replace so a crash mid-write cannot truncate the metadata."""
    target = _metadata_path(asset_id)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(meta, fh, indent=2)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def default_name(meta: dict, asset_id: str) -> str:
    src = meta.get("source_filename") or ""
    stem = Path(src).stem.strip()
    return stem or asset_id


def _summarise(asset_id: str, meta: dict) -> dict:
    glb = output_dir(asset_id) / "model.glb"
    size = glb.stat().st_size if glb.is_file() else 0
    mode = meta.get("pipeline_type") or "512"
    validation = meta.get("validation") or {}
    created = meta.get("created_at") or ""
    if not created:
        try:
            created = datetime.utcfromtimestamp(glb.stat().st_mtime).isoformat() + "+00:00"
        except OSError:
            created = ""
    return {
        "id": asset_id,
        "name": meta.get("name") or default_name(meta, asset_id),
        "created_at": created,
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "glb_bytes": size,
        "glb_mb": round(size / 1024**2, 2),
        "vertices": validation.get("vertices"),
        "faces": validation.get("faces"),
        "has_texture": validation.get("has_texture"),
        "seed": meta.get("seed"),
        "source_filename": meta.get("source_filename"),
        "has_thumbnail": thumbnail_path(asset_id) is not None,
        "timings_s": meta.get("timings_s") or {},
    }


def list_assets() -> list[dict]:
    """Newest first. Directories without a finished GLB are skipped.

    A failed generation can leave an output directory with no model.glb (that
    is exactly what the DINOv3 failure left behind in Step 2), so the presence
    of a readable GLB *and* metadata is what makes something an asset.
    """
    base = config.OUTPUT_DIR
    if not base.is_dir():
        return []
    out = []
    for entry in base.iterdir():
        if not entry.is_dir() or not ID_RE.match(entry.name):
            continue
        try:
            meta = _read_metadata(entry.name)
        except (AssetNotFound, BadAssetId):
            continue
        if not (entry / "model.glb").is_file():
            continue
        out.append(_summarise(entry.name, meta))
    out.sort(key=lambda a: a["created_at"], reverse=True)
    return out


def get_asset(asset_id: str) -> dict:
    return _summarise(asset_id, _read_metadata(asset_id))


def get_metadata(asset_id: str) -> dict:
    return _read_metadata(asset_id)


MAX_NAME = 120


def rename_asset(asset_id: str, name: str) -> dict:
    """Change only the user-facing name. Id and directories are untouched."""
    meta = _read_metadata(asset_id)
    clean = " ".join((name or "").split())[:MAX_NAME]
    if not clean:
        raise ValueError("name must not be empty")
    meta["name"] = clean
    _write_metadata(asset_id, meta)
    return _summarise(asset_id, meta)


def delete_asset(asset_id: str) -> dict:
    """Remove this generation's output and its reference upload — nothing else.

    Both directories are keyed by the same generation id, so they are the one
    asset. Every other generation is left alone.
    """
    out = output_dir(asset_id)
    up = upload_dir(asset_id)
    if not out.is_dir() and not up.is_dir():
        raise AssetNotFound(asset_id)
    removed = []
    for d in (out, up):
        if d.is_dir():
            shutil.rmtree(d)
            removed.append(str(d))
    return {"id": asset_id, "deleted": True, "removed": removed}
