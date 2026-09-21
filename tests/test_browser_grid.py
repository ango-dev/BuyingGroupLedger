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


@pytest.fixture
def phone():
    """A phone: touch, a coarse pointer, a narrow viewport (Playwright's Pixel 5 descriptor)."""
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no local Chrome for Playwright: {type(exc).__name__}")
        context = browser.new_context(**p.devices["Pixel 5"])
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.errors = errors  # type: ignore[attr-defined]
        yield page
        browser.close()


def _touch(cdp, kind, x=0, y=0):
    """One touch event over an open CDP session (a touch sequence must stay within one session)."""
    points = [] if kind == "touchEnd" else [{"x": x, "y": y}]
    cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})


def _touch_drag(page, x1, y1, x2, y2, steps=6):
    cdp = page.context.new_cdp_session(page)
    _touch(cdp, "touchStart", x1, y1)
    for i in range(1, steps + 1):
        _touch(cdp, "touchMove", x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
    _touch(cdp, "touchEnd")
    cdp.detach()


def _long_press(page, x, y, hold_ms=750):
    cdp = page.context.new_cdp_session(page)
    _touch(cdp, "touchStart", x, y)
    page.wait_for_timeout(hold_ms)
    _touch(cdp, "touchEnd")
    cdp.detach()


def _centre(box):
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_the_grid_works_by_touch(served, phone, client):
    """On a phone a tap selects and shows the corner handle; a drag from the handle extends the
    range; a drag down the row numbers selects rows; a long press opens the menu, which has Select
    all. Real touch events through CDP in the machine's Chrome."""
    csv_text = "Order Number,Date,Item,Qty,Status\n" + "".join(f"X{i},3/1{i}/2026,Thing {i},1,paid\n" for i in range(1, 4))
    client.post("/tools/import/upload", files={"source": ("old.csv", csv_text.encode(), "text/csv")}, follow_redirects=False)
    client.post("/tools/import/map", data={"map.0": "order_id", "map.1": "order_date", "map.2": "item_name", "map.3": "quantity",
                                          "map.4": "status", "date_order": "", "profile": ""}, follow_redirects=False)
    assert client.post("/tools/import/run", follow_redirects=False).status_code == 303
    page = phone
    page.goto(f"{served}/tools/import")
    page.wait_for_selector("table.sheetlike tbody tr")
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    rows = page.locator("table.sheetlike tbody tr")
    assert rows.count() == 3
    first_cells = [rows.nth(i).locator("td.edit").first for i in range(3)]
    field = first_cells[0].get_attribute("data-field")
    # a tap selects the cell and shows the handle at its corner
    first_cells[0].scroll_into_view_if_needed()
    x, y = _centre(first_cells[0].bounding_box())
    page.touchscreen.tap(x, y)
    page.wait_for_timeout(100)
    assert page.locator("td.sel-cell").count() == 1
    handle = page.locator(".sel-handle.on")
    assert handle.count() == 1
    # a drag from the handle to the third row's cell selects the three cells of the column
    hx, hy = _centre(handle.bounding_box())
    tx, ty = _centre(first_cells[2].bounding_box())
    _touch_drag(page, hx, hy, tx, ty)
    assert page.locator("td.sel-cell").count() == 3
    assert page.evaluate(f"Array.from(document.querySelectorAll('td.sel-cell')).every(td => td.dataset.field === '{field}')")
    # a drag down the row numbers selects the rows (and their cells)
    nums = page.locator("table.sheetlike tbody td.rownum")
    x1, y1 = _centre(nums.nth(0).bounding_box())
    x3, y3 = _centre(nums.nth(2).bounding_box())
    _touch_drag(page, x1, y1, x3, y3)
    assert page.locator("table.sheetlike tbody tr.selected").count() == 3
    cols = page.evaluate("document.querySelector('table.sheetlike tbody tr').cells.length")
    assert page.locator("td.sel-cell").count() == 3 * (cols - 1)
    assert page.locator("th.sel-col").count() == 0  # every row by a drag: no header mark (only Select all marks)
    # a finger along the header row selects the columns crossed
    head = page.locator(f"table.sheetlike thead th.col-{field}")
    head.scroll_into_view_if_needed()
    hx1, hy1 = _centre(head.bounding_box())
    hx0, hy0 = _centre(page.locator("table.sheetlike thead th.missing").bounding_box())
    _touch_drag(page, hx1, hy1, max(hx0, 4), hy0)
    assert page.locator("table.sheetlike thead th.sel-col").count() == 2
    assert page.locator("td.sel-cell").count() == 2 * 3
    page.wait_for_timeout(100)
    # a tap on a cell drops that; a long press opens the menu, whose Select all takes every row
    x, y = _centre(first_cells[1].bounding_box())
    page.touchscreen.tap(x, y)
    assert page.locator("table.sheetlike tbody tr.selected").count() == 0 and page.locator("td.sel-cell").count() == 1
    _long_press(page, x, y)
    page.wait_for_selector(".ctx.on")
    page.wait_for_timeout(650)  # the menu ignores the tap that opened it for a moment
    item = page.locator(".ctx.on button[data-act=all]")
    ix, iy = _centre(item.bounding_box())
    page.touchscreen.tap(ix, iy)
    assert page.locator("table.sheetlike tbody tr.selected").count() == 3
    assert page.locator("th.sel-col").count() == cols - 1  # Select all marks the headers
    assert page.errors == []


def test_the_tabs_fold_into_a_dropdown_on_a_phone(served, phone):
    phone.goto(f"{served}/orders")
    phone.wait_for_selector("header.top.compact")
    assert not phone.locator("header nav .tabs").is_visible()
    pick = phone.locator("header nav details.nav-pick")
    assert pick.is_visible() and pick.locator("summary strong").inner_text() == "Orders"
    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")  # nothing sticks out sideways
    pick.locator("summary").first.click()  # the dropdown's own summary, not the nested Tools one
    items = pick.locator(".menu a").evaluate_all("as => as.map(a => a.firstChild.textContent.trim())")  # the name, not its badge
    assert items[:6] == ["Overview", "Orders", "Activity", "Audit", "Reconciliation", "Taxes"] and "Settings" not in items  # the gear has it
    # Tools is a dropdown of its own inside the menu: closed here, its items hidden
    tools = pick.locator(".menu details.sub")
    assert tools.locator("summary").inner_text().startswith("Tools") and not tools.locator("a", has_text="Run once").is_visible()
    tools.locator("summary").click()
    assert tools.locator("a", has_text="Run once").is_visible()
    # the open menu hangs right below the header (its own 4px gap) and scrolls with it -- on a page the window scrolls (Overview; Orders scrolls inside its main area)
    header_bottom = phone.evaluate("document.querySelector('header.top').getBoundingClientRect().bottom")
    menu = pick.locator(".menu")
    assert 0 <= menu.bounding_box()["y"] - header_bottom < 8
    phone.goto(f"{served}/")
    phone.wait_for_selector("header.top.compact")
    phone.locator("header nav details.nav-pick summary").first.click()
    menu = phone.locator("header nav details.nav-pick .menu")
    before = menu.bounding_box()["y"]
    phone.evaluate("window.scrollBy(0, 200)")
    phone.wait_for_timeout(100)
    assert phone.evaluate("scrollY") == 200
    assert abs((before - menu.bounding_box()["y"]) - 200) < 12  # moved with the page (a fixed menu would not)
    phone.goto(f"{served}/orders")
    phone.wait_for_selector("header.top.compact")
    pick = phone.locator("header nav details.nav-pick")
    pick.locator("summary").first.click()
    pick.locator(".menu a", has_text="Taxes").click()
    phone.wait_for_url("**/taxes*")
    assert phone.locator("header nav details.nav-pick summary strong").inner_text() == "Taxes"
    assert phone.errors == []


def test_the_expenses_and_activity_tables_stack_on_a_phone(served, phone, client, tmp_path):
    """("does it make sense for cards to exist for expenses and activity?" -- no:
    the same table stacks on a narrow screen): each row's cells one under another with a heading,
    the row number at the left, the headers a row of sort chips; the Orders grid keeps its sheet."""
    from diagnostics import activity
    log = tmp_path / "logs" / "activity.jsonl"
    activity.record("edit", "Orders: Quantity 2 -> 3", {"order_id": "111-0000001-0000001"}, path=log)
    activity.record("backup", "Backup ledger_backup_1.zip", {}, path=log)
    client.post("/taxes/expense", params={"year": "2026"}, follow_redirects=False,
                data={"date": "2026-03-04", "description": "boxes", "amount": "12.50", "profile": "alpha",
                      "category": "supplies", "receipt_url": "https://x/r"})
    page = phone
    page.goto(f"{served}/taxes?year=2026")
    page.wait_for_selector("table.expenses tbody tr")
    assert page.evaluate("getComputedStyle(document.querySelector('table.expenses tbody tr')).display") == "grid"
    assert page.evaluate("getComputedStyle(document.querySelector('table.expenses td[data-field=amount]'), '::before').content") == '"Amount"'
    assert page.evaluate("document.querySelector('table.expenses').getBoundingClientRect().width <= innerWidth")
    # the 46px number cell fits its track: it never overhangs the first field
    gap_after_rownum = """(sel => {
        const tr = document.querySelector(sel), num = tr.querySelector('td.rownum');
        const first = [...tr.querySelectorAll('td')].find(td => td !== num && td.offsetParent);
        return first.getBoundingClientRect().left - num.getBoundingClientRect().right;
    })"""
    assert page.evaluate(gap_after_rownum, "table.expenses tbody tr.has-num") >= 0
    chips = page.locator("table.expenses thead th")
    assert chips.filter(has_text="Amount").is_visible() and not page.locator("table.expenses thead th.rownum").is_visible()
    chips.filter(has_text="Amount").locator("a.sort").click()   # the chip's arrow still sorts
    page.wait_for_url("**esort=amount*")
    page.wait_for_selector("table.expenses tbody tr")
    page.locator("table.expenses td[data-field=description]").first.tap()  # a cell still selects
    assert page.locator("table.expenses td.sel-cell").count() == 1
    page.goto(f"{served}/activity?days=0")
    page.wait_for_selector("table.activity tbody tr")
    assert page.evaluate("getComputedStyle(document.querySelector('table.activity tbody tr')).display") == "grid"
    assert page.evaluate("getComputedStyle(document.querySelector('table.activity tbody td.what'), '::before').content") == '"What happened"'
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert page.evaluate(gap_after_rownum, "table.activity tbody tr.has-num") >= 0  # same 46px track
    row = page.locator("table.activity tbody tr.kind-edit")
    row.locator("button", has_text="details").tap()
    details = page.locator("table.activity tbody tr.kind-edit + tr.details-row")  # the row's own details
    assert details.is_visible() and "order id" in details.inner_text()
    # the Orders grid is still a sheet on a phone
    page.goto(f"{served}/orders")
    page.wait_for_selector("table.sheetlike tbody tr")
    assert page.evaluate("getComputedStyle(document.querySelector('table.sheetlike tbody tr')).display") == "table-row"
    assert page.errors == []


def test_a_card_saves_in_place_and_the_anniversary_field_follows_the_reset(served, page):
    """ "hide on (MM-DD)
    unless the reset is on a date each year"."""
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{served}/settings")
    page.wait_for_selector("#s-cards details.entry-card")
    page.evaluate("window.__loaded = true")  # a reload would lose it
    card = page.locator("#s-cards details.entry-card").first
    card.evaluate("d => d.open = true")
    day = card.locator("input[name='cap_all.anniversary']")
    assert not day.is_visible()  # calendar year: no date
    card.locator("details[data-param='cap_all.resets'] summary").click()
    card.locator("details[data-param='cap_all.resets'] label", has_text="on a date each year").click()
    assert day.is_visible()
    card.locator("details[data-param='cap_all.resets'] summary").click()
    card.locator("details[data-param='cap_all.resets'] label", has_text="each calendar year").click()
    assert not day.is_visible()
    card.locator("input[name='cashback_rate']").fill("3%")
    card.locator("button", has_text="Save card").click()
    page.wait_for_selector("#toast .toast.ok")  # the notification from the top
    assert "Saved card" in page.locator("#toast .toast.ok").inner_text()
    box = page.locator("#toast .toast.ok").bounding_box()
    assert box["y"] < 60
    assert page.evaluate("window.__loaded === true") and page.url.endswith("/settings")
    saved = page.locator("#s-cards details.entry-card").first
    assert saved.evaluate("d => d.open") and saved.locator("input[name='cashback_rate']").input_value() == "3%"
    # a retailer sits on one row: picking it on the Add row takes it from the row that had it, and the
    # other rows' pickers hide it
    add = saved.locator("details.multi[data-param$='.retailers']").last
    add.locator("summary").click()
    add.locator("label", has_text="Amazon").first.locator("input").check()
    add.locator("label", has_text="Amazon Business").locator("input").check()
    page.keyboard.press("Escape")
    saved.locator("input[name$='.rate']").last.fill("7%")  # a row needs a rate to be kept
    saved.locator("button", has_text="Save card").click()
    page.wait_for_selector("#toast .toast.ok")
    saved = page.locator("#s-cards details.entry-card").first
    pickers = saved.locator("details.multi[data-param$='.retailers']")
    page.wait_for_timeout(250)  # the pickers sync once the swapped section settles
    assert pickers.count() == 2  # the saved row and the Add row: both retailers on ONE row
    assert pickers.first.locator(".summary-value").inner_text() == "Amazon, Amazon Business"
    assert pickers.last.locator("label", has_text="Amazon Business").evaluate("l => l.hidden")  # held by the row above
    assert not pickers.last.locator("label", has_text="Best Buy").evaluate("l => l.hidden")
    pickers.last.locator("summary").click()
    pickers.last.locator("label", has_text="Best Buy").locator("input").check()
    page.keyboard.press("Escape")
    assert pickers.first.locator("label", has_text="Best Buy").evaluate("l => l.hidden")
    # and picking Amazon on the Add row (made visible for the test) takes it from the first row
    pickers.last.locator("summary").click()
    pickers.last.locator("label", has_text="Amazon").first.evaluate("l => l.hidden = false")
    pickers.last.locator("label", has_text="Amazon").first.locator("input").check()
    assert pickers.first.locator(".summary-value").inner_text() == "Amazon Business"
    # typing into the Add-a-retailer box is typing, not the menu's type-to-narrow
    box = pickers.last.locator("input.new-option")
    box.click()
    page.keyboard.type("Woo")
    assert box.input_value() == "Woo" and pickers.last.locator(".menu .narrow").count() == 0
    page.keyboard.press("Enter")
    assert pickers.last.locator("label", has_text="Woo").locator("input").is_checked()
    page.keyboard.press("Escape")  # close the picker before the next save
    # a refused save shows in place too (a 400 htmx would otherwise drop; user: "it did nothing")
    saved.locator("input[name='cashback_rate']").fill("2")
    saved.locator("button", has_text="Save card").click()
    page.wait_for_selector("#toast .toast.warn")
    assert "outside 0-1" in page.locator("#s-cards .banner.warn").inner_text()
    assert page.evaluate("window.__loaded === true")
    assert page.errors == []


def test_a_desktop_window_keeps_the_tabs(served, page):
    page.set_viewport_size({"width": 1400, "height": 800})
    page.goto(f"{served}/orders")
    page.wait_for_selector("header nav .tabs")
    assert not page.evaluate("document.querySelector('header.top').classList.contains('compact')")
    assert page.locator("header nav .tabs").is_visible() and not page.locator("header nav details.nav-pick").is_visible()
    # the Tools menu opens in full below the tabs (nothing clips it)
    page.locator("header nav .tabs details.nav-menu summary").click()
    menu = page.locator("header nav .tabs details.nav-menu .menu")
    assert menu.is_visible() and menu.locator("a").last.is_visible()
    box = menu.bounding_box()
    assert box["height"] > 150 and box["y"] > 30
    assert page.errors == []


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
    # hover: the page's a:hover underline must
    # not reach the SVG <a>, whose default black fill is what paints the decoration
    bar.hover()
    assert page.evaluate("el => getComputedStyle(el).textDecorationLine", bar.element_handle()) == "none"
    assert page.evaluate("el => getComputedStyle(el.querySelector('text.month')).textDecorationLine", bar.element_handle()) == "none"


def test_headers_row_numbers_and_esc_select_like_sheets(served, page, client):
    """a click on a column's NAME selects the column (the header marked too)
    instead of sorting; the sort is a double-click on the name or the menu's Sort items. A row
    number's right-click opens the whole menu over the row's cells, not only the delete. Esc still
    clears the selection after a click on blank page took the focus."""
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{served}/orders")
    page.wait_for_selector("table.sheetlike tbody tr")
    th = page.locator("table.sheetlike thead th.col-quantity")
    idx = page.evaluate("el => el.cellIndex", th.element_handle())
    th.locator(".name").click()
    page.wait_for_timeout(200)
    assert "sort=" not in page.url                       # selected, not sorted
    assert page.evaluate("document.querySelector('th.col-quantity').classList.contains('sel-col')")
    n_rows = page.locator("table.sheetlike tbody tr").count()
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == n_rows
    assert page.evaluate(f"document.querySelector('table.sheetlike tbody tr').cells[{idx}].classList.contains('sel-cell')")
    # a drag across the headers selects the columns crossed
    a = page.locator("table.sheetlike thead th.col-quantity .name").bounding_box()
    b = page.locator("table.sheetlike thead th.col-order_id .name").bounding_box()
    page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=6)
    page.mouse.up()
    assert page.locator("table.sheetlike thead th.sel-col").count() == 2
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == 2 * n_rows
    assert "sort=" not in page.url
    # the menu's sort: descending, and the URL follows
    th.click(button="right")
    page.locator(".ctx.on button[data-act=sort-desc]").click()
    page.wait_for_url("**sort=quantity*")
    assert "dir=desc" in page.url
    page.wait_for_selector("th.col-quantity.sorted")
    # the arrow is one click a step: after descending it clears the sort, then ascending
    page.locator("th.col-quantity a.sort").click()
    page.wait_for_url(lambda url: "sort=" not in url)
    page.wait_for_selector("th.col-quantity:not(.sorted)")
    page.locator("th.col-quantity a.sort").click()
    page.wait_for_url("**dir=asc*")
    # Ctrl+A on a grid without row ticks (this view-only Orders page) selects every cell -- straight
    # after the load, nothing clicked first (the browser's own select-all must not run)
    page.wait_for_selector("table.sheetlike tbody tr")
    page.locator("h1").click()
    page.keyboard.press("Control+a")
    assert page.evaluate("String(window.getSelection()).length") < 40, "the page's text got selected instead"
    n_rows = page.locator("table.sheetlike tbody tr").count()
    cols = page.evaluate("document.querySelector('table.sheetlike tbody tr').cells.length")
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == n_rows * (cols - 1)
    assert page.locator("table.sheetlike thead th.sel-col").count() == cols - 1  # the headers too
    page.keyboard.press("Escape")
    # a cell selected, a click on blank page, then Esc clears it
    page.wait_for_selector("table.sheetlike tbody tr")
    page.locator("table.sheetlike tbody tr").first.locator("td[data-field=quantity]").click()
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == 1
    page.locator("h1").click()
    assert not page.evaluate("!!document.activeElement.closest('table.sheetlike')")
    page.keyboard.press("Escape")
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == 0
    assert page.errors == []
    # a row number's right-click, on a grid with row selection (the staging sheet; the served Orders
    # page has no writer, so no row checkboxes): the row ticked, its cells selected, the whole menu
    csv_text = "Order Number,Date,Item,Qty,Status\nX1,3/11/2026,Widget,2,paid\nX2,3/12/2026,Gadget,1,paid\n"
    client.post("/tools/import/upload", files={"source": ("old.csv", csv_text.encode(), "text/csv")}, follow_redirects=False)
    client.post("/tools/import/map", data={"map.0": "order_id", "map.1": "order_date", "map.2": "item_name", "map.3": "quantity",
                                          "map.4": "status", "date_order": "", "profile": ""}, follow_redirects=False)
    assert client.post("/tools/import/run", follow_redirects=False).status_code == 303
    page.goto(f"{served}/tools/import")
    page.wait_for_selector("table.sheetlike tbody tr")
    row = page.locator("table.sheetlike tbody tr").nth(1)
    row.locator("td.rownum").click(button="right")
    assert row.evaluate("tr => tr.classList.contains('selected')")
    cols = page.evaluate("document.querySelector('table.sheetlike tbody tr').cells.length")
    assert row.locator("td.sel-cell").count() == cols - 1
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == cols - 1
    acts = page.locator(".ctx.on button[data-act]").evaluate_all("bs => bs.map(b => b.dataset.act)")
    for act in ("copy", "paste", "clear", "undo", "delete-rows"):
        assert act in acts, acts
    page.keyboard.press("Escape")
    assert not page.evaluate("document.querySelector('.ctx').classList.contains('on')")
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == 0
    assert page.locator("table.sheetlike tbody tr.selected").count() == 0
    # a plain click on a row number selects the row's cells too; Ctrl+C then copies the row
    page.locator("table.sheetlike tbody tr").first.locator("td.rownum").click()
    assert page.locator("table.sheetlike tbody tr.selected").count() == 1
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == cols - 1
    page.keyboard.press("Control+c")
    page.wait_for_timeout(150)
    copied = page.evaluate("(document.getElementById('grid-clipboard') || {}).value")
    assert "X1" in copied and "Widget" in copied
    # Ctrl+A ticks every row, as the # corner does
    page.keyboard.press("Control+a")
    assert page.locator("table.sheetlike tbody tr.selected").count() == 2
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == 2 * (cols - 1)
    assert page.locator("table.sheetlike thead th.sel-col").count() == cols - 1  # the headers too
    page.keyboard.press("Escape")
    assert page.locator("table.sheetlike thead th.sel-col").count() == 0
    # every cell of a column selected BY HAND does not mark the header
    cells = page.locator("table.sheetlike tbody td[data-field=quantity]")
    cells.first.click()
    cells.nth(1).click(modifiers=["Shift"])
    assert page.locator("table.sheetlike tbody td.sel-cell").count() == 2
    assert page.locator("table.sheetlike th.sel-col").count() == 0
    page.locator("table.sheetlike thead th.col-quantity").click()  # the staging sheet's headers are plain text
    assert page.locator("table.sheetlike th.sel-col").count() == 1
    page.keyboard.press("Escape")
    assert page.errors == []
    # the expenses grid sorts by plain links (esort / edir): the menu and the double-click follow them
    client.post("/taxes/expense", params={"year": "2026"}, follow_redirects=False,
                data={"date": "2026-03-01", "description": "Tape", "category": "supplies", "amount": "4.00", "profile": "alpha", "receipt_url": "https://x/r"})
    page.goto(f"{served}/taxes?year=2026")
    page.wait_for_selector("table.expenses tbody tr")
    page.locator("table.expenses thead th.col-amount").scroll_into_view_if_needed()
    page.wait_for_timeout(600)  # a scroll closes the menu: let the scroll into view settle first
    page.locator("table.expenses thead th.col-amount").click(button="right")
    page.locator(".ctx.on button[data-act=sort-asc]").click()
    page.wait_for_url("**esort=amount*")
    assert "edir=asc" in page.url
    page.locator("table.expenses thead th.col-amount .name").click()
    assert page.evaluate("document.querySelector('table.expenses th.col-amount').classList.contains('sel-col')")
    page.locator("table.expenses thead th.col-amount a.sort").click()
    page.wait_for_url("**edir=desc*")
    assert page.errors == []


