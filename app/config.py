"""Runtime configuration, all overridable through the environment (.env)."""
import os
from pathlib import Path

# Model
MODEL_REPO = os.environ.get("TRELLIS_MODEL_REPO", "microsoft/TRELLIS.2-4B")

# Default generation resolution/pipeline. TRELLIS.2 offers
# '512', '1024', '1024_cascade', '1536_cascade'.
#
# Forge3D defaults to '512' ("Standard"): ~100 s warm generation and a measured
# floor of ~56.5 GiB MemAvailable. TRELLIS.2's own default is '1024_cascade'
# ("High Quality"), which is still fully supported per request but takes ~200 s
# and drew down to ~40.1 GiB - level with content-factory's own 40 GiB gate.
# Both modes produce identical PBR / 4K-texture output.
DEFAULT_PIPELINE_TYPE = os.environ.get("TRELLIS_PIPELINE_TYPE", "512")
VALID_PIPELINE_TYPES = ("512", "1024", "1024_cascade", "1536_cascade")

# Memory gate. This box shares one 128 GB pool between CPU and GPU, and the
# existing content-factory worker fails its own jobs below 40 GiB MemAvailable,
# so we refuse to load or generate below our own (higher) threshold rather than
# risking an OOM that would take other services down with us.
#
# 65 GiB, not 45: this check is PRE-FLIGHT only, and a generation keeps drawing
# after it passes - measured drawdown is ~11 GiB for '512' and ~17.5 GiB for
# '1024_cascade'. Starting at 65 GiB keeps even a 1024_cascade run above the
# content-factory 40 GiB threshold for its whole duration.
MIN_AVAILABLE_GB = float(os.environ.get("TRELLIS_MIN_AVAILABLE_GB", "65"))

# Storage (bind-mounted from the host)
DATA_DIR = Path(os.environ.get("TRELLIS_DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

# GLB export
GLB_TEXTURE_SIZE = int(os.environ.get("TRELLIS_GLB_TEXTURE_SIZE", "4096"))
GLB_DECIMATION_TARGET = int(os.environ.get("TRELLIS_GLB_DECIMATION_TARGET", "1000000"))
