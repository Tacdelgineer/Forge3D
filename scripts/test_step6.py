"""Step 6 checks: the exclusive-mode memory projection, the API contract, the
host-helper client, and — most importantly — that normal mode is unchanged.

Run with:  docker compose exec -T trellis python /app/scripts/test_step6.py

Every case here is one that is refused before a job is created, so running this
never starts a real generation, touches the GPU, or pauses a real workload. The lifecycle
itself (acquire / release / lease expiry / crash recovery) is exercised against
the live host by scripts/test_step6_host.sh, because it can only be proved by
actually stopping and restarting things.
"""
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

from app import config, memory, resctl  # noqa: E402

BASE = "http://127.0.0.1:8189"
FAIL = []
PASSED = 0


def check(name, fn):
    global PASSED
    try:
        print(f"  [ OK ] {name}: {fn()}")
        PASSED += 1
    except Exception as exc:
        print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
        FAIL.append(name)


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as r:
        return json.load(r)


def png_bytes(size=(64, 64)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 60)).save(buf, format="PNG")
    return buf.getvalue()


def post_job(fields, image=True):
    """multipart POST /jobs, returning (status, body)."""
    boundary = "----forge3dstep6"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    body = "".join(parts).encode()
    if image:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                 f"filename=\"r.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        body += png_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/jobs", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --------------------------------------------------------------------------- #
# 1. the gate itself
# --------------------------------------------------------------------------- #
def t_floor_unchanged():
    assert config.PROTECTED_FLOOR_GB == 40.0, config.PROTECTED_FLOOR_GB
    assert config.MIN_AVAILABLE_GB == 65.0, config.MIN_AVAILABLE_GB
    return f"floor {config.PROTECTED_FLOOR_GB} GiB, min headroom {config.MIN_AVAILABLE_GB} GiB"


def t_exclusive_uses_same_floor():
    """Exclusive mode buys memory, never permission."""
    a = memory.assess("1536_cascade", "trellis", available=50.0, footprint=0.0,
                      extra_footprint=0.0)
    b = memory.assess("1536_cascade", "trellis", available=50.0, footprint=0.0,
                      extra_footprint=0.0, exclusive=True, releasable=46.0)
    assert a["floor_gb"] == b["floor_gb"] == 40.0
    assert a["required_headroom_gb"] == b["required_headroom_gb"], (a, b)
    return (f"same floor {b['floor_gb']} and requirement "
            f"{b['required_headroom_gb']} GiB with and without exclusive")


def t_exclusive_adds_releasable():
    base = memory.assess("1024_cascade", "trellis", available=50.0, footprint=0.0,
                         extra_footprint=0.0)
    xc = memory.assess("1024_cascade", "trellis", available=50.0, footprint=0.0,
                       extra_footprint=0.0, exclusive=True, releasable=46.0)
    assert not base["available"], "50 GiB should not clear HQ"
    assert xc["available"], "50 + 46 GiB should clear HQ"
    assert xc["headroom_gb"] == base["headroom_gb"] + 46.0
    return f"HQ: {base['headroom_gb']} GiB refused -> {xc['headroom_gb']} GiB allowed"


def t_exclusive_still_refuses_when_short():
    """The honest case: freeing everything is still not enough."""
    xc = memory.assess("1536_cascade", "trellis", available=20.0, footprint=0.0,
                       extra_footprint=0.0, exclusive=True, releasable=30.0)
    assert not xc["available"]
    assert "Even after freeing" in xc["reason"], xc["reason"]
    return xc["reason"][:96] + "..."


def t_zero_releasable_is_identical():
    """No helper -> exclusive projection must equal the normal one exactly."""
    a = memory.assess("1024_cascade", "trellis", available=70.0, footprint=1.0,
                      extra_footprint=0.0)
    b = memory.assess("1024_cascade", "trellis", available=70.0, footprint=1.0,
                      extra_footprint=0.0, exclusive=True, releasable=0.0)
    for k in ("available", "headroom_gb", "projected_min_gb", "required_headroom_gb", "reason"):
        assert a[k] == b[k], f"{k}: {a[k]!r} != {b[k]!r}"
    return "identical across all gate fields"


