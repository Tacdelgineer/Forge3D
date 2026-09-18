"""Step 5 checks: the generator registry, the per-generator memory gate, the
multi-view request contract, and the asset library's handling of pre-Step-5
metadata.

Run with:  docker compose exec -T trellis python - < scripts/test_step5.py

Every HTTP case here is deliberately one that is *rejected* before a job is
queued, so running this never starts a real generation or touches the GPU.
"""
import io
import json
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "/app")

from app import assets, config, memory  # noqa: E402

BASE = "http://127.0.0.1:8189"
FAIL = []
PASSED = 0


def check(name, fn):
    global PASSED
    try:
        detail = fn()
        print(f"  [ OK ] {name}: {detail}")
        PASSED += 1
    except Exception as exc:
        print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
        FAIL.append(name)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def png_bytes(size=(64, 64), colour=(200, 120, 60)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, blob in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
            f'filename="{k}.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + blob + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post_job(fields: dict, files: dict) -> tuple[int, dict]:
    body, ctype = multipart(fields, files)
    req = urllib.request.Request(f"{BASE}/jobs", data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw[:200]}


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as r:
        return json.load(r)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def t_registry():
    assert config.GENERATOR_IDS == ("trellis", "hunyuan21", "hunyuan2mv"), config.GENERATOR_IDS
    for g in config.GENERATORS:
        assert g["modes"], f"{g['id']} has no modes"
        assert g["default_mode"] in config.valid_modes(g["id"]), g["id"]
        assert g["inputs"] in ("single", "views"), g["inputs"]
    mv = config.generator("hunyuan2mv")
    assert mv["inputs"] == "views"
    assert tuple(mv["views"]) == config.VIEW_SLOTS
    assert tuple(mv["required_views"]) == ("front",)
    return f"{len(config.GENERATORS)} generators, multi-view requires {mv['required_views']}"


def t_peak_table():
    """Every advertised (generator, mode) needs its own preflight estimate."""
    missing = [
        (g["id"], m["id"])
        for g in config.GENERATORS
        for m in g["modes"]
        if (g["id"], m["id"]) not in config.MODE_PEAK_GB
    ]
    assert not missing, f"no peak estimate for {missing}"
    # An unknown pair must fall back to the worst known peak, never to zero.
    assert config.peak_gb("nope", "nope") == max(config.MODE_PEAK_GB.values())
    assert config.peak_measured("hunyuan21", "shape") is False
    assert config.peak_measured("trellis", "512") is True
    return f"{len(config.MODE_PEAK_GB)} entries, unknown -> {config.peak_gb('nope','nope')} GiB"


def t_mode_isolation():
    """A generator must not accept another generator's modes."""
    assert "512" not in config.valid_modes("hunyuan21")
    assert "shape" not in config.valid_modes("trellis")
    assert "1024" in config.valid_modes("trellis"), "API-only 1024 should stay valid"
    return "TRELLIS and Hunyuan mode vocabularies are disjoint"


# --------------------------------------------------------------------------- #
# memory gate
# --------------------------------------------------------------------------- #
def t_gate_floor():
    """A run that would breach the protected floor is refused."""
    floor = config.PROTECTED_FLOOR_GB
    peak = config.peak_gb("trellis", "512")
    tight = floor + peak - 5.0          # 5 GiB short of what it needs
    a = memory.assess("512", "trellis", available=tight, footprint=0.0, extra_footprint=0.0)
    assert a["available"] is False, a
    assert a["projected_min_gb"] < floor, a
    assert a["shortfall_gb"] > 0, a
    assert str(floor and int(floor)) in a["reason"] or "floor" in a["reason"], a["reason"]
    return f"refused at {tight} GiB: projected {a['projected_min_gb']} < floor {floor}"


def t_gate_credits_resident_model():
    """A resident model is reusable memory, not a reason to refuse."""
    bare = memory.assess("512", "trellis", available=30.0, footprint=0.0, extra_footprint=0.0)
    with_model = memory.assess("512", "trellis", available=30.0, footprint=40.0, extra_footprint=0.0)
    assert bare["available"] is False, bare
    assert with_model["available"] is True, with_model
    return "same 30 GiB free: refused bare, allowed with a 40 GiB resident model"


def t_gate_credits_worker():
    """The Hunyuan worker's own footprint counts toward its headroom."""
    without = memory.assess("shape", "hunyuan21", available=30.0, footprint=0.0, extra_footprint=0.0)
    with_worker = memory.assess("shape", "hunyuan21", available=30.0, footprint=0.0, extra_footprint=40.0)
    assert without["available"] is False
    assert with_worker["available"] is True
    assert with_worker["backend_footprint_gb"] == 40.0
    return "worker footprint credited to headroom"


def t_gate_no_cross_backend_credit():
    """A Hunyuan run must NOT be credited Forge3D's own resident model.

    Regression test for a real Step 5 bug: TRELLIS keeps holding its model
    while the Hunyuan worker allocates on top of it, so counting it as
    reusable headroom let a multi-view run take MemAvailable down to 36.0 GiB
    against a 40 GiB protected floor.
    """
    # 30 GiB free, with 40 GiB already held by a resident TRELLIS model.
    tre = memory.assess("512", "trellis", available=30.0, footprint=40.0, extra_footprint=0.0)
    hun = memory.assess("shape", "hunyuan21", available=30.0, footprint=40.0, extra_footprint=0.0)
    assert tre["available"] is True, tre
    assert hun["available"] is False, hun
    assert tre["footprint_credited_gb"] == 40.0, tre
    assert hun["footprint_credited_gb"] == 0.0, hun
    return "resident TRELLIS model credited to a TRELLIS run only, never to Hunyuan"


def t_gate_per_generator():
    """Cheaper generators stay available where expensive ones are refused.

    Two independent rules gate a run, and this isolates the second one: the
    headroom must clear MIN_AVAILABLE_GB *and* the projection must stay above
    the floor. Picking a value that only cleared floor+peak would be refused by
    the headroom rule for every generator, proving nothing about peaks.
    """
    cheap_peak = config.peak_gb("hunyuan2mv", "shape")
    avail = max(config.MIN_AVAILABLE_GB, config.PROTECTED_FLOOR_GB + cheap_peak) + 1.0
    cheap = memory.assess("shape", "hunyuan2mv", available=avail, footprint=0.0, extra_footprint=0.0)
    dear = memory.assess("1024_cascade", "trellis", available=avail, footprint=0.0, extra_footprint=0.0)
    assert cheap["available"] is True, cheap
    assert dear["available"] is False, dear
    assert dear["projected_min_gb"] < config.PROTECTED_FLOOR_GB, dear
    return (f"at {avail} GiB: 2mv/shape (peak {cheap_peak}) allowed, "
            f"TRELLIS/1024_cascade (peak {dear['peak_gb']}) refused")


def t_insufficient_payload():
    try:
        memory.require_mode("1536_cascade", "trellis")
    except memory.InsufficientMemory as exc:
        d = exc.as_dict()
        for k in ("error", "generator", "mode", "available_gb", "required_gb",
                  "projected_min_gb", "floor_gb", "peak_gb", "reason"):
            assert k in d, f"missing {k}"
        assert d["error"] == "insufficient_memory"
        return f"payload complete; reason starts {d['reason'][:40]!r}"
    return "Ultra currently fits - gate payload not exercised"


# --------------------------------------------------------------------------- #
# HTTP contract (all rejected before queueing)
# --------------------------------------------------------------------------- #
def t_system_shape():
    s = get("/system")
    assert s["default_generator"] in config.GENERATOR_IDS
    ids = [g["id"] for g in s["generators"]]
    assert ids == list(config.GENERATOR_IDS), ids
    for g in s["generators"]:
        assert "backend_ok" in g and "upstream" in g
        assert "license" in g["upstream"] and "license_url" in g["upstream"]
        for m in g["modes"]:
            for k in ("available", "peak_gb", "peak_measured", "projected_min_gb"):
                assert k in m, f"{g['id']}/{m['id']} missing {k}"
    assert "modes" in s and "models" in s, "Step 4 fields must stay for older clients"
    return f"generators={ids}, legacy fields present"


def t_unknown_generator():
    code, body = post_job({"generator": "stable-diffusion", "mode": "512"}, {"image": png_bytes()})
    assert code == 400, (code, body)
    assert body["error"] == "invalid_generator", body
    return f"{code} {body['error']}"


def t_cross_generator_mode():
    """TRELLIS must reject a Hunyuan mode and vice versa."""
    c1, b1 = post_job({"generator": "trellis", "mode": "shape"}, {"image": png_bytes()})
    c2, b2 = post_job({"generator": "hunyuan21", "mode": "512"}, {"image": png_bytes()})
    assert c1 == 400 and b1["error"] == "invalid_pipeline_type", (c1, b1)
    assert c2 == 400 and b2["error"] == "invalid_pipeline_type", (c2, b2)
    return "both rejected with invalid_pipeline_type"


def t_multiview_requires_front():
    code, body = post_job({"generator": "hunyuan2mv", "mode": "shape"},
                          {"back": png_bytes(), "left": png_bytes()})
    assert code == 400, (code, body)
    assert body["error"] == "view_required", body
    assert body["missing"] == ["front"], body
    return f"{code} view_required missing={body['missing']}"


def t_multiview_rejects_bad_image():
    code, body = post_job({"generator": "hunyuan2mv", "mode": "shape"},
                          {"front": b"this is not an image"})
    assert code == 400, (code, body)
    assert body["error"] == "unsupported_image", body
    return f"{code} {body['error']} (validated by decoding, not by filename)"


def t_no_image():
    code, body = post_job({"generator": "trellis", "mode": "512"}, {})
    assert code == 400 and body["error"] == "image_required", (code, body)
    return f"{code} {body['error']}"


def t_hunyuan_backend_state():
    """When the worker is down, say so - do not report it as a memory problem."""
    s = get("/system")
    hy = [g for g in s["generators"] if g["backend"] == "hunyuan"]
    up = all(g["backend_ok"] for g in hy)
    if not up:
        for g in hy:
            assert g["backend_reason"], f"{g['id']} offline without a reason"
            assert "docker compose" in g["backend_reason"], g["backend_reason"]
            for m in g["modes"]:
                assert m["available"] is False
        return "worker down: reported as backend_unavailable with a fix hint"
    code, body = post_job({"generator": "hunyuan2mv", "mode": "shape"}, {"back": png_bytes()})
    assert code == 400 and body["error"] == "view_required"
    return "worker up: generators available, view validation still enforced"


# --------------------------------------------------------------------------- #
# asset library
# --------------------------------------------------------------------------- #
def t_legacy_assets_load():
    """Pre-Step-5 assets must still list, with a valid generator id."""
    lst = assets.list_assets()
    assert lst, "no assets on disk to check"
    bad = [a["id"] for a in lst if a["generator"] not in config.GENERATOR_IDS]
    assert not bad, f"invalid generator ids leaked: {bad}"
    for a in lst:
        assert a["mode_label"], f"{a['id']} has no mode label"
        assert a["generator_name"], f"{a['id']} has no generator name"
    gens = sorted({a["generator"] for a in lst})
    return f"{len(lst)} assets, generators={gens}"


def t_assets_endpoint():
    d = get("/assets")
    assert "assets" in d
    for a in d["assets"]:
        assert a["generator"] in config.GENERATOR_IDS, a
    return f"/assets -> {len(d['assets'])} assets, all with valid generator ids"


def t_path_traversal():
    bad_ids = ["../etc", "../../etc/passwd", "..", ".", "", "foo/../bar",
               "20260101-000000-deadbeef/../../etc", "20260101-000000-DEADBEEF"]
    for bad in bad_ids:
        for fn in (assets.output_dir, assets.upload_dir):
            try:
                fn(bad)
            except assets.BadAssetId:
                continue
            raise AssertionError(f"{fn.__name__} accepted {bad!r}")
    return f"{len(bad_ids)} hostile ids rejected by both resolvers"


def t_traversal_over_http():
    for bad in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "."):
        try:
            urllib.request.urlopen(f"{BASE}/assets/{bad}/download", timeout=10)
            raise AssertionError(f"server served {bad!r}")
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 404), f"{bad!r} -> {exc.code}"
    return "traversal attempts rejected over HTTP too"