def test_the_staging_sheet_edits_like_the_orders_grid(served, page, client):
    """Tools > Import's staging sheet (web/importer.py) is driven by the same edit.js as Orders:
    click-type-Enter writes a cell (and the gap outline goes), Ctrl+Z undoes it, Space toggles the
    tick cell, a row number plus Delete drops the row through the dialog."""
    csv_text = "Order Number,Date,Item,Qty,Status\nX1,3/11/2026,Widget,2,paid\n"
    client.post("/tools/import/upload", files={"source": ("old.csv", csv_text.encode(), "text/csv")}, follow_redirects=False)
    client.post("/tools/import/map", data={"map.0": "order_id", "map.1": "order_date", "map.2": "item_name", "map.3": "quantity",
                                          "map.4": "status", "date_order": "", "profile": ""}, follow_redirects=False)
    assert client.post("/tools/import/run", follow_redirects=False).status_code == 303
    page.set_viewport_size({"width": 1400, "height": 800})
    page.goto(f"{served}/tools/import")
    cell = 'td[data-field="retailer"][data-entry-id="r0001-1"]'
    assert page.evaluate(f"document.querySelector('{cell}').classList.contains('gap')")
    page.click(cell)
    page.keyboard.type("Costco")
    page.keyboard.press("Enter")
    page.wait_for_function(f"document.querySelector('{cell}').getAttribute('data-raw') === 'Costco'", timeout=3000)
    assert not page.evaluate(f"document.querySelector('{cell}').classList.contains('gap')")
    page.keyboard.press("Control+z")
    page.wait_for_function(f"document.querySelector('{cell}').getAttribute('data-raw') === ''", timeout=3000)
    tick = 'td[data-field="tracking_submitted"][data-entry-id="r0001-1"]'
    page.click(tick)
    page.keyboard.press("Space")
    page.wait_for_function(f"document.querySelector('{tick}').getAttribute('data-raw') === 'TRUE'", timeout=3000)
    page.click("td.rownum")
    page.keyboard.press("Delete")
    page.wait_for_selector("dialog#settings-confirm[open]", timeout=3000)
    page.click('dialog#settings-confirm button[value="ok"]')
    page.wait_for_url("**/tools/import**", timeout=5000)
    page.wait_for_selector('form[action="/tools/import/upload"]', timeout=5000)
    assert page.errors == []


