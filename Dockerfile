# TRELLIS.2 on NVIDIA DGX Spark / GB10 (Grace-Blackwell, aarch64, sm_121)
#
# Build strategy notes (see docs/step-02-trellis-backend.md):
#   * GB10 is compute capability 12.1. Stock PyTorch wheels only ship sm_120 cubins,
#     so every CUDA extension we compile must target 12.1 explicitly.
#   * CUDA 12.9 base is used rather than 13.0 because flash-attn 2.7.x does not build
#     cleanly against the CUDA 13 toolchain. A 12.9-compiled binary runs fine on the
#     host's CUDA 13.0 / 580.x driver (backward compatibility).
#   * torchvision is NOT rebuilt from source: TRELLIS.2 only uses torchvision.transforms
#     (CPU). It calls no torchvision CUDA op, so the stock aarch64 wheel is sufficient.
FROM nvcr.io/nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive

ARG MAX_JOBS=4

# --- system packages -------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl wget build-essential \
      cmake ninja-build pkg-config \
      python3 python3-dev python3-venv python3-pip \
      ffmpeg \
      libssl-dev zlib1g-dev libbz2-dev libsqlite3-dev libffi-dev liblzma-dev \
      libjpeg-dev libpng-dev libtiff-dev libopenjp2-7-dev liblcms2-dev \
      libwebp-dev libfreetype6-dev \
      # OpenGL / EGL stack. TRELLIS.2 itself uses nvdiffrast's CUDA rasterizer,
      # but nvdiffrast builds against these headers and the texturing app can
      # use the GL path, so keep a working EGL available.
      libglvnd-dev libgl1-mesa-dev libegl1-mesa-dev libgles2-mesa-dev \
      libglu1-mesa-dev mesa-common-dev \
      libx11-dev libxext-dev libxi-dev libxxf86vm-dev libxrender-dev libxfixes-dev \
      mesa-utils \
    && rm -rf /var/lib/apt/lists/*

# --- NVIDIA EGL ICD --------------------------------------------------------
# NGC cuda:*devel images ship libglvnd but no NVIDIA EGL ICD manifest. Without
# this file, eglInitialize silently falls through to Mesa/llvmpipe (software
# rendering). The nvidia container runtime injects libEGL_nvidia.so.0 at run
# time; this manifest is what makes libglvnd actually select it.
RUN mkdir -p /usr/share/glvnd/egl_vendor.d && \
    printf '%s\n' \
      '{' \
      '    "file_format_version" : "1.0.0",' \
      '    "ICD" : {' \
      '        "library_path" : "libEGL_nvidia.so.0"' \
      '    }' \
      '}' > /usr/share/glvnd/egl_vendor.d/10_nvidia.json

# --- CUDA / build environment ---------------------------------------------
ENV CUDA_HOME=/usr/local/cuda
ENV PATH="${CUDA_HOME}/bin:/opt/venv/bin:${PATH}"
ENV LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}"
# GB10 = sm_121. "+PTX" keeps forward-compatible PTX in the binaries too.
ENV TORCH_CUDA_ARCH_LIST="12.1+PTX"
ENV TRITON_PTXAS_PATH="${CUDA_HOME}/bin/ptxas"

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install --no-cache-dir --upgrade pip setuptools wheel

# --- PyTorch (pinned, aarch64 + cu129) ------------------------------------
RUN pip install --no-cache-dir \
      torch==2.9.1 torchvision \
      --index-url https://download.pytorch.org/whl/cu129

RUN pip install --no-cache-dir packaging ninja psutil "huggingface_hub[hf_xet]"

# Build parallelism. This is a memory-safety control, not just a speed knob:
# each `cicc` instance peaks around 4-6 GB, and this host shares one 128 GB pool
# with Ollama (~33 GB pinned) and ComfyUI. An unconstrained build drove
# MemAvailable to 7.7 GiB, under the content-factory 40 GiB gate.
ENV MAX_JOBS=${MAX_JOBS}
# flash-attn defaults NVCC_THREADS to 2 and multiplies it by MAX_JOBS.
ENV NVCC_THREADS=1

# --- flash-attention (source build) ----------------------------------------
# Required: TRELLIS.2's sparse attention accepts only flash_attn / xformers,
# there is no SDPA fallback on the sparse path. No aarch64 wheel exists.
#
# CRITICAL for this host: flash-attn's setup.py hardcodes four gencode targets
# (sm_80/90/100/120) via cuda_archs() and ignores TORCH_CUDA_ARCH_LIST for them,
# so it compiles every translation unit five times over. That is what pushed
# MemAvailable to 7.7 GiB on the first attempt. FLASH_ATTN_CUDA_ARCHS=121 makes
# all four hardcoded branches miss, leaving only TORCH_CUDA_ARCH_LIST's sm_121.
ENV FLASH_ATTN_CUDA_ARCHS=121
RUN pip install --no-cache-dir --no-build-isolation flash-attn==2.7.4.post1

# --- TRELLIS.2 CUDA extensions --------------------------------------------
RUN mkdir -p /tmp/ext && \
    git clone -b v0.4.0 --depth 1 https://github.com/NVlabs/nvdiffrast.git /tmp/ext/nvdiffrast && \
    git clone -b renderutils --depth 1 https://github.com/JeffreyXiang/nvdiffrec.git /tmp/ext/nvdiffrec && \
    git clone --recursive --depth 1 https://github.com/JeffreyXiang/CuMesh.git /tmp/ext/CuMesh && \
    git clone --recursive --depth 1 https://github.com/JeffreyXiang/FlexGEMM.git /tmp/ext/FlexGEMM

RUN pip install --no-cache-dir --no-build-isolation /tmp/ext/nvdiffrast
RUN pip install --no-cache-dir --no-build-isolation /tmp/ext/nvdiffrec
RUN pip install --no-cache-dir --no-build-isolation /tmp/ext/CuMesh
RUN pip install --no-cache-dir --no-build-isolation /tmp/ext/FlexGEMM

# --- TRELLIS.2 source (pinned) --------------------------------------------
ARG TRELLIS_COMMIT=75fbf0183001ed9876c8dbb35de6b68552ee08bd
# --recursive matters: o-voxel/src includes <Eigen/Dense>, and Eigen is a git
# submodule (o-voxel/third_party/eigen) that o-voxel's setup.py adds to
# include_dirs. A plain clone leaves it empty and the build fails with
# "fatal error: Eigen/Dense: No such file or directory".
RUN git clone https://github.com/microsoft/TRELLIS.2.git /opt/TRELLIS.2 && \
    cd /opt/TRELLIS.2 && \
    git checkout ${TRELLIS_COMMIT} && \
    git submodule update --init --recursive && \
    rm -rf .git

RUN pip install --no-cache-dir --no-build-isolation /opt/TRELLIS.2/o-voxel

# --- TRELLIS.2 python dependencies ----------------------------------------
# Mirrors setup.sh --basic, minus gradio/tensorboard (we serve our own API)
# and minus pillow-simd (no aarch64 build; stock Pillow is used instead).
# transformers is PINNED. TRELLIS.2's DinoV3FeatureExtractor reaches into the
# model internals (`for layer_module in self.model.layer`), which only matches
# the DINOv3ViTModel layout up to transformers 4.57.x. From 4.58 the module was
# restructured (the ModuleList moved under `.model`, a DINOv3ViTEncoder), and an
# unpinned install picks up 5.x and fails at inference with
# "AttributeError: 'DINOv3ViTModel' object has no attribute 'layer'".
RUN pip install --no-cache-dir \
      imageio imageio-ffmpeg tqdm easydict opencv-python-headless \
      trimesh transformers==4.57.1 pandas lpips zstandard \
      Pillow kornia timm==1.0.12 && \
    pip install --no-cache-dir \
      "git+https://github.com/EasternJournalist/utils3d.git@9a4eb15e4021b67b12c460c7057d642626897ec8"

# --- our API layer ---------------------------------------------------------
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart

# --- torchvision rebuilt for sm_121 ----------------------------------------
# The stock aarch64 wheel contains cubins for sm_50..sm_90 and NO PTX, so its
# custom CUDA ops cannot run or JIT on GB10 (sm_121). The background-removal
# path (briaai/RMBG-2.0 -> birefnet.py -> torchvision.ops.deform_conv2d) hits
# exactly that and fails with cudaErrorNoKernelImageForDevice.
#
# Built here, late in the file, so the expensive flash-attn / nvdiffrast /
# CuMesh / FlexGEMM / o-voxel layers above stay cached. Everything that depends
# on torchvision (timm, lpips, transformers) is installed above this point, so
# nothing reinstalls the wheel afterwards.
#
# Version must track torch: torch 2.9.1 <-> torchvision 0.24.1.
ARG TORCHVISION_VERSION=0.24.1
# torchvision 0.24.1's setup.py imports pkg_resources, which setuptools removed
# in 81. Pin it back for the build; nothing pip-installs after this layer.
RUN git clone --depth 1 --branch "v${TORCHVISION_VERSION}" \
        https://github.com/pytorch/vision.git /tmp/vision && \
    cd /tmp/vision && \
    pip install --no-cache-dir "setuptools<81" && \
    pip uninstall -y torchvision && \
    FORCE_CUDA=1 TORCH_CUDA_ARCH_LIST="12.1+PTX" \
        pip install --no-cache-dir --no-build-isolation . && \
    cd / && rm -rf /tmp/vision

# Fail the build rather than ship a torchvision that cannot run on this GPU.
RUN python - <<'EOF'
import subprocess, sys, torchvision, pathlib
so = pathlib.Path(torchvision.__file__).parent / "_C.so"
out = subprocess.run(["cuobjdump", "--list-elf", str(so)],
                     capture_output=True, text=True).stdout
print("torchvision", torchvision.__version__, "->", so)
print(out.strip()[:400])
if "sm_121" not in out:
    sys.exit("FATAL: torchvision/_C.so has no sm_121 cubin")
print("OK: torchvision _C.so contains sm_121")
EOF

# --- dashboard third-party JS (vendored, not CDN) --------------------------
# three.js is fetched at build time and served from our own /static so the
# dashboard works with no outbound internet access from the browser. The
# examples/jsm layout is preserved because GLTFLoader imports
# '../utils/BufferGeometryUtils.js' relatively.
ARG THREE_VERSION=0.169.0
RUN set -eux; \
    base="https://cdn.jsdelivr.net/npm/three@${THREE_VERSION}"; \
    mkdir -p /app/app/static/vendor/three/build \
             /app/app/static/vendor/three/examples/jsm/loaders \
             /app/app/static/vendor/three/examples/jsm/controls \
             /app/app/static/vendor/three/examples/jsm/utils \
             /app/app/static/vendor/three/examples/jsm/environments; \
    curl -fsSL "$base/build/three.module.min.js"                        -o /app/app/static/vendor/three/build/three.module.min.js; \
    curl -fsSL "$base/examples/jsm/loaders/GLTFLoader.js"               -o /app/app/static/vendor/three/examples/jsm/loaders/GLTFLoader.js; \
    curl -fsSL "$base/examples/jsm/controls/OrbitControls.js"           -o /app/app/static/vendor/three/examples/jsm/controls/OrbitControls.js; \
    curl -fsSL "$base/examples/jsm/utils/BufferGeometryUtils.js"        -o /app/app/static/vendor/three/examples/jsm/utils/BufferGeometryUtils.js; \
    curl -fsSL "$base/examples/jsm/environments/RoomEnvironment.js"     -o /app/app/static/vendor/three/examples/jsm/environments/RoomEnvironment.js

ENV PYTHONPATH=/opt/TRELLIS.2
ENV PYTHONUNBUFFERED=1
ENV OPENCV_IO_ENABLE_OPENEXR=1
ENV PYTORCH_ALLOC_CONF=expandable_segments:True
ENV ATTN_BACKEND=flash_attn
# Caches live on the ./models bind mount so rebuilds never re-download weights
# and nvdiffrast's JIT-compiled extension survives container recreation.
ENV HOME=/app/models
ENV HF_HOME=/app/models/hf
ENV TORCH_HOME=/app/models/torch
ENV TORCH_EXTENSIONS_DIR=/app/models/torch_extensions
ENV TRITON_CACHE_DIR=/app/models/triton

WORKDIR /app

# App code last: editing it must not invalidate any build layer above.
COPY app /app/app
COPY scripts/verify_stack.py /app/scripts/verify_stack.py

EXPOSE 8189
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8189"]