def main():
    print("=" * 72)
    print("Forge3D Step 5 - multi-model checks")
    print("=" * 72)

    print("\n[generator registry]")
    check("registry shape", t_registry)
    check("per-generator peak estimates", t_peak_table)
    check("mode isolation", t_mode_isolation)

    print("\n[memory gate]")
    check("protected floor enforced", t_gate_floor)
    check("resident model credited", t_gate_credits_resident_model)
    check("worker footprint credited", t_gate_credits_worker)
    check("no cross-backend credit", t_gate_no_cross_backend_credit)
    check("per-generator availability", t_gate_per_generator)
    check("InsufficientMemory payload", t_insufficient_payload)

    print("\n[HTTP contract]")
    check("/system shape", t_system_shape)
    check("unknown generator rejected", t_unknown_generator)
    check("cross-generator mode rejected", t_cross_generator_mode)
    check("multi-view requires front", t_multiview_requires_front)
    check("multi-view validates images", t_multiview_rejects_bad_image)
    check("missing image rejected", t_no_image)
    check("hunyuan backend state", t_hunyuan_backend_state)

    print("\n[asset library]")
    check("legacy assets still load", t_legacy_assets_load)
    check("/assets endpoint", t_assets_endpoint)
    check("path traversal (library)", t_path_traversal)
    check("path traversal (HTTP)", t_traversal_over_http)

    print("\n" + "=" * 72)
    if FAIL:
        print(f"RESULT: FAILED ({len(FAIL)}) -> {', '.join(FAIL)}")
        return 1
    print(f"RESULT: all {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
