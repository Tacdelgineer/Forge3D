"""Browser checks for the Exclusive / Max Quality control.

Asserts what a user actually sees: the checkbox appears only for TRELLIS, its
copy says what it does, turning it on unlocks exactly the modes the measured
numbers support and no others, the pause list names real workloads, and the
projection is presented as a projection.

Nothing here starts a generation: the Generate button is never clicked.

    FORGE3D_URL=http://<host>:8189 <venv>/bin/python scripts/test_ui_step6.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("FORGE3D_URL")
if not URL:
    sys.exit("set FORGE3D_URL (the dashboard address)")

FAIL, PASSED = [], 0


def check(name, fn):
    global PASSED
    try:
        print(f"  [ OK ] {name}: {fn()}")
        PASSED += 1
    except Exception as exc:
        print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
        FAIL.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(URL, wait_until="networkidle")
    sysinfo = page.evaluate("() => fetch('/system').then(r => r.json())")
    xsys = sysinfo["exclusive"]

    print("=" * 72)
    print("Forge3D Step 6 - exclusive / max quality UI")
    print("=" * 72)
    print(f"\n  host: releasable {xsys['releasable_gb']} GiB, "
          f"projected {xsys['projected_available_gb']} GiB, "
          f"supported={xsys['supported']}")

    print("\n[the control]")

    def t_visible_for_trellis():
        page.select_option("#modelSelect", "trellis")
        page.wait_for_timeout(300)
        assert page.is_visible("#excl"), "exclusive control not shown for TRELLIS"
        return "shown under Quality for TRELLIS"

    def t_copy():
        txt = page.inner_text("#excl .excl-txt")
        assert "Exclusive / Max Quality" in txt, txt
        for phrase in ("pauses", "free memory", "restores them"):
            assert phrase in txt.lower().replace("frees", "free"), f"missing {phrase!r}: {txt}"
        return repr(" ".join(txt.split())[:88] + "...")

    def t_hidden_for_hunyuan():
        # The Hunyuan worker is itself one of the things exclusive mode pauses,
        # so offering it there would be incoherent.
        page.select_option("#modelSelect", "hunyuan21")
        page.wait_for_timeout(300)
        hidden = page.is_hidden("#excl")
        page.select_option("#modelSelect", "trellis")
        page.wait_for_timeout(300)
        assert hidden, "exclusive control shown for a Hunyuan generator"
        return "hidden for Hunyuan generators, shown again for TRELLIS"

    check("appears for TRELLIS", t_visible_for_trellis)
    check("copy explains pause-and-restore", t_copy)
    check("not offered for Hunyuan", t_hidden_for_hunyuan)

    print("\n[honesty]")

    def t_matches_backend():
        """The checkbox must be usable exactly when the backend says it can
        deliver, never when it cannot."""
        usable = page.evaluate("() => !document.getElementById('exclToggle').disabled")
        expect = bool(xsys["supported"] and not xsys["held"] and xsys["releasable_gb"] > 0)
        assert usable == expect, f"toggle usable={usable} but backend says {expect}"
        return f"enabled={usable}, matching supported={xsys['supported']} " \
               f"releasable={xsys['releasable_gb']} GiB held={xsys['held']}"

    def t_unlocks_only_supported():
        """Turning it on must follow exclusive_available exactly - not simply
        enable everything."""
        page.check("#exclToggle")
        page.wait_for_timeout(400)
        got = page.evaluate("""() => Object.fromEntries(
            [...document.querySelectorAll('#modes .mode')].map(l =>
                [l.dataset.mode, l.dataset.available === 'true']))""")
        trellis = next(g for g in sysinfo["generators"] if g["id"] == "trellis")
        want = {m["id"]: bool(m["exclusive_available"]) for m in trellis["modes"]}
        assert got == want, f"UI {got} != backend {want}"
        return " ".join(f"{k}={v}" for k, v in got.items())

    def t_marks_exclusive_only():
        """A mode reachable only because exclusive is on must be marked as such,
        not shown as ordinary availability."""
        marked = page.evaluate("""() => [...document.querySelectorAll('#modes .mode')]
            .filter(l => l.dataset.exclusiveOnly === 'true').map(l => l.dataset.mode)""")
        trellis = next(g for g in sysinfo["generators"] if g["id"] == "trellis")
        want = [m["id"] for m in trellis["modes"]
                if m["exclusive_available"] and not m["available"]]
        assert sorted(marked) == sorted(want), f"marked {marked}, expected {want}"
        state = page.evaluate("""() => [...document.querySelectorAll('#modes .mode')]
            .filter(l => l.dataset.exclusiveOnly === 'true')
            .map(l => l.querySelector('.mode-state').textContent)""")
        assert all("exclusive" in s for s in state), state
        return f"{want or 'none'} marked exclusive-only, labelled {state}"

    def t_names_what_pauses():
        detail = page.inner_text("#exclDetail")
        assert "Will pause" in detail, detail
        for item in xsys["would_pause"]:
            assert item["name"] in detail, f"{item['name']} not listed: {detail}"
        return "lists " + ", ".join(i["name"] for i in xsys["would_pause"])

    def t_projection_labelled():
        """The projected figure must not be dressed up as a measurement."""
        import re

        detail = page.inner_text("#exclDetail")
        m = re.search(r"Projected MemAvailable ~([\d.]+) GiB", detail)
        assert m, detail
        shown = float(m.group(1))
        live = page.evaluate("() => fetch('/system').then(r => r.json())")["exclusive"]
        # A few GiB of drift between the two reads is the machine working, not a
        # bug; an order-of-magnitude difference would be.
        assert abs(shown - live["projected_available_gb"]) < 5.0, \
            f"shown {shown}, backend {live['projected_available_gb']}"
        low = detail.lower()
        assert "estimated" in low and "measured after" in low, detail
        return (f"~{shown} GiB shown (backend {live['projected_available_gb']}), "
                f"labelled estimated and gated on measured")

    def t_off_restores_old_view():
        """With the box clear, availability must be byte-identical to Step 5."""
        page.uncheck("#exclToggle")
        page.wait_for_timeout(400)
        got = page.evaluate("""() => Object.fromEntries(
            [...document.querySelectorAll('#modes .mode')].map(l =>
                [l.dataset.mode, l.dataset.available === 'true']))""")
        trellis = next(g for g in sysinfo["generators"] if g["id"] == "trellis")
        want = {m["id"]: bool(m["available"]) for m in trellis["modes"]}
        assert got == want, f"UI {got} != normal-mode backend {want}"
        assert page.is_hidden("#exclDetail"), "pause list still shown with the box clear"
        return " ".join(f"{k}={v}" for k, v in got.items()) + "  (normal-mode values)"

    check("enabled state matches the backend", t_matches_backend)
    check("unlocks exactly the supported modes", t_unlocks_only_supported)
    check("marks exclusive-only modes", t_marks_exclusive_only)
    check("names the workloads it would pause", t_names_what_pauses)
    check("labels the projection as a projection", t_projection_labelled)
    check("turning it off restores normal-mode view", t_off_restores_old_view)

    print("\n[console]")
    check("console clean", lambda: f"no errors ({len(errors)} messages)"
          if not errors else (_ for _ in ()).throw(AssertionError(errors[:3])))
    browser.close()

print("\n" + "=" * 72)
if FAIL:
    print(f"RESULT: {PASSED} passed, {len(FAIL)} FAILED -> {FAIL}")
    sys.exit(1)
print(f"RESULT: all {PASSED} Step 6 UI checks passed")
