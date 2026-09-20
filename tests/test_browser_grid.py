"""The spreadsheet-like grids in a REAL browser: the Taxes page's expenses grid driven by the
same static/edit.js as Orders. Playwright launches the machine's own Chrome (no download); the
test skips where there is none (the Pi, CI). It exists because a unit test of the HTML cannot see
that Ctrl+Z did nothing (2026-09-19: the undo step lacked the row's entry id)."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_taxes import client  # noqa: E402,F401  (the fixture: an app with a fixture ledger)

playwright = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def served(client):
    """The test app on a real port, for a real browser."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture
def page():
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001 -- no Chrome here: not a failure of the code
            pytest.skip(f"no local Chrome for Playwright: {type(exc).__name__}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.errors = errors  # type: ignore[attr-defined]
        yield page
        browser.close()


def test_the_expenses_grid_writes_undoes_narrows_and_deletes(client, served, page, tmp_path):
    for when, what in (("2026-03-04", "boxes"), ("2026-03-05", "tape")):
        client.post("/taxes/expense", params={"year": "2026"}, follow_redirects=False,
                    data={"date": when, "description": what, "amount": "12.50", "profile": "alpha",
                          "category": "supplies", "receipt_url": "https://x/r"})
    store = tmp_path / "data" / "tax_inputs.json"
    ids = [e["id"] for e in json.loads(store.read_text(encoding="utf-8"))["2026"]["expenses"]]
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{served}/taxes?year=2026")

    # click, type, Enter writes the cell; Ctrl+Z puts it back; Ctrl+Y again
    cell = f'td[data-field="description"][data-entry-id="{ids[0]}"]'
    page.click(cell)
    page.keyboard.type("bigger")
    assert page.evaluate('!!document.querySelector("input.cell-input")')
    page.keyboard.press("Enter")
    page.wait_for_selector(cell + '[data-raw="bigger"]', timeout=5000)
    page.keyboard.press("Control+z")
    page.wait_for_selector(cell + '[data-raw="boxes"]', timeout=5000)
    page.keyboard.press("Control+y")
    page.wait_for_selector(cell + '[data-raw="bigger"]', timeout=5000)
    assert json.loads(store.read_text(encoding="utf-8"))["2026"]["expenses"][0]["description"] == "bigger"

    # a choice cell's dropdown narrows as one types
    page.click(f'td[data-field="category"][data-entry-id="{ids[1]}"]')
    page.keyboard.type("s")
    page.wait_for_timeout(150)
    assert page.evaluate("document.querySelector('.pop').innerText").strip() == "supplies", page.evaluate(
        "(document.activeElement ? document.activeElement.outerHTML.slice(0, 160) : 'none') + ' | pop: ' + ((document.querySelector('.pop') || {}).className) + ' | editor: ' + !!document.querySelector('input.cell-input') + ' | pop html: ' + (document.querySelector('.pop') || {}).innerHTML") + " | errors: " + repr(page.errors)
    page.keyboard.type("zz")
    page.wait_for_timeout(150)
    assert "a new answer" in page.evaluate("document.querySelector('.pop').innerText")
    page.keyboard.press("Escape")

    # a click on a link inside a cell selects the cell instead of following it
    page.click(f'td[data-field="receipt_url"][data-entry-id="{ids[1]}"] a')
    assert "/taxes" in page.url and page.evaluate('document.querySelectorAll("td.sel-cell").length') == 1
    assert page.evaluate('document.querySelector("td.sel-cell").dataset.field') == "receipt_url"
    page.click(f'td[data-field="receipt_url"][data-entry-id="{ids[1]}"]', button="right")
    assert page.evaluate("document.querySelector('.ctx button[data-act=\"open\"]').disabled") is False
    page.keyboard.press("Escape")

    # the right-click menu: Clear on a category cell, then Undo from the menu
    cat2 = f'td[data-field="category"][data-entry-id="{ids[1]}"]'
    page.click(cat2, button="right")
    assert page.is_visible(".ctx.on") and ">Copy<" in page.inner_html(".ctx")
    assert page.evaluate("document.querySelector('.ctx button[data-act=\"undo\"]').disabled") is False  # a write was made above
    page.click('.ctx button[data-act="clear"]')
    page.wait_for_selector(cat2 + '[data-raw=""]', timeout=5000)
    page.click(cat2, button="right")
    page.click('.ctx button[data-act="undo"]')
    page.wait_for_selector(cat2 + '[data-raw="supplies"]', timeout=5000)
    assert not page.is_visible(".ctx.on")

    # a click on a column's header cell selects the whole column
    page.click("table.expenses thead th:nth-child(4)")  # Category
    assert page.evaluate('document.querySelectorAll("td.sel-cell").length') == 2
    assert page.evaluate('Array.from(document.querySelectorAll("td.sel-cell")).every(td => td.dataset.field === "category")')
    page.keyboard.press("Escape")
    assert page.evaluate('document.querySelectorAll("td.sel-cell").length') == 0

    # the Add-an-Expense form's profile picker sits exactly beside its inputs
    page.evaluate("document.querySelector('details.add-row').open = true")  # folded by default
    boxes = page.evaluate("""() => Array.from(document.querySelectorAll('.expense-form .egrid > label, .expense-form .egrid > .lbl'))
        .slice(0, 4).map(el => { const c = el.querySelector('input, .summary-text').getBoundingClientRect(); return [Math.round(c.top), Math.round(c.height), Math.round(c.width)]; })""")
    assert len({b[0] for b in boxes}) == 1 and len({b[1] for b in boxes}) == 1 and len({b[2] for b in boxes}) == 1, boxes

    # a checkbox dropdown (the year picker is a radio one) narrows as one types, too
    opened = page.evaluate("""() => { const d = document.querySelector('details.multi[data-param]'); d.setAttribute('open', ''); d.querySelector('summary').focus(); return d.dataset.param; }""")
    assert opened
    assert not page.evaluate("!!document.querySelector('details.multi[open] .menu .narrow')")
    page.keyboard.type("zzz")
    assert page.evaluate("document.querySelector('details.multi[open] .menu .narrow').textContent") == "narrow: zzz"
    assert page.evaluate("Array.from(document.querySelectorAll('details.multi[open] .menu label:not(.all)')).every(l => l.hidden)")
    page.keyboard.press("Escape")
    assert not page.evaluate("!!document.querySelector('details.multi[open] .menu .narrow')")  # no prompt before typing
    page.keyboard.press("Escape")
    assert not page.evaluate("!!document.querySelector('details.multi[open]')")

    # a row number selects the row; Delete asks once through the page's dialog, then removes it
    page.click("table.expenses td.rownum")
    page.keyboard.press("Delete")
    page.wait_for_selector("dialog#settings-confirm[open]", timeout=3000)
    assert "Delete the selected expense?" in page.evaluate('document.querySelector("dialog#settings-confirm .question").textContent')
    page.click('dialog#settings-confirm button[value="ok"]')
    page.wait_for_url("**notice=Deleted**", timeout=5000)
    assert page.evaluate('document.querySelectorAll("table.expenses tbody tr").length') == 1
    assert page.evaluate('document.querySelectorAll("table.expenses .cell-upload").length') == 1  # the receipt cell's ⤒
    assert page.errors == []


@pytest.fixture
def phone():
    """A phone-sized, touch-capable page (iPhone-ish): a coarse pointer, 390px wide."""
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no local Chrome for Playwright: {type(exc).__name__}")
        context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                      is_mobile=True, has_touch=True)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.errors = errors  # type: ignore[attr-defined]
        yield page
        browser.close()


