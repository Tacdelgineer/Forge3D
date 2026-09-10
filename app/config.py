"""Runtime configuration, all overridable through the environment (.env)."""
import os
from pathlib import Path

# Model
MODEL_REPO = os.environ.get("TRELLIS_MODEL_REPO", "microsoft/TRELLIS.2-4B")

# Default generation resolution/pipeline. TRELLIS.2 offers
# '512', '1024', '1024_cascade', '1536_cascade'. The model's own default is
# '1024_cascade'; a request may override it per generation.
DEFAULT_PIPELINE_TYPE = os.environ.get("TRELLIS_PIPELINE_TYPE", "1024_cascade")
VALID_PIPELINE_TYPES = ("512", "1024", "1024_cascade", "1536_cascade")

# Memory gate. This box shares one 128 GB pool between CPU and GPU, and the
# existing content-factory worker fails its own jobs below 40 GiB MemAvailable,
# so we refuse to load or generate below our own (higher) threshold rather than
# risking an OOM that would take other services down with us.
MIN_AVAILABLE_GB = float(os.environ.get("TRELLIS_MIN_AVAILABLE_GB", "45"))

# Storage (bind-mounted from the host)
DATA_DIR = Path(os.environ.get("TRELLIS_DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

# GLB export
GLB_TEXTURE_SIZE = int(os.environ.get("TRELLIS_GLB_TEXTURE_SIZE", "4096"))
GLB_DECIMATION_TARGET = int(os.environ.get("TRELLIS_GLB_DECIMATION_TARGET", "1000000"))
