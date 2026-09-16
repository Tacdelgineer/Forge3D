"""Runtime configuration, all overridable through the environment (.env)."""
import os
from pathlib import Path

# ------------------------------------------------------------------ models --
MODEL_ID = "trellis"
MODEL_NAME = "TRELLIS.2"
MODEL_REPO = os.environ.get("TRELLIS_MODEL_REPO", "microsoft/TRELLIS.2-4B")

# ------------------------------------------------------ pipelines / modes --
# The exact identifiers Trellis2ImageTo3DPipeline.run(pipeline_type=...) accepts
# (trellis2/pipelines/trellis2_image_to_3d.py). '1536_cascade' reuses the same
# three checkpoints as '1024_cascade' and upsamples to 1536; when an object
# exceeds the 49,152-token budget TRELLIS steps the resolution down by 128 until
# it fits (never below 1024).
VALID_PIPELINE_TYPES = ("512", "1024", "1024_cascade", "1536_cascade")
DEFAULT_PIPELINE_TYPE = os.environ.get("TRELLIS_PIPELINE_TYPE", "512")

TRELLIS_MODES = (
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
        "time_hint": "~210 s",
        "experimental": True,
    },
)

# Hunyuan's quality axis is "shape only" vs "shape plus baked PBR texture" —
# nothing like TRELLIS's geometry resolutions, so the two are kept separate
# rather than forced into one shared vocabulary.
HUNYUAN_MODES = (
    {
        "id": "shape",
        "label": "Shape",
        "badge": "Recommended",
        "summary": "Geometry only · untextured mesh",
        "time_hint": "~60-120 s",
        "experimental": False,
    },
    {
        "id": "shape_texture",
        "label": "Shape + PBR texture",
        "badge": None,
        "summary": "Geometry then baked PBR texture · much higher memory",
        "time_hint": "~4-6 min",
        "experimental": False,
    },
)

MODE_LABELS = {
    "512": "Standard",
    "1024": "1024",
    "1024_cascade": "High Quality",
    "1536_cascade": "Ultra",
    "shape": "Shape",
    "shape_texture": "Shape + PBR texture",
}

# ------------------------------------------------------------- generators --
# A plain list, not a plugin framework: three entries, each naming its inputs,
# its modes and its upstream licence. `inputs` is "single" (one reference
# image) or "views" (up to four named views).
UPSTREAM = {
    "trellis": {
        "repo": "microsoft/TRELLIS.2",
        "weights": MODEL_REPO,
        "license": "MIT (code); model weights per Microsoft's model card",
        "license_url": "https://github.com/microsoft/TRELLIS.2/blob/main/LICENSE",
        "restricted": False,
        "license_note": "",
    },
    "hunyuan21": {
        "repo": "Tencent-Hunyuan/Hunyuan3D-2.1",
        "weights": "tencent/Hunyuan3D-2.1",
        "license": "Tencent Hunyuan 3D 2.1 Community License",
        "license_url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE",
        "restricted": True,
        "license_note": (
            "Not licensed in the European Union, the United Kingdom or South Korea. "
            "Separate licence required above 1 million monthly active users. "
            "Acceptable Use Policy applies. Weights are downloaded at runtime and "
            "never redistributed by Forge3D."
        ),
    },
    "hunyuan2mv": {
        "repo": "Tencent-Hunyuan/Hunyuan3D-2 (hy3dgen) + tencent/Hunyuan3D-2mv weights",
        "weights": "tencent/Hunyuan3D-2mv",
        "license": "Tencent Hunyuan 3D 2.0 Community License",
        "license_url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2/blob/main/LICENSE",
        "restricted": True,
        "license_note": (
            "Not licensed in the European Union, the United Kingdom or South Korea. "
            "Separate licence required above 1 million monthly active users. "
            "Acceptable Use Policy applies. Weights are downloaded at runtime and "
            "never redistributed by Forge3D."
        ),
    },
}

VIEW_SLOTS = ("front", "back", "left", "right")

GENERATORS = (
    {
        "id": "trellis",
        "name": "TRELLIS.2",
        "backend": "local",
        "inputs": "single",
        "modes": TRELLIS_MODES,
        "default_mode": DEFAULT_PIPELINE_TYPE,
        "texture_control": True,
        "blurb": "Microsoft TRELLIS.2 · 4 B · PBR up to 4K",
    },
    {
        "id": "hunyuan21",
        "name": "Hunyuan3D 2.1",
        "backend": "hunyuan",
        "inputs": "single",
        "modes": HUNYUAN_MODES,
        "default_mode": "shape",
        "texture_control": False,
        "blurb": "Tencent Hunyuan3D 2.1 · 3.3 B shape + 2 B paint",
    },
    {
        "id": "hunyuan2mv",
        "name": "Hunyuan3D Multi-View",
        "backend": "hunyuan",
        "inputs": "views",
        "views": VIEW_SLOTS,
        "required_views": ("front",),
        "modes": HUNYUAN_MODES,
        "default_mode": "shape",
        "texture_control": False,
        "blurb": "Hunyuan3D-2mv · 1-4 views (front required)",
    },
)
DEFAULT_GENERATOR = os.environ.get("FORGE3D_GENERATOR", "trellis")
GENERATOR_IDS = tuple(g["id"] for g in GENERATORS)


def generator(gen_id: str) -> dict | None:
    for g in GENERATORS:
        if g["id"] == gen_id:
            return g
    return None


def valid_modes(gen_id: str) -> tuple[str, ...]:
    g = generator(gen_id)
    if not g:
        return ()
    if gen_id == "trellis":
        return VALID_PIPELINE_TYPES          # '1024' stays API-only
    return tuple(m["id"] for m in g["modes"])


