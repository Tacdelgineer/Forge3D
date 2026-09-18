"""In-container verification of the Hunyuan3D worker stack on GB10 / sm_121.

Run with:  docker compose --profile hunyuan exec -T hunyuan python - < scripts/verify_hunyuan.py
or:        docker run --rm -v "$PWD/scripts:/scripts" -v "$PWD/models/hunyuan:/models" \
               --runtime nvidia 3d-generator-hunyuan:latest python /scripts/verify_hunyuan.py

Exits non-zero if any required component fails. The point is to catch the two
failure classes this port actually hit: CUDA extensions built without an sm_121
cubin, and the missing linux-aarch64 `bpy` wheel that upstream's OBJ->GLB
conversion depended on.
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

FAIL = []


def check(name, fn, required=True):
    try:
        detail = fn()
        print(f"  [ OK ] {name}: {detail}")
        return True
    except Exception as exc:
        tag = "FAIL" if required else "WARN"
        print(f"  [{tag}] {name}: {type(exc).__name__}: {exc}")
        if os.environ.get("VERIFY_TRACE"):
            traceback.print_exc()
        if required:
            FAIL.append(name)
        return False


def c_torch():
    import torch

    assert torch.cuda.is_available(), "torch.cuda.is_available() is False"
    cap = torch.cuda.get_device_capability(0)
    assert cap == (12, 1), f"expected sm_121, got sm_{cap[0]}{cap[1]}"
    return (
        f"torch {torch.__version__}, cuda {torch.version.cuda}, "
        f"{torch.cuda.get_device_name(0)}, sm_{cap[0]}{cap[1]}"
    )


def c_cuda_exec():
    """Real kernel execution (catches PTX-JIT-only builds)."""
    import torch

    a = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
    b = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
    c = (a @ b).float()
    torch.cuda.synchronize()
    assert torch.isfinite(c).all(), "matmul produced non-finite values"
    return f"fp16 matmul 1024^2 OK, mean={c.mean().item():.4f}"


def c_rasterizer_cubin():
    """The compiled extension must contain an sm_121 cubin, not just PTX.

    This is the exact check that would have caught the torchvision
    cudaErrorNoKernelImageForDevice regression before a generation hit it.
    """
    import custom_rasterizer_kernel

    so = custom_rasterizer_kernel.__file__
    out = subprocess.run(
        ["cuobjdump", "--list-elf", so], capture_output=True, text=True
    ).stdout
    assert "sm_121" in out, f"no sm_121 cubin in {so}:\n{out[:300]}"
    return f"{Path(so).name} contains sm_121"


def c_rasterizer_import():
    import custom_rasterizer as cr

    assert hasattr(cr, "rasterize"), "custom_rasterizer.rasterize missing"
    return "custom_rasterizer imported, rasterize present"


def c_mesh_inpaint():
    """The C++ extension compiled from source this session.

    Upstream reaches it as a submodule of the DifferentiableRenderer package
    (MeshRender.py: `from .mesh_inpaint_processor import meshVerticeInpaint`),
    so that is the import path that actually has to work.
    """
    from DifferentiableRenderer.mesh_inpaint_processor import meshVerticeInpaint

    assert callable(meshVerticeInpaint), "meshVerticeInpaint is not callable"
    return "DifferentiableRenderer.mesh_inpaint_processor.meshVerticeInpaint OK"


def c_pymeshlab():
    """Guards a regression that silently breaks every Hunyuan import.

    pymeshlab bundles Qt and dlopens libharfbuzz/libfontconfig/libfreetype;
    when those are absent it raises ImportError, and since hy3dshape, hy3dgen
    and hy3dpaint all import it transitively, every pipeline import fails.
    """
    import pymeshlab

    return f"pymeshlab {getattr(pymeshlab, '__version__', 'imported')} (Qt libs present)"


def c_quadric_decimation():
    """The textured path's hidden dependency: trimesh -> open3d.

    hy3dpaint's remesh_mesh() calls trimesh.simplify_quadric_decimation(),
    which delegates to open3d. Nothing in Hunyuan's own source imports open3d,
    so grepping the repos suggests it is unused; dropping it instead breaks
    every textured run ~50 s in, once the paint model has already loaded.
    """
    import trimesh

    m = trimesh.creation.icosphere(subdivisions=3)
    target = len(m.faces) // 2
    s = m.simplify_quadric_decimation(target)
    assert len(s.faces) > 0, "decimation produced an empty mesh"
    return f"trimesh -> open3d decimation OK ({len(m.faces)} -> {len(s.faces)} faces)"


def c_hy3dshape():
    from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline  # noqa: F401

    return "hy3dshape (2.1 shape) imports cleanly"


def c_hy3dgen():
    """2.0's shapegen, used for multi-view. texgen is deliberately absent."""
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline  # noqa: F401

    import hy3dgen

    texgen = Path(hy3dgen.__file__).parent / "texgen"
    assert not texgen.exists(), "hy3dgen/texgen should not be installed (rasterizer name clash)"
    return "hy3dgen.shapegen imports cleanly; texgen correctly absent"