def test_the_dated_log_totals_live_grows_a_row_and_saves(served, page):
    """the outside-spend log -- open the total, type an amount, the total follows,
    a fresh row appears, a removed row leaves the total, an outside click closes it, the save keeps it."""
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{served}/settings")
    page.wait_for_selector("#s-cards details.entry-card")
    card = page.locator("#s-cards details.entry-card").first
    card.evaluate("d => d.open = true")
    log = card.locator("details.log[data-log='cap_all.os']")
    assert not log.is_visible()  # no spend limit yet: no outside spend to log
    card.locator("input[name='cap_all.spend_limit']").fill("25000")
    assert log.is_visible()
    assert log.locator(".summary-value").inner_text() == "$0.00"
    log.locator("summary").click()
    assert log.evaluate("d => d.open")
    log.locator("input[name='cap_all.os.new.amount']").fill("4000")
    assert log.locator(".summary-value").inner_text() == "$4,000.00"
    assert log.locator("tr.entry").count() == 2  # the typed row is numbered now, a fresh new row follows
    assert log.locator("input[name='cap_all.os.0.amount']").input_value() == "4000"
    assert log.locator("input[name='cap_all.os.new.amount']").input_value() == ""
    log.locator("input[name='cap_all.os.new.amount']").fill("-500")
    assert log.locator(".summary-value").inner_text() == "$3,500.00" and log.locator("tr.entry").count() == 3
    log.locator("label.remove").first.click()  # the 4,000 entry ticked away
    assert log.locator(".summary-value").inner_text() == "-$500.00"
    log.locator("label.remove").first.click()  # and back
    assert log.locator(".summary-value").inner_text() == "$3,500.00"
    # the page's calendar on a date box: walking its month and year grids stays inside the log

    log.locator("input[name='cap_all.os.new.date']").click()
    page.wait_for_selector(".pop.cal")
    page.locator(".pop button[data-view='months']").click()
    page.wait_for_timeout(50)
    assert log.evaluate("d => d.open") and page.locator(".pop").count() == 1
    page.locator(".pop button[data-view='years']").click() if page.locator(".pop button[data-view='years']").count() else None
    page.wait_for_timeout(50)
    assert log.evaluate("d => d.open")
    page.keyboard.press("Escape")  # closes the calendar first
    page.wait_for_timeout(50)
    assert log.evaluate("d => d.open")
    page.mouse.click(5, 5)  # outside: closes
    assert not log.evaluate("d => d.open")
    card.locator("button", has_text="Save card").click()
    page.wait_for_selector("#toast .toast.ok")
    saved = page.locator("#s-cards details.entry-card").first.locator("details.log[data-log='cap_all.os']")
    assert saved.locator(".summary-value").inner_text() == "$3,500.00"
    assert saved.locator("input[name='cap_all.os.0.amount']").input_value() in ("-500", "4000")
    assert saved.locator("tr.entry").count() == 3
    # the menu stays inside the window: at a phone width the rates table scrolls sideways and would
    # clip an absolute menu; at the page's right edge it would run off it
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)  # the layout settles before the menu is placed
    log = page.locator("#s-cards details.entry-card").first.locator("details.log[data-log='cap_all.os']")
    log.scroll_into_view_if_needed()
    log.locator("summary").click()
    page.wait_for_timeout(100)  # the toggle event that places the menu is dispatched as its own task
    # what the placement chose: inside the 390px window, and the amount box usable
    style = log.locator(".menu").evaluate("m => ({left: parseFloat(m.style.left), top: parseFloat(m.style.top), width: m.getBoundingClientRect().width})")
    assert 8 <= style["left"] and style["left"] + style["width"] <= 390 and style["width"] > 200
    assert log.locator("input[name='cap_all.os.new.amount']").is_visible()
    page.mouse.wheel(0, 120)  # a page scroll: the fixed menu is placed again, under the summary
    page.wait_for_timeout(150)
    assert log.evaluate("d => d.open")
    moved = log.locator(".menu").evaluate("m => parseFloat(m.style.top)")
    assert moved != style["top"] or page.evaluate("window.scrollY") == 0
    assert page.errors == []


