"""Runtime configuration, all overridable through the environment (.env)."""
import os
from pathlib import Path

# ------------------------------------------------------------------ model --
MODEL_ID = "trellis2"
MODEL_NAME = "TRELLIS.2"
MODEL_REPO = os.environ.get("TRELLIS_MODEL_REPO", "microsoft/TRELLIS.2-4B")

# What the UI's model picker lists. One entry today; the next milestone appends
# to it. Deliberately a plain list, not a plugin registry.
MODELS = [{"id": MODEL_ID, "name": MODEL_NAME, "repo": MODEL_REPO}]

# ------------------------------------------------------ pipelines / modes --
# The exact identifiers Trellis2ImageTo3DPipeline.run(pipeline_type=...) accepts
# (trellis2/pipelines/trellis2_image_to_3d.py). '1536_cascade' reuses the same
# three checkpoints as '1024_cascade' and upsamples to 1536; when an object
# exceeds the 49,152-token budget TRELLIS steps the resolution down by 128 until
# it fits (never below 1024).
VALID_PIPELINE_TYPES = ("512", "1024", "1024_cascade", "1536_cascade")
DEFAULT_PIPELINE_TYPE = os.environ.get("TRELLIS_PIPELINE_TYPE", "512")

# What the dashboard offers. '1024' (non-cascade) stays API-only.
UI_MODES = (
    {
        "id": "512",
        "label": "Standard",
        "badge": "Recommended",
        "summary": "512³ geometry · faster · lower memory",
        "time_hint": "~100 s warm",
        "experimental": False,
    },
    {
        "id": "1024_cascade",
        "label": "High Quality",
        "badge": None,
        "summary": "1024³ geometry · more detail · higher memory",
        "time_hint": "~200 s",
        "experimental": False,
    },
    {
        "id": "1536_cascade",
        "label": "Ultra",
        "badge": "Experimental",
        "summary": "Up to 1536³ geometry · highest memory",
        "time_hint": "untested here",
        "experimental": True,
    },
)
MODE_LABELS = {
    "512": "Standard",
    "1024": "1024",
    "1024_cascade": "High Quality",
    "1536_cascade": "Ultra",
}

# ----------------------------------------------------------- memory gate --
# GB10 has one 128 GB pool shared by CPU and GPU. Two rules, both must hold
# (see app/memory.py and docs/step-04-quality-ui.md):
#
#  1. Headroom: MemAvailable + what Forge3D already holds >= MIN_AVAILABLE_GB.
#     Same 65 GiB as before; it now credits a resident model instead of
#     counting it against us.
#  2. Projection: headroom - peak(mode) >= PROTECTED_FLOOR_GB. content-factory
#     fails its own visual jobs below 40 GiB MemAvailable, so a run must never
#     be allowed to take the machine below that.
MIN_AVAILABLE_GB = float(os.environ.get("TRELLIS_MIN_AVAILABLE_GB", "65"))
PROTECTED_FLOOR_GB = float(os.environ.get("TRELLIS_PROTECTED_FLOOR_GB", "40"))

# Peak memory each mode takes out of MemAvailable over a whole run (model load +
# generation + GLB export), relative to Forge3D holding nothing. Rounded up.
# Measured on this host unless MODE_PEAK_MEASURED says otherwise.
MODE_PEAK_GB = {
    "512": 27.0,           # measured: MemAvailable 83.4 -> 56.5 (Step 2)
    "1024_cascade": 44.0,  # measured: 83.4 -> 40.1 (Step 2)
    "1024": 44.0,          # estimate: same token count as 1024_cascade, API-only
    "1536_cascade": 64.0,  # estimate: extrapolated from the two measured modes
}
MODE_PEAK_MEASURED = {"512": True, "1024_cascade": True, "1024": False, "1536_cascade": False}

# ---------------------------------------------------------------- storage --
DATA_DIR = Path(os.environ.get("TRELLIS_DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

# ------------------------------------------------------------- GLB export --
# Texture resolution is the size of the baked PBR maps (base colour and
# metallic-roughness), in pixels. It does not affect geometry.
TEXTURE_SIZES = (1024, 2048, 4096)
GLB_TEXTURE_SIZE = int(os.environ.get("TRELLIS_GLB_TEXTURE_SIZE", "4096"))
GLB_DECIMATION_TARGET = int(os.environ.get("TRELLIS_GLB_DECIMATION_TARGET", "1000000"))

# ------------------------------------------------------------------- seed --
# Same range as TRELLIS.2's own demo (np.iinfo(np.int32).max).
SEED_MAX = 2**31 - 1
