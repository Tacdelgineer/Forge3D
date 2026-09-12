"""Render genuine per-view images of a GLB, for multi-view generation input.

Hunyuan3D-2mv takes one real image per named view (front/back/left/right) and
consumes them as separate conditioning images. This produces those images
honestly: four independent textured rasterisations of a real mesh from four
camera azimuths. Nothing here stitches images into a grid.

Runs inside the TRELLIS container, which already has torch, nvdiffrast and
trimesh; it only reads the GLB and writes PNGs, and changes nothing about that
environment.

    docker exec -i 3d-generator-trellis python - \
        < scripts/render_views.py -- --glb /app/data/outputs/<id>/model.glb \
                                     --out /app/data/views/<name>
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np
import torch

# Camera azimuths, in degrees, for each named view. 0 faces the mesh's -Z side;
# the model only needs the four to be mutually consistent and correctly
# labelled, which these are.
VIEW_AZIM = {"front": 0.0, "right": 90.0, "back": 180.0, "left": 270.0}


def look_at(eye, target, up=(0.0, 1.0, 0.0)):
    f = target - eye
    f = f / np.linalg.norm(f)
    up = np.asarray(up, dtype=np.float64)
    s = np.cross(f, up)
    n = np.linalg.norm(s)
    if n < 1e-8:  # camera straight above/below: pick another up vector
        up = np.array([0.0, 0.0, 1.0])
        s = np.cross(f, up)
        n = np.linalg.norm(s)
    s = s / n
    u = np.cross(s, f)
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[:3, 3] = -m[:3, :3] @ eye
    return m


def perspective(fovy_deg, aspect, near, far):
    t = 1.0 / math.tan(math.radians(fovy_deg) / 2.0)
    m = np.zeros((4, 4))
    m[0, 0] = t / aspect
    m[1, 1] = t
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def load_mesh(glb_path: Path):
    """Flatten a GLB to one mesh with UVs and a base-colour texture."""
    import trimesh
    from PIL import Image

    scene = trimesh.load(str(glb_path), process=False, force="scene")
    geoms = [g for g in scene.geometry.values() if hasattr(g, "faces")]
    if not geoms:
        raise SystemExit(f"no mesh geometry in {glb_path}")

    verts, faces, uvs, offset = [], [], [], 0
    texture = None
    for name, geom in scene.geometry.items():
        if not hasattr(geom, "faces"):
            continue
        # Apply the node transform so multi-node scenes compose correctly.
        try:
            transform = scene.graph.get(geometry_name=name)[0]
            g = geom.copy()
            g.apply_transform(transform)
        except Exception:
            g = geom
        verts.append(np.asarray(g.vertices, dtype=np.float32))
        faces.append(np.asarray(g.faces, dtype=np.int32) + offset)
        offset += len(g.vertices)

        visual = getattr(g, "visual", None)
        uv = getattr(visual, "uv", None)
        uvs.append(
            np.asarray(uv, dtype=np.float32)
            if uv is not None and len(uv) == len(g.vertices)
            else np.zeros((len(g.vertices), 2), dtype=np.float32)
        )
        if texture is None:
            mat = getattr(visual, "material", None)
            img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
            if img is not None:
                texture = img.convert("RGB") if isinstance(img, Image.Image) else None

    return (
        np.concatenate(verts),
        np.concatenate(faces),
        np.concatenate(uvs),
        texture,
    )


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--resolution", type=int, default=768)
    ap.add_argument("--elev", type=float, default=10.0, help="camera elevation, degrees")
    ap.add_argument("--fov", type=float, default=30.0)
    ap.add_argument("--dist", type=float, default=None,
                    help="camera distance; default fits the whole mesh in frame")
    ap.add_argument("--views", default="front,back,left,right")
    ap.add_argument("--bg", default="white", choices=("white", "transparent"))
    args = ap.parse_args(argv)

    import nvdiffrast.torch as dr
    from PIL import Image

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    verts, faces, uvs, texture = load_mesh(Path(args.glb))
    print(f"mesh: {len(verts)} verts, {len(faces)} faces, "
          f"texture={'yes ' + str(texture.size) if texture else 'no (flat shading)'}")

    # Normalise into a unit sphere at the origin so one camera distance works
    # for any mesh.
    centre = (verts.max(0) + verts.min(0)) / 2.0
    verts = verts - centre
    radius = float(np.linalg.norm(verts, axis=1).max())
    verts = verts / radius

    dev = torch.device("cuda")
    v = torch.tensor(verts, dtype=torch.float32, device=dev)
    f = torch.tensor(faces, dtype=torch.int32, device=dev)
    vt = torch.tensor(uvs, dtype=torch.float32, device=dev)
    # glTF UVs have their origin at the top-left; nvdiffrast samples with v
    # increasing upwards, so flip once here rather than per-view.
    vt = torch.stack([vt[:, 0], 1.0 - vt[:, 1]], dim=1)

    if texture is not None:
        tex = torch.tensor(np.asarray(texture, dtype=np.float32) / 255.0, device=dev)[None]
    else:
        tex = None

    # TRELLIS.2 uses the CUDA rasteriser on this host; no GL context needed.
    ctx = dr.RasterizeCudaContext()
    res = args.resolution
    # The mesh is normalised into the unit sphere, which is fully inside the
    # frustum only when the camera is at least 1/sin(fov/2) away. Closer than
    # that silently crops the subject at the frame edge, and a cropped view is
    # exactly what the multi-view model should not be fed.
    fit = 1.0 / math.sin(math.radians(args.fov) / 2.0)
    dist = args.dist if args.dist is not None else fit * 1.06

    written = []
    for view in [x.strip() for x in args.views.split(",") if x.strip()]:
        if view not in VIEW_AZIM:
            raise SystemExit(f"unknown view {view!r}; choose from {list(VIEW_AZIM)}")
        azim = math.radians(VIEW_AZIM[view])
        elev = math.radians(args.elev)
        eye = np.array([
            dist * math.cos(elev) * math.sin(azim),
            dist * math.sin(elev),
            dist * math.cos(elev) * math.cos(azim),
        ])
        mvp = perspective(args.fov, 1.0, 0.1, 100.0) @ look_at(eye, np.zeros(3))
        mvp_t = torch.tensor(mvp, dtype=torch.float32, device=dev)

        homog = torch.cat([v, torch.ones(len(v), 1, device=dev)], dim=1)
        clip = (homog @ mvp_t.T)[None]

        rast, _ = dr.rasterize(ctx, clip, f, resolution=[res, res])
        mask = (rast[..., 3:4] > 0).float()

        if tex is not None:
            uv_i, _ = dr.interpolate(vt[None], rast, f)
            colour = dr.texture(tex, uv_i, filter_mode="linear")
        else:
            # No texture: shade by normal so the views still carry real shape.
            tri = v[f.long()]
            n = torch.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0], dim=1)
            n = torch.nn.functional.normalize(n, dim=1)
            face_id = (rast[..., 3].long() - 1).clamp(min=0)
            shade = (n[face_id] * 0.5 + 0.5)
            colour = shade

        colour = colour * mask + (1.0 - mask) * (1.0 if args.bg == "white" else 0.0)
        img = (colour[0].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
        img = np.flipud(img)  # nvdiffrast's origin is bottom-left

        if args.bg == "transparent":
            alpha = (np.flipud(mask[0, ..., 0].cpu().numpy()) * 255).astype(np.uint8)
            pil = Image.fromarray(np.dstack([img, alpha]), mode="RGBA")
        else:
            pil = Image.fromarray(img, mode="RGB")

        path = out_dir / f"{view}.png"
        pil.save(path)
        covered = float(mask.mean().item())
        written.append((view, path, covered))
        print(f"  {view:6s} azim={VIEW_AZIM[view]:5.1f}  coverage={covered*100:5.1f}%  -> {path}")

    empty = [v for v, _, c in written if c < 0.01]
    if empty:
        raise SystemExit(f"views rendered essentially empty: {empty}")
    print(f"wrote {len(written)} views to {out_dir}")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--" in argv:                      # tolerate `python - -- --glb ...`
        argv = argv[argv.index("--") + 1:]
    sys.exit(main(argv))
