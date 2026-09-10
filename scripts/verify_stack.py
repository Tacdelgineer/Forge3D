"""In-container verification of the GB10 / sm_121 CUDA + graphics stack.

Run with:  docker compose exec trellis python /app/scripts/verify_stack.py
Exits non-zero if any required component fails.
"""
import ctypes
import os
import sys
import traceback

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
        f"{torch.cuda.get_device_name(0)}, sm_{cap[0]}{cap[1]}, "
        f"arch_list={torch.cuda.get_arch_list()}"
    )


def c_cuda_exec():
    """Real kernel execution on sm_121 (catches PTX JIT failures)."""
    import torch

    a = torch.randn(2048, 2048, device="cuda", dtype=torch.bfloat16)
    b = torch.randn(2048, 2048, device="cuda", dtype=torch.bfloat16)
    c = (a @ b).float()
    torch.cuda.synchronize()
    assert torch.isfinite(c).all(), "matmul produced non-finite values"
    return f"bf16 matmul 2048^2 OK, mean={c.mean().item():.4f}"


def c_flash_attn():
    import flash_attn
    import torch
    from flash_attn import flash_attn_varlen_qkvpacked_func

    n, h, d = 256, 8, 64
    qkv = torch.randn(n, 3, h, d, device="cuda", dtype=torch.bfloat16)
    cu = torch.tensor([0, n], device="cuda", dtype=torch.int32)
    out = flash_attn_varlen_qkvpacked_func(qkv, cu, n, dropout_p=0.0)
    torch.cuda.synchronize()
    assert out.shape == (n, h, d), out.shape
    assert torch.isfinite(out).all(), "flash-attn produced non-finite values"
    return f"flash_attn {flash_attn.__version__}, varlen_qkvpacked OK, out={tuple(out.shape)}"


def c_nvdiffrast_cuda():
    """The path TRELLIS.2 actually uses: RasterizeCudaContext, not GL."""
    import nvdiffrast.torch as dr
    import torch

    ctx = dr.RasterizeCudaContext()
    pos = torch.tensor(
        [[[-0.8, -0.8, 0.0, 1.0], [0.8, -0.8, 0.0, 1.0], [0.0, 0.8, 0.0, 1.0]]],
        device="cuda",
        dtype=torch.float32,
    )
    tri = torch.tensor([[0, 1, 2]], device="cuda", dtype=torch.int32)
    rast, _ = dr.rasterize(ctx, pos, tri, resolution=[256, 256])
    torch.cuda.synchronize()
    covered = int((rast[..., 3] > 0).sum().item())
    assert covered > 0, "rasterized triangle covered 0 pixels"
    return f"RasterizeCudaContext OK, {covered} px covered"


def c_o_voxel():
    import o_voxel

    assert hasattr(o_voxel, "postprocess"), "o_voxel.postprocess missing"
    assert hasattr(o_voxel.postprocess, "to_glb"), "o_voxel.postprocess.to_glb missing"
    return "o_voxel.postprocess.to_glb present"


def c_cumesh():
    import cumesh

    return f"cumesh {getattr(cumesh, '__version__', 'imported')}"


def c_flexgemm():
    # Distribution "flex_gemm"; TRELLIS.2 selects it as SPARSE_CONV_BACKEND=flex_gemm
    import flex_gemm

    return f"flex_gemm {getattr(flex_gemm, '__version__', 'imported')}"


def c_nvdiffrec():
    # nvdiffrec's renderutils branch installs as the module "nvdiffrec_render"
    import nvdiffrec_render

    return "nvdiffrec_render imported"


def c_trellis_import():
    from trellis2.pipelines import Trellis2ImageTo3DPipeline  # noqa: F401

    return "trellis2.pipelines imports cleanly"