def t_assess_marks_projections():
    a = memory.assess("512", "trellis")
    b = memory.assess("512", "trellis", exclusive=True, releasable=10.0)
    assert a["exclusive"] is False and a["releasable_gb"] == 0.0
    assert b["exclusive"] is True and b["releasable_gb"] == 10.0
    return "a projection is always labelled as one"


# --------------------------------------------------------------------------- #
# 2. /system
# --------------------------------------------------------------------------- #
def t_system_exclusive_block():
    x = get("/system")["exclusive"]
    for k in ("supported", "releasable_gb", "projected_available_gb", "would_pause",
              "held", "last_restore"):
        assert k in x, f"missing {k}"
    return (f"supported={x['supported']} releasable={x['releasable_gb']} GiB "
            f"projected={x['projected_available_gb']} GiB "
            f"would_pause={[i['name'] for i in x['would_pause']]}")


def t_system_never_claims_free_ram():
    """/system must report the real machine, not 128 GB of wishful thinking."""
    s = get("/system")
    m, x = s["memory"], s["exclusive"]
    assert m["available_gb"] < m["total_gb"], m
    assert x["projected_available_gb"] <= m["total_gb"], x
    assert abs(x["projected_available_gb"] - (m["available_gb"] + x["releasable_gb"])) < 0.05
    return (f"available {m['available_gb']} of {m['total_gb']} GiB; projected "
            f"{x['projected_available_gb']} = available + {x['releasable_gb']} releasable")


def t_modes_carry_both_verdicts():
    trellis = next(g for g in get("/system")["generators"] if g["id"] == "trellis")
    for m in trellis["modes"]:
        for k in ("available", "exclusive_available", "exclusive_projected_min_gb"):
            assert k in m, f"{m['id']} missing {k}"
    return " ".join(f"{m['id']}:{m['available']}/{m['exclusive_available']}"
                    for m in trellis["modes"]) + "  (normal/exclusive)"


def t_exclusive_never_weaker():
    """Freeing memory can only ever help; it must never make a mode unavailable
    that was available, which would mean the projection is arithmetically wrong."""
    trellis = next(g for g in get("/system")["generators"] if g["id"] == "trellis")
    bad = [m["id"] for m in trellis["modes"] if m["available"] and not m["exclusive_available"]]
    assert not bad, f"exclusive made these worse: {bad}"
    return "no mode is available normally but unavailable exclusively"


# --------------------------------------------------------------------------- #
# 3. API contract
# --------------------------------------------------------------------------- #
def t_job_default_not_exclusive():
    """A job record built the Step 5 way is a normal-mode job."""
    from app import jobs

    j = jobs.Job(id="t", generator="trellis", mode="512", seed=1,
                 filename="r.png", texture_size=4096)
    d = j.as_dict()
    assert d["exclusive"] is False, d
    assert "exclusive_report" not in d, d
    return "Job defaults to exclusive=False with no report"


def t_exclusive_parsing():
    """Opt-in means opt-in: only an explicit yes enables it."""
    from app.main import _parse_exclusive

    for raw in ("true", "True", "1", "yes", "on", " TRUE "):
        assert _parse_exclusive(raw) is True, raw
    for raw in (None, "", "false", "0", "no", "off", "maybe", "２"):
        assert _parse_exclusive(raw) is False, raw
    return "only 1/true/yes/on enable it; unknown values do not"


def t_omitted_field_still_gated():
    """A Step 5 client posting no `exclusive` field gets the unchanged gate:
    an unavailable mode is still refused on the normal numbers."""
    code, body = post_job({"generator": "trellis", "mode": "1536_cascade", "seed": "1"})
    assert code == 503, (code, body)
    assert body["error"] == "insufficient_memory", body
    assert body["floor_gb"] == 40.0, body
    return f"HTTP 503 insufficient_memory against the {body['floor_gb']} GiB floor"


def t_bad_mode_still_rejected():
    code, body = post_job({"generator": "trellis", "mode": "9999", "exclusive": "true"})
    assert code == 400 and body["error"] == "invalid_pipeline_type", (code, body)
    return "exclusive does not bypass mode validation"


