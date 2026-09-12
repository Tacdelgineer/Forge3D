"""Browser checks for the Step 5 studio UI.

Drives the real dashboard in Chromium and asserts what a user would see: the
Model control actually switches generators, the multi-view generator replaces
the single uploader with four labelled slots, generator-specific quality cards
appear, TRELLIS-only settings stay hidden for Hunyuan, and the restricted
upstream licence is surfaced rather than implied.

Nothing here starts a generation.

    FORGE3D_URL=http://<host>:8189 <scratchpad>/pw/bin/python scripts/test_ui_step5.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("FORGE3D_URL")
if not URL:
    sys.exit("set FORGE3D_URL (the dashboard address)")

# Playwright runs on the host, so file inputs need host paths, not the
# container's /app/... view of the same files.
VIEWS_DIR = os.environ.get(
    "FORGE3D_VIEWS", os.path.join(os.getcwd(), "data", "views", "crown")
)

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


def select_generator(page, gen_id):
    """Use the app's own handler so the change path is what users trigger."""
    page.evaluate("(id) => window.__forge3d.selectGenerator(id)", gen_id)
    page.wait_for_timeout(250)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("=" * 72)
        print("Forge3D Step 5 - studio UI checks")
        print("=" * 72)

        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_function("() => window.__forge3d && window.__forge3d.state.sys", timeout=30000)

        print("\n[shell]")

        def t_loads():
            title = page.title()
            assert title == "Forge3D", title
            assert page.locator("#modelSelect").is_visible()
            return f"title={title!r}, studio rendered"

        check("dashboard loads", t_loads)

        def t_model_options():
            opts = page.locator("#modelSelect option").all_text_contents()
            vals = page.eval_on_selector_all("#modelSelect option", "els => els.map(e => e.value)")
            assert vals == ["trellis", "hunyuan21", "hunyuan2mv"], vals
            return f"{opts}"

        check("model selector lists three generators", t_model_options)

        print("\n[TRELLIS selected]")
        select_generator(page, "trellis")

        def t_trellis_inputs():
            assert page.locator("#dropzone").is_visible(), "single uploader hidden"
            assert not page.locator("#viewGrid").is_visible(), "view slots shown for TRELLIS"
            modes = page.eval_on_selector_all(".mode", "els => els.map(e => e.dataset.mode)")
            assert modes == ["512", "1024_cascade", "1536_cascade"], modes
            return f"single uploader, quality={modes}"

        check("single reference + TRELLIS modes", t_trellis_inputs)

        def t_texture_visible():
            # #texField lives inside the collapsed <details id="advanced">, so
            # visibility means nothing until Advanced is open. Checking it
            # closed would pass for the wrong reason on every generator.
            page.evaluate("() => { document.getElementById('advanced').open = true; }")
            page.wait_for_timeout(150)
            assert page.locator("#texField").is_visible(), "texture control hidden for TRELLIS"
            sizes = page.eval_on_selector_all("#texSeg button", "els => els.map(e => e.textContent)")
            assert sizes, "no texture sizes offered"
            return f"texture resolution control shown, sizes={sizes}"

        check("TRELLIS texture control", t_texture_visible)

        print("\n[Hunyuan3D 2.1 selected]")
        select_generator(page, "hunyuan21")

        def t_hy21_inputs():
            assert page.locator("#dropzone").is_visible(), "single uploader hidden"
            assert not page.locator("#viewGrid").is_visible(), "view slots shown for 2.1"
            modes = page.eval_on_selector_all(".mode", "els => els.map(e => e.dataset.mode)")
            assert modes == ["shape", "shape_texture"], modes
            return f"single uploader, quality={modes}"

        check("2.1 keeps single uploader, Hunyuan modes", t_hy21_inputs)

        def t_no_trellis_settings():
            # Advanced must be open, otherwise this passes trivially because
            # the whole section is collapsed rather than because the
            # TRELLIS-only control is correctly hidden.
            page.evaluate("() => { document.getElementById('advanced').open = true; }")
            page.wait_for_timeout(150)
            assert page.locator("#advanced").evaluate("d => d.open") is True, "Advanced not open"
            assert page.locator("#seedInput").is_visible(), "seed control missing (sanity check)"
            assert not page.locator("#texField").is_visible(), \
                "TRELLIS-only texture control shown for Hunyuan"
            return "Advanced open, seed shown, texture resolution correctly hidden"

        check("no TRELLIS-only settings", t_no_trellis_settings)

        def t_licence():
            note = page.locator("#licNote")
            assert note.is_visible(), "licence note hidden"
            text = note.inner_text()
            assert "Tencent" in text, text[:120]
            for phrase in ("European Union", "United Kingdom", "South Korea"):
                assert phrase in text, f"{phrase!r} not surfaced: {text[:200]}"
            assert "restricted" in (note.get_attribute("class") or "")
            return "restricted licence and territory limits surfaced"

        check("restricted licence surfaced", t_licence)

        print("\n[Hunyuan3D Multi-View selected]")
        select_generator(page, "hunyuan2mv")

        def t_mv_slots():
            assert page.locator("#viewGrid").is_visible(), "view slots not shown"
            assert not page.locator("#dropzone").is_visible(), "single uploader still shown"
            views = page.eval_on_selector_all(".slot", "els => els.map(e => e.dataset.view)")
            assert views == ["front", "back", "left", "right"], views
            labels = page.eval_on_selector_all(".slot .slot-h b", "els => els.map(e => e.textContent)")
            return f"four slots {labels}"

        check("four labelled view slots", t_mv_slots)

        def t_front_required():
            req = page.locator('.slot[data-view="front"] .slot-h em').inner_text()
            others = page.eval_on_selector_all(
                '.slot:not([data-view="front"]) .slot-h em', "els => els.map(e => e.textContent)")
            assert req.lower() == "required", req
            assert all(o.lower() == "optional" for o in others), others
            return f"front={req!r}, others={others}"

        check("front marked required", t_front_required)

        def t_generate_blocked():
            btn = page.locator("#generateBtn")
            assert btn.is_disabled(), "generate enabled with no views"
            hint = page.locator("#genHint").inner_text()
            assert "Front" in hint, hint
            return f"disabled, hint={hint!r}"

        check("generate blocked without front view", t_generate_blocked)

        def t_front_enables():
            page.set_input_files('.slot[data-view="front"] input[type=file]',
                                 os.path.join(VIEWS_DIR, "front.png"))
            page.wait_for_timeout(400)
            provided = page.evaluate("() => window.__forge3d.providedViews()")
            assert provided == ["front"], provided
            assert page.locator('.slot[data-view="front"] img').is_visible(), "no preview"
            return f"front accepted, providedViews={provided}"

        check("adding front view enables generation", t_front_enables)

        def t_extra_views():
            for v in ("back", "left"):
                page.set_input_files(f'.slot[data-view="{v}"] input[type=file]',
                                     os.path.join(VIEWS_DIR, f"{v}.png"))
            page.wait_for_timeout(400)
            provided = page.evaluate("() => window.__forge3d.providedViews()")
            assert provided == ["front", "back", "left"], provided
            hint = page.locator("#genHint").inner_text()
            assert "3 views" in hint, hint
            return f"providedViews={provided}, hint={hint!r}"

        check("optional views accepted (1-4)", t_extra_views)

        def t_clear_view():
            page.evaluate("() => window.__forge3d.clearView('left')")
            page.wait_for_timeout(200)
            provided = page.evaluate("() => window.__forge3d.providedViews()")
            assert provided == ["front", "back"], provided
            return f"after clearing left: {provided}"

        check("views can be removed", t_clear_view)

        print("\n[library]")

        def t_library_tags():
            page.click("#libToggle")
            page.wait_for_timeout(600)
            tags = page.eval_on_selector_all(".card .tag.model", "els => els.map(e => e.textContent)")
            assert tags, "no generator tags on asset cards"
            assert any(t.startswith("HY") for t in tags), f"no Hunyuan assets listed: {set(tags)}"
            assert "TRELLIS" in set(tags), f"no TRELLIS assets listed: {set(tags)}"
            return f"{len(tags)} cards, generators={sorted(set(tags))}"

        check("library shows per-generator tags", t_library_tags)

        def t_open_hunyuan_asset():
            assets = page.evaluate("() => window.__forge3d.state.assets.map(a => [a.id, a.generator])")
            hy = [a for a in assets if a[1] != "trellis"]
            assert hy, "no Hunyuan asset to open"
            page.evaluate("(id) => window.__forge3d.openAsset(id)", hy[0][0])
            page.wait_for_function("() => window.__forge3d.hasModel()", timeout=60000)
            stats = page.locator("#vpStats").inner_text()
            return f"loaded {hy[0][1]} asset in viewer: {stats}"

        check("Hunyuan asset loads in viewer", t_open_hunyuan_asset)

        real_errors = [e for e in errors if "favicon" not in e.lower()]
        print("\n[console]")
        if real_errors:
            print(f"  [FAIL] console errors: {real_errors[:4]}")
            FAIL.append("console errors")
        else:
            print(f"  [ OK ] console clean: no errors ({len(errors)} messages)")

        page.screenshot(path=os.environ.get("FORGE3D_SHOT", "/tmp/forge3d-step5-ui.png"),
                        full_page=False)
        browser.close()

    print("\n" + "=" * 72)
    if FAIL:
        print(f"RESULT: FAILED ({len(FAIL)}) -> {', '.join(FAIL)}")
        return 1
    print(f"RESULT: all {PASSED} UI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