def test_a_phone_fits_the_pages_and_taps_edit(client, served, phone, tmp_path):
    client.post("/taxes/expense", params={"year": "2026"}, follow_redirects=False,
                data={"date": "2026-03-04", "description": "boxes", "amount": "12.50", "profile": "alpha",
                      "category": "supplies", "receipt_url": "https://x/r"})
    for path in ("/", "/orders", "/orders?view=cards", "/taxes?year=2026", "/settings", "/activity", "/audit"):
        phone.goto(f"{served}{path}")
        over = phone.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert over == 0, f"{path} sticks out {over}px past a phone's viewport"
    assert phone.evaluate("window.matchMedia('(pointer: coarse)').matches")
    phone.goto(f"{served}/taxes?year=2026")
    cell = 'td[data-field="description"]'
    phone.tap(cell)  # the first tap selects
    assert phone.evaluate('document.querySelectorAll("td.sel-cell").length') == 1
    assert not phone.evaluate('!!document.querySelector("input.cell-input")')
    phone.tap(cell)  # the second tap on the selected cell edits
    phone.wait_for_selector("input.cell-input", timeout=3000)
    phone.keyboard.press("Escape")
    assert phone.errors == []


def test_only_the_sheet_header_sticks_and_the_filter_bar_wraps_to_the_window(client, served, page):
    """ "for iPad it is still really
    unoptimised -- the app should detect the window size and optimise for it"."""
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{served}/orders")
    assert page.evaluate("getComputedStyle(document.querySelector('body.wide .pinned')).top") == "auto"  # sticky LEFT only: it scrolls away vertically
    th = page.evaluate("(() => { const cs = getComputedStyle(document.querySelector('table.sheetlike th')); return [cs.position, cs.top]; })()")
    assert th == ["sticky", "0px"]
    ROWS = """(() => { const els = Array.from(document.querySelectorAll('#filters > *')).filter(e => e.offsetWidth);
        let rows = 1; for (let i = 1; i < els.length; i++) if (els[i].getBoundingClientRect().left <= els[i - 1].getBoundingClientRect().left) rows++; return rows; })()"""
    assert page.evaluate(ROWS) == 1  # a full-size window: the filter bar is one row, as before
    page.set_viewport_size({"width": 820, "height": 1180})  # an iPad, portrait
    page.goto(f"{served}/orders")
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0
    assert page.evaluate("Array.from(document.querySelectorAll('#filters > *')).every(e => e.getBoundingClientRect().right <= innerWidth + 1)")
    assert page.evaluate(ROWS) > 1  # wrapped onto more rows instead of running off the edge
    assert page.errors == []