def t_image_still_required():
    code, body = post_job({"generator": "trellis", "mode": "512", "exclusive": "true"},
                          image=False)
    assert code == 400 and body["error"] == "image_required", (code, body)
    return "exclusive does not bypass input validation"


# --------------------------------------------------------------------------- #
# 4. the helper client
# --------------------------------------------------------------------------- #
def t_client_degrades():
    """An absent helper must be a clean 'no', never an exception."""
    real = resctl.SOCKET_PATH
    try:
        resctl.SOCKET_PATH = "/nonexistent/forge3d.sock"
        assert resctl.available() is False
        assert resctl.state() is None
        assert resctl.releasable_gb() == 0.0
        assert resctl.describe_pause() == []
        try:
            resctl._call("GET", "/state")
            raise AssertionError("should have raised")
        except resctl.ResctlError as exc:
            assert exc.error == "helper_unavailable", exc.error
    finally:
        resctl.SOCKET_PATH = real
    return "missing socket -> not available, 0.0 GiB, clean ResctlError"


def t_helper_reachable():
    s = resctl.state()
    assert s is not None, f"no helper at {resctl.SOCKET_PATH}"
    assert s["ok"] and "snapshot" in s
    return (f"helper up, exclusive={s['exclusive']}, "
            f"managed_unit={s['managed_unit']}, MemAvailable={s['mem_available_gb']} GiB")


def t_describe_pause_is_specific():
    items = resctl.describe_pause()
    assert items, "helper reports nothing releasable"
    for i in items:
        assert i["basis"], f"{i['name']} has no stated basis"
        assert i["action"], f"{i['name']} has no stated action"
    return "; ".join(f"{i['name']} ~{i['est_gb']} GiB" for i in items)


def t_no_arbitrary_command():
    """The client exposes no way to ask the helper to run something."""
    import inspect

    src = inspect.getsource(resctl)
    for bad in ("subprocess", "os.system", "shell=True", "popen"):
        assert bad not in src.lower(), f"client references {bad}"
    import re

    paths = {"/state", "/acquire", "/renew", "/release"}
    used = set(re.findall(r'_call\(\s*"(?:GET|POST)",\s*"(/[a-z]+)"', src))
    assert used, "found no helper calls to check"
    assert used <= paths, f"unexpected helper path: {used - paths}"
    return f"client calls only {sorted(used)} and cannot execute anything"


print("=" * 72)
print("Forge3D Step 6 - exclusive / max quality checks")
print("=" * 72)
print("\n[memory gate]")
check("protected floor and minimum are unchanged", t_floor_unchanged)
check("exclusive uses the same floor and requirement", t_exclusive_uses_same_floor)
check("exclusive credits the releasable memory", t_exclusive_adds_releasable)
check("exclusive still refuses when it is not enough", t_exclusive_still_refuses_when_short)
check("zero releasable is identical to normal mode", t_zero_releasable_is_identical)
check("a projection is labelled as a projection", t_assess_marks_projections)

print("\n[/system]")
check("exclusive block present and complete", t_system_exclusive_block)
check("reports the real machine, not 128 GB", t_system_never_claims_free_ram)
check("modes carry normal and exclusive verdicts", t_modes_carry_both_verdicts)
check("exclusive never makes a mode worse", t_exclusive_never_weaker)

print("\n[API contract]")
check("a job defaults to normal mode", t_job_default_not_exclusive)
check("only an explicit yes enables exclusive", t_exclusive_parsing)
check("omitting the field keeps the old gate", t_omitted_field_still_gated)
check("exclusive does not bypass mode validation", t_bad_mode_still_rejected)
check("exclusive does not bypass input validation", t_image_still_required)

print("\n[host helper client]")
check("degrades cleanly when the helper is absent", t_client_degrades)
check("helper is reachable", t_helper_reachable)
check("what it would pause is specific and sourced", t_describe_pause_is_specific)
check("client cannot execute arbitrary commands", t_no_arbitrary_command)

print("\n" + "=" * 72)
if FAIL:
    print(f"RESULT: {PASSED} passed, {len(FAIL)} FAILED -> {FAIL}")
    sys.exit(1)
print(f"RESULT: all {PASSED} Step 6 checks passed")