# --- EGL --------------------------------------------------------------------
EGL_SUCCESS = 0x3000
EGL_VENDOR = 0x3053
EGL_VERSION = 0x3054
EGL_PLATFORM_DEVICE_EXT = 0x313F
EGL_NO_DISPLAY = ctypes.c_void_p(0)
GL_RENDERER = 0x1F01
GL_VENDOR = 0x1F00
GL_VERSION = 0x1F02
EGL_OPENGL_API = 0x30A2
EGL_PBUFFER_BIT = 0x0001
EGL_SURFACE_TYPE = 0x3033
EGL_WIDTH = 0x3057
EGL_HEIGHT = 0x3056
EGL_NONE = 0x3038


def c_egl():
    """Prove EGL resolves to the NVIDIA ICD and not Mesa/llvmpipe."""
    egl = ctypes.CDLL("libEGL.so.1")

    egl.eglGetProcAddress.restype = ctypes.c_void_p
    egl.eglQueryString.restype = ctypes.c_char_p
    egl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]

    # Enumerate EGL devices (headless, no X server needed).
    addr = egl.eglGetProcAddress(b"eglQueryDevicesEXT")
    if not addr:
        raise RuntimeError("eglQueryDevicesEXT unavailable (no EGL_EXT_device_base)")
    query_devices = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int)
    )(addr)

    max_dev = 16
    devices = (ctypes.c_void_p * max_dev)()
    num = ctypes.c_int(0)
    if not query_devices(max_dev, devices, ctypes.byref(num)):
        raise RuntimeError("eglQueryDevicesEXT failed")
    if num.value == 0:
        raise RuntimeError("no EGL devices found")

    addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
    get_platform_display = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
    )(addr)

    results = []
    for i in range(num.value):
        dpy = get_platform_display(EGL_PLATFORM_DEVICE_EXT, devices[i], None)
        if not dpy:
            continue
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(ctypes.c_void_p(dpy), ctypes.byref(major), ctypes.byref(minor)):
            continue
        vendor = egl.eglQueryString(ctypes.c_void_p(dpy), EGL_VENDOR)
        version = egl.eglQueryString(ctypes.c_void_p(dpy), EGL_VERSION)
        results.append(
            (
                i,
                vendor.decode() if vendor else "?",
                version.decode() if version else "?",
                dpy,
            )
        )

    if not results:
        raise RuntimeError("no EGL display could be initialized")

    nvidia = [r for r in results if "NVIDIA" in r[1].upper()]
    summary = "; ".join(f"device{i}: vendor={v!r} version={ver!r}" for i, v, ver, _ in results)
    if not nvidia:
        raise RuntimeError(f"EGL initialized but no NVIDIA vendor (software fallback?): {summary}")
    return f"{len(results)} device(s); NVIDIA ICD active -> {summary}"