def test_the_taxes_inputs_save_in_place_on_enter(served, page):
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(f"{served}/taxes?year=2026")
    page.wait_for_selector("#tax-form")
    # label and amount stay in one glance on a wide window: the input tables cap at a readable
    # width instead of stretching the amount to the panel's far edge (2026-09-21)
    assert page.evaluate(
        "document.querySelector('#s-programs table.entry-table').getBoundingClientRect().width") <= 680
    page.evaluate("window.__loaded = true")  # a reload would lose it
    log = page.locator("#s-programs details.log").first
    log.locator("summary").click()
    log.locator("input[name$='.new.amount']").fill("30")
    page.keyboard.press("Enter")
    page.wait_for_selector("#toast .toast.ok")
    assert "Saved 2026" in page.locator("#toast .toast.ok").inner_text()
    assert page.evaluate("window.__loaded === true") and "/taxes" in page.url
    assert page.locator("#s-programs details.log").first.locator(".summary-value").inner_text() == "$30.00"
    assert page.locator("#s-schedule-c").count() == 1 and "$30.00" in page.locator("#s-schedule-c").inner_text()
    assert page.errors == []


def test_saving_one_card_keeps_another_cards_unsaved_edits(served, page):
    """change one card, change another, save one -- the other's edits stay."""
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{served}/settings")
    page.wait_for_selector("#s-cards details.entry-card")
    cards = page.locator("#s-cards > .entry-list > details.entry-card, #s-cards .entry-list[data-section='cards'] > details.entry-card")
    first, second = cards.nth(0), cards.nth(1)
    first.evaluate("d => d.open = true")
    second.evaluate("d => d.open = true")
    second.locator("input[name='name']").fill("Venmo Visa (edited, unsaved)")
    first.locator("input[name='cashback_rate']").fill("4%")
    first.locator("button", has_text="Save card").click()
    page.wait_for_selector("#toast .toast.ok")
    assert page.locator("#s-cards details.entry-card").first.locator("input[name='cashback_rate']").input_value() == "4%"
    assert second.locator("input[name='name']").input_value() == "Venmo Visa (edited, unsaved)"  # untouched by the swap
    assert page.errors == []


