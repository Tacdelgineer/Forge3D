# Third-party software and models

Audited against Forge3D's Dockerfiles and the working GB10 images on 2026-09-17.
This records the current integrations, not a complete transitive dependency SBOM.
Upstream agreements govern their own code, weights and derivatives regardless of
any future license for Forge3D's original code.

## Forge3D source-license decision

There is currently **no root source-code license**. The API, UI, worker wrapper,
resource helper and compatibility scripts appear to be project-authored, but
`step-01-dgx-audit.md` explicitly identifies
[dr-vij/Trellis2-DGX-Spark-Docker](https://github.com/dr-vij/Trellis2-DGX-Spark-Docker)
as the seed build recipe. That repository has no visible license file or declared
GitHub license at audit time. A public repository alone does not grant permission
to relicense copied content. Maintainer confirmation of what was reused and its
permission/provenance is needed before licensing the Docker recipe.

**Proposal, not an applied license:** MIT for confirmed original Forge3D code,
with explicit third-party exclusions, after provenance and owner approval. No
blanket license was added. Until resolved, describe Forge3D as source-available,
not as an entirely MIT-licensed or unrestricted open-source stack.

## Generation backends and separately downloaded weights

| Project / model | What Forge3D uses | Relevant terms | Weight delivery |
| --- | --- | --- | --- |
| [Microsoft TRELLIS.2](https://github.com/microsoft/TRELLIS.2) | Image-to-3D pipeline and o-voxel GLB export; source pinned to `75fbf0183001ed9876c8dbb35de6b68552ee08bd` | [MIT source license](https://github.com/microsoft/TRELLIS.2/blob/75fbf0183001ed9876c8dbb35de6b68552ee08bd/LICENSE); [TRELLIS.2-4B model card](https://huggingface.co/microsoft/TRELLIS.2-4B) declares MIT for its main weights. Preserve Microsoft's notice. Dependent models/libraries have separate terms. | Main weights downloaded at runtime into `models/hf`; not committed or bundled with this repository. |
| [Hunyuan3D 2.1](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1) | `hy3dshape`, `hy3dpaint`, compiled rasterizer and mesh painter; source pinned to `82920d643c0dc2f7bfd7255f45f62d386edfe60c` | [Tencent Hunyuan 3D 2.1 Community License](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE), including acceptable-use policy; [local agreement copy](../hunyuan/licenses/Hunyuan3D-2.1.txt). **Not a permissive open-source license.** | [tencent/Hunyuan3D-2.1](https://huggingface.co/tencent/Hunyuan3D-2.1) downloaded at runtime into `models/hunyuan`. |
| [Hunyuan3D Multi-View / Hunyuan3D-2](https://github.com/Tencent-Hunyuan/Hunyuan3D-2) | `hy3dgen.shapegen` from source pinned to `f8db63096c8282cb27354314d896feba5ba6ff8a`, with `tencent/Hunyuan3D-2mv` checkpoints. The 2.0 texgen code is removed from the image. | [Tencent Hunyuan 3D 2.0 Community License](https://github.com/Tencent-Hunyuan/Hunyuan3D-2/blob/main/LICENSE); [weight agreement](https://huggingface.co/tencent/Hunyuan3D-2mv/blob/main/LICENSE); [local agreement copy](../hunyuan/licenses/Hunyuan3D-2.0.txt). Multi-View texturing uses **2.1 paint**, so both backend agreements apply. | [tencent/Hunyuan3D-2mv](https://huggingface.co/tencent/Hunyuan3D-2mv) downloaded at runtime into `models/hunyuan`. |
| [Meta DINOv3 ViT-L/16](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m) | TRELLIS image conditioner | [Custom DINOv3 License](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m/blob/main/LICENSE.md), with redistribution, trade-control and use conditions; not automatically covered by TRELLIS's MIT license. Account approval required. | Separate gated runtime download into `models/hf`. |
| [BRIA RMBG-2.0](https://huggingface.co/briaai/RMBG-2.0) | TRELLIS background remover; instantiated even when an input already has alpha | Model card specifies **CC BY-NC 4.0** for non-commercial use; commercial use requires a separate BRIA agreement. Accept gated access terms. | Separate gated runtime download into `models/hf`. |

### Hunyuan restrictions and redistribution

Both community agreements exclude the **EU, UK and South Korea** and include an
acceptable-use policy. Their additional commercial clause requires Tencent's
separate permission when the licensee's products/services exceeded **1 million
monthly active users in the preceding month on the relevant model release date**.
The agreements also prohibit using these works/outputs to improve other AI models
outside the permitted Hunyuan derivatives. Review the actual agreements for the
full conditions rather than treating this summary as a commercial-use grant.

Redistribution requires agreement copies, retained notices and prominent notices
on modified files. Non-hosted distributions require a `Notice` text file. The
repository supplies [NOTICE](../hunyuan/licenses/NOTICE) and agreement copies,
and the Hunyuan Dockerfile includes them in the built worker image. Cloned source
also retains its upstream license/copyright files.

The current agreements additionally require prominent identification of the
**actual provider's full legal name/entity** and a disclaimer of Tencent
association when providing integrated functionality to third parties. Forge3D
is an independent project; Tencent is not affiliated with, associated with,
sponsoring or endorsing Forge3D. **Maintainer action:** supply the provider's
correct legal identity before publishing an integrated release or service; a
GitHub handle is not assumed to satisfy that requirement. Territory restrictions
still apply; downloading weights separately does not waive product/service terms.

## Other significant installed components

| Upstream | Usage | License / distribution note |
| --- | --- | --- |
| [NVlabs/nvdiffrast v0.4.0](https://github.com/NVlabs/nvdiffrast/tree/v0.4.0) | CUDA/GL rasterization in the TRELLIS stack | [NVIDIA Source Code License (1-Way Commercial)](https://github.com/NVlabs/nvdiffrast/blob/v0.4.0/LICENSE.txt). Despite its name, clause 3.3 limits third-party use to **non-commercial research/evaluation**, excluding direct/indirect monetary gain. Preserve the full license and notices. |
| [JeffreyXiang/nvdiffrec, renderutils branch](https://github.com/JeffreyXiang/nvdiffrec/tree/renderutils) | Installed rendering utilities and stack checks | [NVIDIA Source Code License](https://github.com/JeffreyXiang/nvdiffrec/blob/renderutils/LICENSE.txt); clause 3.3 limits third-party use to **non-commercial research/evaluation**. Preserve license and notices. |
| [CuMesh](https://github.com/JeffreyXiang/CuMesh), [FlexGEMM](https://github.com/JeffreyXiang/FlexGEMM), [utils3d](https://github.com/EasternJournalist/utils3d/tree/9a4eb15e4021b67b12c460c7057d642626897ec8) | Mesh processing, sparse kernels and 3D utilities | MIT; retain each project's copyright/license notices. Sources are fetched during the TRELLIS build. |
| [Three.js r169](https://github.com/mrdoob/three.js/tree/r169) | Vendored browser GLB loader, orbit controls, utilities and viewer | [MIT](https://github.com/mrdoob/three.js/blob/r169/LICENSE). Modules and the license are fetched into the TRELLIS image's `app/static/vendor/three/`; vendored files are ignored in Git. |
| [PyTorch](https://github.com/pytorch/pytorch), [torchvision](https://github.com/pytorch/vision), [flash-attention](https://github.com/Dao-AILab/flash-attention) | Inference runtime; TRELLIS torchvision rebuilt for sm_121; flash-attn 2.7.4.post1 source build | BSD-3-Clause primary project licenses, plus bundled third-party notices. Retain packaged notices when distributing images. |
| [PyMeshLab](https://github.com/cnr-isti-vclab/PyMeshLab) | Hunyuan mesh processing | [GPL](https://github.com/cnr-isti-vclab/PyMeshLab/blob/main/LICENSE); installed package metadata says GPL3. Binary/image redistribution requires a separate copyleft/source-compliance review, not merely an MIT notice for Forge3D. |
| [xatlas-python](https://github.com/mworchel/xatlas-python), [xatlas](https://github.com/jpcy/xatlas), [trimesh](https://github.com/mikedh/trimesh), [Open3D](https://github.com/isl-org/Open3D) | UV atlas, mesh loading/export and Hunyuan remesh/decimation | MIT primary licenses; retain notices. Other libraries bundled with these projects keep their own terms. |
| [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | Hunyuan paint upscaler, including `RealESRGAN_x4plus.pth` | [BSD-3-Clause](https://github.com/xinntao/Real-ESRGAN/blob/master/LICENSE). This small checkpoint is downloaded into the **Hunyuan image at build time**; unlike the main weights, it is part of that image. Not committed to Git. |
| [rembg](https://github.com/danielgatis/rembg), [U-2-Net](https://github.com/xuebinqin/U-2-Net) | Hunyuan image background removal | rembg MIT; [U-2-Net Apache-2.0](https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE). Default background-removal weights download on demand to `models/hunyuan/u2net`. |
| [NVIDIA CUDA container images](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/cuda) | CUDA 12.9.1/cuDNN base images and toolchain | NVIDIA component/container terms apply independently; retain image-provided license/EULA files and review NGC terms before image redistribution. |

FastAPI/Three.js/trimesh use MIT; uvicorn uses BSD-3-Clause; transformers,
diffusers and huggingface-hub use Apache-2.0. Their dependencies retain their own
licenses. This document does not clear every transitive dependency for binary or
commercial redistribution.

**Commercial-use limitation:** TRELLIS's MIT code/main weights do not override
RMBG's non-commercial terms or the installed NVIDIA rendering libraries' use
restrictions. Do not advertise this whole stack as cleared for commercial asset
generation. Resolve applicable permissions before commercial use/distribution.

## Copied and patched source

Full TRELLIS/Tencent source trees are fetched into Docker images, not vendored in
this Git repository. They retain their upstream notices. Forge3D contains a small
patch script and build-time edits, rather than a relicensed copy of those trees:

- `hunyuan/patches/patch_no_bpy.py` changes 2.1's
  `hy3dpaint/DifferentiableRenderer/mesh_utils.py` to make bpy optional and use
  trimesh for OBJ→GLB conversion. The patched upstream file carries a prominent
  Forge3D modification notice and remains subject to its upstream terms.
- `hunyuan/Dockerfile` edits 2.0's `hy3dgen/__init__.py` to omit texgen, retaining
  its upstream header and adding a Forge3D modification notice.
- TRELLIS's torchvision rebuild changes compilation targets, not upstream source
  licensing. The original build-recipe provenance issue is recorded above.

No model weights are committed. This cleanup publishes source/setup documentation,
not prebuilt images. Before distributing images, audit all bundled code, checkpoints,
license/NOTICE files and GPL source obligations separately.