def test_a_sideways_scroll_moves_only_the_table(client, served, page):
    page.set_viewport_size({"width": 900, "height": 700})
    page.goto(f"{served}/orders")
    LEFTS = """() => ['h1', '.lead', '.pinned', '.count', 'details.add-row'].map(s => { const el = document.querySelector('body.wide ' + s); return el ? [s, Math.round(el.getBoundingClientRect().left)] : [s, null]; })"""
    before = page.evaluate(LEFTS)
    page.evaluate("document.querySelector('main').scrollLeft = 600")
    page.wait_for_timeout(100)
    assert page.evaluate("document.querySelector('main').scrollLeft") >= 500  # the table is wider than the window
    assert page.evaluate(LEFTS) == before, "a block above the table shifted on a sideways scroll"  # not even the margin's width
    widths = page.evaluate("""() => { const m = document.querySelector('main'); return ['h1', '.lead', '.pinned', '.count'].map(s => Math.round(document.querySelector('body.wide ' + s).getBoundingClientRect().width) - m.clientWidth); }""")
    assert widths == [0, 0, 0, 0], widths  # each exactly as wide as the scroll region
    assert int(page.evaluate("getComputedStyle(document.querySelector('body.wide .pinned')).zIndex")) > int(page.evaluate("getComputedStyle(document.querySelector('table.sheetlike th')).zIndex") or 0)  # its dropdowns paint over the sheet header
    add_row_z = page.evaluate("() => { const d = document.createElement('details'); d.className = 'add-row'; document.body.appendChild(d); const z = getComputedStyle(d).zIndex; d.remove(); return z; }")
    assert int(page.evaluate("getComputedStyle(document.querySelector('body.wide .pinned')).zIndex")) > int(add_row_z or 0)  # and over the closed Add-a-row box
    assert page.evaluate("document.querySelector('table.sheetlike th.col-order_date').getBoundingClientRect().left") < 0  # the table did move
    assert page.errors == []


def test_a_cell_that_is_not_editable_still_copies(served, page):
    """Item Name is a key, never editable, so it had no tabindex, took no
    focus, and the Ctrl+C handler (gated on focus inside the grid) ignored it."""
    page.set_viewport_size({"width": 1400, "height": 800})
    page.goto(f"{served}/orders?days=0")
    cell = page.locator("table.sheetlike td[data-field=item_name]").first
    cell.click()
    page.keyboard.press("Control+c")
    page.wait_for_timeout(150)
    assert page.evaluate("(document.getElementById('grid-clipboard') || {}).value") == cell.inner_text().strip()
    assert page.evaluate("document.activeElement.getAttribute('data-field')") == "item_name"



def test_a_clicked_month_bar_shows_no_focus_ring(served, page):
    """on the 12-month chart --
    Chrome's default focus ring round the column's hit area after a click. Keyboard focus keeps a
    tint on the hit rect instead."""
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{served}/")
    bar = page.locator("figure.months a.bar").first
    page.evaluate("el => el.focus()", bar.element_handle())
    assert page.evaluate("document.activeElement.classList.contains('bar')")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "none"
    # keyboard focus: tab forward from the body until a bar has focus, then its hit rect is tinted
    page.evaluate("document.body.focus()")
    for _ in range(80):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.classList.contains('bar')"):
            break
    assert page.evaluate("document.activeElement.classList.contains('bar')")
    assert page.evaluate("getComputedStyle(document.activeElement.querySelector('.hit')).fill") != "rgba(0, 0, 0, 0)"