def test_the_dark_theme_keeps_filled_controls_and_grid_lines_readable(served, page):
    """2026-09-21: in dark mode a filled control (the accent) wore white text on the PALE dark
    accent -- unreadable -- and the sheet's translucent-black grid lines vanished on uncoloured
    rows. --on-accent and --grid-line-plain fix both; the pastel status rows keep the dark line."""
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{served}/orders")
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    page.wait_for_selector("table.sheetlike tbody tr")
    # a selected column's header: dark ink on the pale accent, never white
    page.locator("table.sheetlike thead th.col-quantity .name").click()
    page.wait_for_selector("th.col-quantity.sel-col")
    assert page.evaluate(
        "getComputedStyle(document.querySelector('th.sel-col')).color") == "rgb(21, 22, 26)"
    # a primary button's text follows the same token
    assert page.evaluate(
        """(() => { const b = document.createElement('button'); b.className = 'primary';
             document.body.appendChild(b); const c = getComputedStyle(b).color; b.remove(); return c; })()"""
    ) == "rgb(21, 22, 26)"
    # an uncoloured header cell draws a VISIBLE light grid line; a status row keeps the dark one
    assert page.evaluate(
        "getComputedStyle(document.querySelector('table.sheetlike thead th.col-quantity')).borderRightColor"
    ) == "rgba(255, 255, 255, 0.1)"
    assert page.evaluate(
        "getComputedStyle(document.querySelector('table.sheetlike tbody tr[class*=status-] td')).borderRightColor"
    ) == "rgba(0, 0, 0, 0.12)"
    # and the light theme still reads white on the accent
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    assert page.evaluate(
        "getComputedStyle(document.querySelector('th.sel-col')).color") == "rgb(255, 255, 255)"
    # the browser chrome's colour follows the toggle (theme-color mirrors --bg, 2026-09-21)
    assert page.evaluate("document.querySelector('meta[name=theme-color]').content") == "#f5f5f7"
    page.evaluate("toggleTheme()")
    assert page.evaluate("document.querySelector('meta[name=theme-color]').content") == "#161618"
    page.evaluate("toggleTheme()")
    assert page.errors == []