# ----------------------------------------------------------- memory gate --
# GB10 has one 128 GB pool shared by CPU and GPU. Two rules, both must hold
# (see app/memory.py and docs/step-04-quality-ui.md):
#
#  1. Headroom: MemAvailable + what Forge3D (and, for Hunyuan, its worker)
#     already holds >= MIN_AVAILABLE_GB.
#  2. Projection: headroom - peak(generator, mode) >= PROTECTED_FLOOR_GB.
#     content-factory fails its own visual jobs below 40 GiB MemAvailable, so a
#     run must never be allowed to take the machine below that.
MIN_AVAILABLE_GB = float(os.environ.get("TRELLIS_MIN_AVAILABLE_GB", "65"))
PROTECTED_FLOOR_GB = float(os.environ.get("TRELLIS_PROTECTED_FLOOR_GB", "40"))

# Peak memory a whole run takes out of MemAvailable (model load + generate +
# export), relative to the backend holding nothing. Rounded up.
# TRELLIS figures are measured on this host; Hunyuan figures are estimates
# derived from upstream's stated VRAM plus its weight sizes, and are replaced
# by measurements as runs happen. See docs/step-05-multi-model.md.
MODE_PEAK_GB = {
    ("trellis", "512"): 27.0,            # measured: 26.9 and 26.65
    ("trellis", "1024_cascade"): 44.0,   # measured: 43.3
    ("trellis", "1024"): 44.0,           # estimate, API-only
    # Measured 2026-09-16, the first Ultra run on this host (Step 6): a whole
    # run drew 30.55 GiB, against the 64.0 GiB that had only ever been a guess.
    # NOT set to 31: that is one image. Peaks scale with mesh complexity, and
    # 1024_cascade's own stored 44.0 came from a 43.3 GiB run on a heavier image
    # than the 32.6 GiB this same reference produced - a third more, from the
    # image alone. 48.0 keeps Ultra above 1024_cascade (it cannot draw less in
    # general; measuring lower here is export-phase noise) and leaves ~17 GiB
    # over the one measurement for image variance. Revisit with more runs.
    ("trellis", "1536_cascade"): 48.0,   # measured 30.55 + margin
    # Hunyuan figures are raised above upstream's stated VRAM because the first
    # measured runs showed the earlier estimates were optimistic (Step 5):
    #   2mv/shape took MemAvailable 51.4 -> 36.05 GiB, a 15.3 GiB drop, and that
    #   was with a shape model already resident in the worker; a cold run also
    #   pays the weight load. GPU reserved peaked at 13.2 GiB.
    #   2.1/shape peaked at 11.5 GiB GPU reserved and left the worker holding
    #   9.5 GiB.
    # These are deliberately conservative: under-estimating here is what lets a
    # run breach the protected floor.
    # The textured figures were ESTIMATED from upstream's stated 29 GB and were
    # badly wrong. A measured 2.1 shape_texture run took MemAvailable from
    # 78.9 GiB to 24.8 GiB - a 54 GiB draw against a 36 GiB estimate - and
    # breached the 40 GiB protected floor while the pre-flight gate was
    # projecting 43.2 GiB. The paint stage loads a second ~6.4 GiB model on top
    # of the shape pipeline and holds both, plus multiview diffusion at 512 per
    # view and a RealESRGAN upscale.
    #
    # Consequence, stated plainly: with Ollama resident (~33 GiB) this host
    # cannot offer the ~96 GiB of headroom a textured run now requires, so the
    # textured modes show as unavailable in the UI. That is the correct
    # outcome - the alternative is starving content-factory - and it is what
    # the measurement supports, not a number chosen to make the mode fit.
    ("hunyuan21", "shape"): 22.0,
    ("hunyuan21", "shape_texture"): 56.0,
    ("hunyuan2mv", "shape"): 24.0,
    # Same paint pipeline, on the denser multi-view mesh.
    ("hunyuan2mv", "shape_texture"): 58.0,
}
MODE_PEAK_MEASURED = {
    ("trellis", "512"): True,
    ("trellis", "1024_cascade"): True,
    ("trellis", "1536_cascade"): True,
    # Measured the hard way: this run breached the protected floor.
    ("hunyuan21", "shape_texture"): True,
}


def peak_gb(gen_id: str, mode: str) -> float:
    key = (gen_id, mode)
    if key in MODE_PEAK_GB:
        return MODE_PEAK_GB[key]
    # Unknown combination: assume the worst we know about.
    return max(MODE_PEAK_GB.values())


def peak_measured(gen_id: str, mode: str) -> bool:
    return MODE_PEAK_MEASURED.get((gen_id, mode), False)


# ---------------------------------------------------------------- storage --
DATA_DIR = Path(os.environ.get("TRELLIS_DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

# ------------------------------------------------------------- GLB export --
# Texture resolution is the size of the baked PBR maps (base colour and
# metallic-roughness), in pixels. It does not affect geometry. TRELLIS only.
TEXTURE_SIZES = (1024, 2048, 4096)
GLB_TEXTURE_SIZE = int(os.environ.get("TRELLIS_GLB_TEXTURE_SIZE", "4096"))
GLB_DECIMATION_TARGET = int(os.environ.get("TRELLIS_GLB_DECIMATION_TARGET", "1000000"))

# ------------------------------------------------------------------- seed --
# Same range as TRELLIS.2's own demo (np.iinfo(np.int32).max).
SEED_MAX = 2**31 - 1