def c_egl_gl_renderer():
    """Bind a GL context through EGL and read GL_RENDERER (llvmpipe detector)."""
    egl = ctypes.CDLL("libEGL.so.1")
    egl.eglGetProcAddress.restype = ctypes.c_void_p
    egl.eglQueryString.restype = ctypes.c_char_p

    addr = egl.eglGetProcAddress(b"eglQueryDevicesEXT")
    query_devices = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int)
    )(addr)
    devices = (ctypes.c_void_p * 16)()
    num = ctypes.c_int(0)
    query_devices(16, devices, ctypes.byref(num))
    addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
    get_platform_display = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
    )(addr)

    for i in range(num.value):
        dpy = get_platform_display(EGL_PLATFORM_DEVICE_EXT, devices[i], None)
        if not dpy:
            continue
        major, minor = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(ctypes.c_void_p(dpy), ctypes.byref(major), ctypes.byref(minor)):
            continue
        vendor = egl.eglQueryString(ctypes.c_void_p(dpy), EGL_VENDOR)
        if not vendor or b"NVIDIA" not in vendor.upper():
            continue

        egl.eglBindAPI.argtypes = [ctypes.c_uint]
        egl.eglChooseConfig.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int, ctypes.POINTER(ctypes.c_int),
        ]
        egl.eglCreateContext.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ]
        egl.eglCreateContext.restype = ctypes.c_void_p
        egl.eglMakeCurrent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]

        egl.eglBindAPI(EGL_OPENGL_API)
        cfg = ctypes.c_void_p()
        ncfg = ctypes.c_int()
        attribs = (ctypes.c_int * 3)(EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_NONE)
        if not egl.eglChooseConfig(
            ctypes.c_void_p(dpy), attribs, ctypes.byref(cfg), 1, ctypes.byref(ncfg)
        ) or ncfg.value == 0:
            raise RuntimeError("eglChooseConfig found no config")

        ctx = egl.eglCreateContext(ctypes.c_void_p(dpy), cfg, None, None)
        if not ctx:
            raise RuntimeError("eglCreateContext failed")
        if not egl.eglMakeCurrent(
            ctypes.c_void_p(dpy), None, None, ctypes.c_void_p(ctx)
        ):
            raise RuntimeError("eglMakeCurrent failed")

        gl = ctypes.CDLL("libGL.so.1")
        gl.glGetString.restype = ctypes.c_char_p
        gl.glGetString.argtypes = [ctypes.c_uint]
        renderer = (gl.glGetString(GL_RENDERER) or b"?").decode()
        gl_vendor = (gl.glGetString(GL_VENDOR) or b"?").decode()
        gl_version = (gl.glGetString(GL_VERSION) or b"?").decode()

        if "llvmpipe" in renderer.lower() or "softpipe" in renderer.lower():
            raise RuntimeError(f"SOFTWARE RENDERING: GL_RENDERER={renderer!r}")
        return f"GL_RENDERER={renderer!r}, GL_VENDOR={gl_vendor!r}, GL_VERSION={gl_version!r}"

    raise RuntimeError("no NVIDIA EGL display available to bind a GL context")


def c_nvdiffrast_gl():
    """nvdiffrast's OpenGL rasterizer end to end — the real 'is GL usable' test.

    TRELLIS.2 does not use this path (it uses RasterizeCudaContext), so this is
    informational: it proves the EGL/graphics plumbing works for anything that
    later wants the GL rasterizer, e.g. app_texturing.py.
    """
    import nvdiffrast.torch as dr
    import torch

    ctx = dr.RasterizeGLContext(output_db=False)
    pos = torch.tensor(
        [[[-0.8, -0.8, 0.0, 1.0], [0.8, -0.8, 0.0, 1.0], [0.0, 0.8, 0.0, 1.0]]],
        device="cuda",
        dtype=torch.float32,
    )
    tri = torch.tensor([[0, 1, 2]], device="cuda", dtype=torch.int32)
    rast, _ = dr.rasterize(ctx, pos, tri, resolution=[256, 256])
    torch.cuda.synchronize()
    covered = int((rast[..., 3] > 0).sum().item())
    assert covered > 0, "GL-rasterized triangle covered 0 pixels"
    return f"RasterizeGLContext OK, {covered} px covered"


def main():
    print("=" * 72)
    print("TRELLIS.2 / GB10 stack verification")
    print("=" * 72)

    print("\n[core CUDA]")
    check("torch + sm_121", c_torch)
    check("CUDA kernel execution", c_cuda_exec)

    print("\n[TRELLIS.2 native extensions]")
    check("flash-attention (varlen)", c_flash_attn)
    check("nvdiffrast (CUDA raster)", c_nvdiffrast_cuda)
    check("nvdiffrec / renderutils", c_nvdiffrec)
    check("CuMesh", c_cumesh)
    check("FlexGEMM", c_flexgemm)
    check("o-voxel", c_o_voxel)
    check("trellis2 import", c_trellis_import)

    print("\n[graphics / EGL]  (informational: TRELLIS.2 uses the CUDA rasterizer)")
    check("EGL NVIDIA ICD", c_egl, required=False)
    check("EGL GL context (llvmpipe check)", c_egl_gl_renderer, required=False)
    check("nvdiffrast RasterizeGLContext", c_nvdiffrast_gl, required=False)

    print("\n" + "=" * 72)
    if FAIL:
        print(f"RESULT: FAILED -> {', '.join(FAIL)}")
        return 1
    print("RESULT: all required checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
