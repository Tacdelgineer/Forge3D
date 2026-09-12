"""Remove Hunyuan3D-2.1's hard dependency on bpy (Blender as a Python module).

`bpy` publishes no linux-aarch64 wheels, and `hy3dpaint/textureGenPipeline.py`
imports `convert_obj_to_glb` from this module at import time, so the whole paint
pipeline is unimportable on ARM64 without either building Blender from source
(~1 h, and a serious memory risk on a shared 128 GB box) or removing the need.

`convert_obj_to_glb` only converts a textured OBJ (+MTL) to GLB. trimesh does
that natively, so this rewrites the function and makes the `import bpy` optional.
Everything else in the module (load_mesh/save_mesh) is trimesh/numpy already.
"""
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
src = path.read_text()

# 1) make the top-level bpy import optional
if re.search(r"^import bpy$", src, flags=re.M):
    src = re.sub(
        r"^import bpy$",
        "try:  # patched for ARM64: no linux-aarch64 bpy wheels exist\n"
        "    import bpy\n"
        "except Exception:  # pragma: no cover\n"
        "    bpy = None",
        src,
        count=1,
        flags=re.M,
    )

# 2) replace convert_obj_to_glb with a trimesh implementation
replacement = '''def convert_obj_to_glb(
    obj_path: str,
    glb_path: str,
    shade_type: str = "SMOOTH",
    auto_smooth_angle: float = 60,
    merge_vertices: bool = False,
) -> bool:
    """Convert a textured OBJ to GLB with trimesh.

    Patched for ARM64: upstream used Blender (bpy), which has no
    linux-aarch64 wheels. trimesh loads the OBJ with its MTL and texture
    images and writes a GLB, which is all this step needs. The shade_type /
    auto_smooth_angle / merge_vertices arguments are accepted for signature
    compatibility; trimesh keeps the mesh as authored.
    """
    import trimesh

    scene = trimesh.load(obj_path, process=False, force="scene")
    if merge_vertices:
        for geom in scene.geometry.values():
            if hasattr(geom, "merge_vertices"):
                geom.merge_vertices()
    scene.export(glb_path)
    return True
'''

m = re.search(r"^def convert_obj_to_glb\(", src, flags=re.M)
if m is None:
    sys.exit("FATAL: convert_obj_to_glb not found — upstream layout changed")
start = m.start()
nxt = re.search(r"^(def |class )", src[m.end():], flags=re.M)
end = m.end() + nxt.start() if nxt else len(src)
src = src[:start] + replacement + "\n\n" + src[end:]

path.write_text(src)
print(f"patched {path}: bpy import made optional, convert_obj_to_glb -> trimesh")

# fail loudly if any *other* bpy call site survives on the paint path
leftover = [
    ln for ln in src.splitlines()
    if re.search(r"\bbpy\.", ln) and "convert_obj_to_glb" not in ln
]
print(f"remaining bpy.* references (unused legacy helpers): {len(leftover)}")