def c_mv_processor():
    """The real multi-view API: a dict of named views, not a stitched image.

    MVImageProcessorV2.__call__ takes {view_tag: image}, maps each tag through
    view2idx and sorts by view index, so each view stays a separate
    conditioning image. view2idx is set in __init__, not on the class, so the
    processor has to be instantiated to inspect it.
    """
    from hy3dgen.shapegen.pipelines import Hunyuan3DDiTFlowMatchingPipeline  # noqa: F401
    from hy3dgen.shapegen.preprocessors import IMAGE_PROCESSORS, MVImageProcessorV2

    proc = MVImageProcessorV2()
    idx = proc.view2idx
    for v in ("front", "left", "back", "right"):
        assert v in idx, f"view {v!r} missing from view2idx"
    assert proc.return_view_idx is True, "processor would not return view indices"
    assert IMAGE_PROCESSORS.get("mv_v2") is MVImageProcessorV2
    return f"view2idx={idx}, dict-of-views API confirmed"


def c_hy3dpaint():
    from textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline  # noqa: F401

    conf = Hunyuan3DPaintConfig(max_num_view=6, resolution=512)
    return f"textureGenPipeline imports; default cfg ok ({type(conf).__name__})"


def c_realesrgan_ckpt():
    p = Path("/opt/Hunyuan3D-2.1/hy3dpaint/ckpt/RealESRGAN_x4plus.pth")
    assert p.is_file(), f"missing {p}"
    return f"{p.name} present ({p.stat().st_size/1024**2:.0f} MB)"


def c_no_bpy_needed():
    """bpy has no linux-aarch64 wheel; nothing may hard-require it."""
    import importlib.util

    spec = importlib.util.find_spec("bpy")
    assert spec is None, "bpy is unexpectedly installed - the patch assumed it is absent"
    from DifferentiableRenderer.mesh_utils import convert_obj_to_glb  # noqa: F401

    return "bpy absent and mesh_utils imports without it"


def c_obj_to_glb():
    """Exercise the trimesh replacement for upstream's Blender-based export."""
    from DifferentiableRenderer.mesh_utils import convert_obj_to_glb

    with tempfile.TemporaryDirectory() as d:
        obj = Path(d) / "tri.obj"
        glb = Path(d) / "tri.glb"
        obj.write_text(
            "v 0.0 0.0 0.0\nv 1.0 0.0 0.0\nv 0.0 1.0 0.0\n"
            "vt 0.0 0.0\nvt 1.0 0.0\nvt 0.0 1.0\n"
            "f 1/1 2/2 3/3\n"
        )
        convert_obj_to_glb(str(obj), str(glb))
        assert glb.is_file(), "convert_obj_to_glb produced no file"
        head = glb.read_bytes()[:4]
        assert head == b"glTF", f"not a GLB: {head!r}"
        size = glb.stat().st_size
    return f"OBJ -> GLB via trimesh OK ({size} bytes, magic=glTF)"


def c_background_remover():
    from hy3dshape.rembg import BackgroundRemover  # noqa: F401

    return "BackgroundRemover importable"


def c_worker_module():
    sys.path.insert(0, "/opt/worker")
    import worker

    for route in ("/status", "/generate", "/unload"):
        assert any(getattr(r, "path", None) == route for r in worker.app.routes), \
            f"worker route {route} missing"
    return "worker.py imports; /status /generate /unload registered"


def c_model_cache():
    """Weights live on the bind mount, never inside the image."""
    hf = Path(os.environ.get("HF_HOME", "/models/hf"))
    if not hf.is_dir():
        raise RuntimeError(f"{hf} not mounted")
    repos = sorted(p.name for p in hf.glob("hub/models--*"))
    total = sum(f.stat().st_size for f in hf.rglob("*") if f.is_file() and not f.is_symlink())
    return f"{hf}: {total/1024**3:.1f} GiB, repos={repos or 'none yet'}"


def main():
    print("=" * 72)
    print("Hunyuan3D worker / GB10 stack verification")
    print("=" * 72)

    print("\n[core CUDA]")
    check("torch + sm_121", c_torch)
    check("CUDA kernel execution", c_cuda_exec)

    print("\n[compiled extensions]")
    check("custom_rasterizer sm_121 cubin", c_rasterizer_cubin)
    check("custom_rasterizer import", c_rasterizer_import)
    check("mesh_inpaint_processor (C++)", c_mesh_inpaint)

    print("\n[Hunyuan3D packages]")
    check("pymeshlab (Qt runtime libs)", c_pymeshlab)
    check("quadric decimation (trimesh->open3d)", c_quadric_decimation)
    check("hy3dshape (2.1 shape)", c_hy3dshape)
    check("hy3dgen (2.0 shape, for 2mv)", c_hy3dgen)
    check("multi-view image processor", c_mv_processor)
    check("hy3dpaint (textureGenPipeline)", c_hy3dpaint)
    check("RealESRGAN checkpoint", c_realesrgan_ckpt)
    check("BackgroundRemover", c_background_remover)

    print("\n[ARM64 portability patch]")
    check("no bpy dependency", c_no_bpy_needed)
    check("OBJ -> GLB without Blender", c_obj_to_glb)

    print("\n[worker + storage]")
    check("worker module", c_worker_module)
    check("model cache mount", c_model_cache, required=False)

    print("\n" + "=" * 72)
    if FAIL:
        print(f"RESULT: FAILED -> {', '.join(FAIL)}")
        return 1
    print("RESULT: all required checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
