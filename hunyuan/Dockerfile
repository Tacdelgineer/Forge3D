# Hunyuan3D worker for NVIDIA DGX Spark / GB10 (Grace-Blackwell, aarch64, sm_121)
#
# Deliberately a SEPARATE image from the TRELLIS one. Hunyuan3D-2.1 pins
# transformers==4.46.0 / diffusers==0.30.0, while TRELLIS.2 needs
# transformers==4.57.1 — they cannot share an environment, and the known-good
# TRELLIS CUDA stack must not be disturbed.
#
# One worker serves BOTH Hunyuan generators:
#   * Hunyuan3D-2.1   (hy3dshape + hy3dpaint)  — single image -> shape (+PBR)
#   * Hunyuan3D-2mv   (hy3dgen from the 2.0 repo) — 1-4 views -> shape
# 2.0's texgen is deliberately NOT installed: it declares the same
# distribution name ("custom_rasterizer") as 2.1's, so the two would collide.
# 2.1's paint pipeline textures an arbitrary mesh, so 2mv shapes are textured
# by 2.1 instead.
#
# Ubuntu 22.04 base: it ships Python 3.10, which is what Hunyuan3D's pins
# target. That avoids building CPython from source.
FROM nvcr.io/nvidia/cuda:12.9.1-cudnn-devel-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.10 python3.10-dev python3.10-venv python3-pip \
      git curl wget ca-certificates build-essential cmake ninja-build pkg-config \
      libgl1 libglx0 libopengl0 libglu1-mesa libglib2.0-0 libgomp1 \
      libxrender1 libxext6 libsm6 libx11-6 libxi6 libxfixes3 libxxf86vm1 \
    && rm -rf /var/lib/apt/lists/*
# pymeshlab and opencv load Qt/GL libraries even headless.
ENV QT_QPA_PLATFORM=offscreen

RUN python3.10 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# --- PyTorch (pinned, aarch64 + cu129, cp310) -------------------------------
# Same CUDA minor as the TRELLIS image, which is proven on this host's 580.x
# CUDA 13.0 driver (minor-version forward compatibility).
RUN pip install --no-cache-dir torch==2.9.1 torchvision \
      --index-url https://download.pytorch.org/whl/cu129

# --- Hunyuan3D dependencies -------------------------------------------------
# Hunyuan3D-2.1's requirements.txt with the ARM64 substitutions it needs:
#   bpy          dropped   — no linux-aarch64 wheels at all. Only used by
#                            convert_obj_to_glb (OBJ->GLB), which is patched to
#                            trimesh below, so Blender is not needed.
#   pymeshlab    unpinned  — the pinned 2022.2.post3 has no aarch64 wheel; the
#                            filter names this code uses are the post-2022.2
#                            names and still current.
#   xatlas       unpinned  — built from sdist, no aarch64 wheel published.
#   open3d       dropped   — in requirements but imported nowhere in either repo.
#   cupy-cuda12x dropped   — likewise imported nowhere.
#   gradio/tb_nightly/deepspeed dropped — demo/training only; this worker
#                            serves its own small FastAPI instead.
RUN pip install --no-cache-dir \
      ninja==1.11.1.1 pybind11==2.13.4 \
      transformers==4.46.0 diffusers==0.30.0 accelerate==1.1.1 \
      pytorch-lightning==1.9.5 huggingface-hub==0.30.2 safetensors==0.4.4 \
      "huggingface_hub[hf_xet]==0.30.2" \
      numpy==1.24.4 scipy==1.14.1 einops==0.8.0 pandas==2.2.2 \
      opencv-python==4.10.0.84 imageio==2.36.0 scikit-image==0.24.0 \
      rembg==2.0.65 onnxruntime==1.16.3 \
      realesrgan==0.3.0 basicsr==1.4.2 torchmetrics==1.6.0 timm torchdiffeq \
      trimesh==4.4.7 pygltflib==1.16.3 pymeshlab xatlas \
      omegaconf==2.3.0 pyyaml==6.0.2 configargparse==1.7 \
      tqdm==4.66.5 psutil==6.0.0 Pillow \
      fastapi "uvicorn[standard]" python-multipart

# --- source checkouts (pinned) ---------------------------------------------
ARG HY21_COMMIT=82920d643c0dc2f7bfd7255f45f62d386edfe60c
ARG HY20_COMMIT=f8db63096c8282cb27354314d896feba5ba6ff8a
RUN git clone https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git /opt/Hunyuan3D-2.1 && \
    cd /opt/Hunyuan3D-2.1 && git checkout ${HY21_COMMIT} && rm -rf .git assets
# Only hy3dgen (shape) is taken from the 2.0 repo — see the note above about
# the custom_rasterizer collision.
RUN git clone https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git /opt/Hunyuan3D-2 && \
    cd /opt/Hunyuan3D-2 && git checkout ${HY20_COMMIT} && \
    rm -rf .git assets hy3dgen/texgen && \
    sed -i 's/^from \.texgen import .*$/# texgen not installed (see Dockerfile)/' hy3dgen/__init__.py || true

# --- ARM64 patch: remove the hard bpy dependency ---------------------------
COPY patches/patch_no_bpy.py /tmp/patch_no_bpy.py
RUN python /tmp/patch_no_bpy.py /opt/Hunyuan3D-2.1/hy3dpaint/DifferentiableRenderer/mesh_utils.py

# --- CUDA / C++ extensions for sm_121 --------------------------------------
# Memory-safety controls, not speed knobs: each cicc peaks at several GiB and
# this host shares one 128 GB pool with Ollama and the TRELLIS container.
ARG MAX_JOBS=4
ENV MAX_JOBS=${MAX_JOBS}
ENV NVCC_THREADS=1
ENV CUDA_HOME=/usr/local/cuda
ENV PATH="${CUDA_HOME}/bin:${PATH}"
ENV LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}"
ENV TORCH_CUDA_ARCH_LIST="12.1+PTX"

RUN cd /opt/Hunyuan3D-2.1/hy3dpaint/custom_rasterizer && \
    pip install --no-cache-dir --no-build-isolation .
# compile_mesh_painter.sh invokes the UNVERSIONED `python3-config`, which is
# shipped by python3-dev; python3.10-dev provides python3.10-config only. Give
# it the name it asks for instead of patching upstream's script, and assert the
# extension suffix matches the venv interpreter that has to import the .so.
RUN ln -sf "$(command -v python3.10-config)" /usr/local/bin/python3-config && \
    test "$(python3-config --extension-suffix)" = \
         "$(python -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
RUN cd /opt/Hunyuan3D-2.1/hy3dpaint/DifferentiableRenderer && \
    bash compile_mesh_painter.sh && ls -1 *.so

# Fail the build rather than ship an extension that cannot run on this GPU.
RUN python - <<'EOF'
import subprocess, sys, glob, importlib
so = glob.glob('/opt/venv/lib/python3.10/site-packages/custom_rasterizer_kernel*.so')
assert so, "custom_rasterizer_kernel not installed"
out = subprocess.run(["cuobjdump", "--list-elf", so[0]], capture_output=True, text=True).stdout
print(so[0]); print(out.strip()[:300])
if "sm_121" not in out:
    sys.exit("FATAL: custom_rasterizer_kernel has no sm_121 cubin")
print("OK: custom_rasterizer_kernel contains sm_121")
EOF

# --- RealESRGAN weights (small, needed by the paint pipeline) --------------
RUN mkdir -p /opt/Hunyuan3D-2.1/hy3dpaint/ckpt && \
    wget -q https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
      -O /opt/Hunyuan3D-2.1/hy3dpaint/ckpt/RealESRGAN_x4plus.pth && \
    ls -la /opt/Hunyuan3D-2.1/hy3dpaint/ckpt/

# --- runtime shared libraries ----------------------------------------------
# pymeshlab ships its own Qt build and dlopens text-shaping/font libraries that
# the CUDA base image does not carry. Without these, importing pymeshlab fails
# with "libharfbuzz.so.0: cannot open shared object file", and because
# hy3dshape/hy3dgen/hy3dpaint all import it transitively, EVERY Hunyuan
# pipeline import fails. Deliberately a late layer so the expensive pip,
# clone and CUDA-compile layers above stay cached.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libharfbuzz0b libfontconfig1 libfreetype6 \
    && rm -rf /var/lib/apt/lists/* \
    && python -c "import pymeshlab; pymeshlab.MeshSet(); print('pymeshlab imports and constructs OK')"

# basicsr/realesrgan (the paint pipeline's upscaler) still import pkg_resources,
# which setuptools 81 removed. Without this the shape pipeline works but every
# textured run dies with ModuleNotFoundError: No module named 'pkg_resources'.
RUN pip install --no-cache-dir "setuptools<81" \
    && python -c "import pkg_resources; print('pkg_resources OK')"

# hy3dpaint's remesh step calls trimesh.simplify_quadric_decimation(), and
# trimesh delegates that to open3d. open3d is imported NOWHERE in Hunyuan's own
# source -- the import lives inside trimesh -- so a source grep makes it look
# unused. It is not: without it every textured run dies ~50 s in, after the
# paint model has already loaded. 0.18.0 is the newest release with a
# linux-aarch64 wheel (manylinux_2_27, cp310).
RUN pip install --no-cache-dir "open3d==0.18.0" \
    && python -c "import trimesh; m=trimesh.creation.icosphere(subdivisions=3); s=m.simplify_quadric_decimation(len(m.faces)//2); print('open3d quadric decimation OK:', len(m.faces), '->', len(s.faces))"

# --- worker ----------------------------------------------------------------
# 2.1's modules import each other by bare name and its configs use paths
# relative to the repo root, so the repo root is both CWD and on sys.path.
ENV PYTHONPATH=/opt/worker:/opt/Hunyuan3D-2.1:/opt/Hunyuan3D-2.1/hy3dshape:/opt/Hunyuan3D-2.1/hy3dpaint:/opt/Hunyuan3D-2
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/models/hf
ENV TORCH_HOME=/models/torch
ENV PYTORCH_ALLOC_CONF=expandable_segments:True

# The worker runs as the invoking user (compose `user:`), whose HOME is "/" and
# is not writable. Anything that caches under HOME then fails at import time:
# numba cannot place its JIT cache for pymatting's kdtree ("no locator
# available"), and pooch cannot create /.u2net for the background-removal
# weights. Point every cache somewhere writable. u2net goes on the model bind
# mount so it is fetched once rather than per container.
ENV HOME=/tmp
ENV NUMBA_CACHE_DIR=/tmp/numba
ENV MPLCONFIGDIR=/tmp/mpl
ENV U2NET_HOME=/models/u2net

COPY worker.py /opt/worker/worker.py

WORKDIR /opt/Hunyuan3D-2.1
EXPOSE 8190
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8190"]