def test_the_chart_legends_stay_inside_their_cards_on_a_phone(served, phone):
    """2026-09-21: at 360px the donut legends' nowrap entries overflowed the card's right edge
    ("Amazon Business  1  11%" clipped outside Rows by Retailer). The legend shrinks and the
    label ellipsizes; the counts and percents stay whole."""
    phone.goto(f"{served}/")
    phone.wait_for_selector(".charts figure")
    overflow = phone.evaluate(
        """(() => {
             const out = [];
             for (const fig of document.querySelectorAll('.charts figure')) {
               const edge = fig.getBoundingClientRect().right;
               for (const li of fig.querySelectorAll('.legend li'))
                 if (li.getBoundingClientRect().right > edge + 0.5)
                   out.push(fig.getAttribute('aria-label') + ': ' + li.textContent.trim());
             }
             return out;
           })()""")
    assert overflow == [], overflow
    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert phone.errors == []


def test_a_360px_phone_fits_every_page_and_the_schedule_table_scrolls_inside_its_panel(served, page):
    """2026-09-21: at 360px (the session's phone floor; the Pixel 5 test above is 393) the Taxes
    page scrolled sideways -- the Schedule C table ran 21px past the viewport with no scroll
    container of its own. It scrolls inside the panel now, and no page pans sideways."""
    page.set_viewport_size({"width": 360, "height": 780})
    for path in ("/", "/orders?view=cards", "/taxes?year=2026", "/settings", "/activity", "/audit", "/tools/import"):
        page.goto(f"{served}{path}")
        page.wait_for_timeout(150)
        over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert over == 0, f"{path} sticks out {over}px past a 360px viewport"
    page.goto(f"{served}/taxes?year=2026")
    wrap = page.locator("#s-schedule-c .scroll")
    assert wrap.count() == 1
    assert page.evaluate("el => el.scrollWidth > el.clientWidth", wrap.element_handle())  # the table pans here
    assert page.errors == []


def test_the_taxes_page_prints_as_the_preparers_artifact(served, page):
    """2026-09-21: printed, the Taxes page is what the tax preparer receives. The chrome goes,
    the surfaces print white even from the dark theme, the schedule opens out of its scroll box,
    and a folded panel opens for the print and folds back after."""
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(f"{served}/taxes?year=2026")
    page.wait_for_selector("#s-schedule-c")
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    page.evaluate("document.querySelector('#s-programs').open = false")
    page.evaluate("window.dispatchEvent(new Event('beforeprint'))")
    assert page.evaluate("document.querySelector('#s-programs').open")  # opened for the print
    page.emulate_media(media="print")
    assert page.evaluate("getComputedStyle(document.querySelector('header.top')).display") == "none"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    assert page.evaluate("getComputedStyle(document.querySelector('#s-schedule-c .scroll')).overflow") == "visible"
    assert page.evaluate("getComputedStyle(document.querySelector('.tax-years form.open-year')).display") == "none"
    page.emulate_media(media="screen")
    page.evaluate("window.dispatchEvent(new Event('afterprint'))")
    assert not page.evaluate("document.querySelector('#s-programs').open")  # folded back
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    assert page.errors == []
