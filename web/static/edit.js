// The Orders table edits like a spreadsheet. No build step, no framework.
//
//   click            selects a cell (the active cell, outlined); shift-click or drag selects a range;
//                    Ctrl-click adds a cell (or takes a selected one out) and keeps the rest selected;
//                    a click on a column's header cell (the name included) selects the column, the
//                    header too, and a drag across the headers selects the columns crossed;
//                    the arrow at the header's right sorts by it, one click a step
//                    (the menu offers both directions); a click on a row number selects the row,
//                    its cells included; Ctrl+A selects every row (every cell where rows have no tick)
//   double-click     opens the editor (or press Enter, or just start typing: the keystroke replaces
//                    the value, as in Sheets)
//   Enter            saves; with a RANGE selected, fills every editable cell in it with the value
//   Esc              cancels the editor, or clears the selection (cells and rows both), also after
//                    a click elsewhere on the page took the focus
//   Delete/Backspace clears every editable selected cell
//   Ctrl+;           puts today's date into every selected date cell (as in Sheets)
//   Ctrl+Shift+H     toggles the hand-edit mark on the selection: marked cells are released (values
//                    kept, the runs may write them again), else the selected values are marked (no
//                    button for it)
//   Space            toggles a Tracking Submitted checkbox cell (click does too)
//   Ctrl+Z / Ctrl+Y  undo / redo the last accepted write (a range fill, a paste or Ctrl+; is one
//                    step; Ctrl+Shift+Z redoes too); the old value goes back through the same
//                    conflict-checked POST, so a cell someone changed meanwhile is refused, not clobbered
//   right-click      a menu of these actions, with their keys, on any cell, header cell or row number
//   touch            a tap selects, a second tap on the selected cell edits, a long press opens the
//                    menu; the selection's corner handle drags a range, a drag down the row numbers
//                    selects rows; the menu's Paste opens a box to paste into where the page may not
//                    read the clipboard (http)
//   a link in a cell a plain click selects the cell (copy works); Ctrl-click, middle-click or a
//                    double-click on a read-only cell opens it
//   Ctrl+C / Ctrl+V  copies the selection as tab-separated values (pastes into Sheets / Excel too),
//                    pastes a single value into every selected cell, or a block cell by cell from
//                    the top-left of the selection (through a hidden textarea, so it works on http)
//   arrows           move the active cell; with Shift, extend the selection
//
// Every write is one POST to /orders/cell with the row's key, the field, the value and what the
// cell showed before ("expected"), so a cell someone else changed meanwhile is refused as a
// conflict instead of overwritten. The server answers with the whole <td> re-rendered from a
// fresh read (or with data-error), and htmx swaps it in place. Writes run one after another.
(function () {
  "use strict";

  // ---- the grid: coordinates survive the swaps that replace cells ------------------------------
  // The Orders table and an order page's shipment tables are all grids; the selection lives in
  // ONE of them at a time (the one last clicked), and a cell swap keeps the table element.
  var GRID = "table.sheetlike, table.order-rows";
  // A touch screen (a phone, an iPad): taps instead of clicks, no hover, no keyboard until an
  // input has focus.
  var COARSE = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  var GRID_TD = "table.sheetlike td, table.order-rows td";  // NOT GRID + " td": the comma would split it
  var grid = null;     // the table the selection is in
  function table() { return grid && grid.isConnected ? grid : null; }
  function body() { var t = table(); return t ? t.tBodies[0] : null; }
  function inGrid(el) { return !!(el && el.closest && el.closest(GRID)); }
  function cellAt(r, c) {
    var b = body();
    var row = b && b.rows[r];
    return row ? row.cells[c] : null;
  }
  function coordsOf(td) {
    var tr = td.parentElement;
    return { r: tr.sectionRowIndex, c: td.cellIndex };
  }
  function selectable(td) {  // a hidden row (the Activity table's folded details) is never selected
    return !!td && td.tagName === "TD" && !td.classList.contains("rownum") && !(td.parentElement && td.parentElement.hidden);
  }
  function rowsChecked() { return !!document.querySelector('input[name="sel"]:checked'); }
  function editable(td) { return !!td && td.classList.contains("edit") && !td.hasAttribute("data-editing"); }

  var ranges = [];     // [{r1, c1, r2, c2}, ...]: the selection; the LAST one is what shift / drag extend
  var active = null;   // {r, c} or null
  var anchor = null;   // {r, c}: where a shift-click / drag range starts
  var dragging = false;

  function rect(a, b) {
    return { r1: Math.min(a.r, b.r), c1: Math.min(a.c, b.c), r2: Math.max(a.r, b.r), c2: Math.max(a.c, b.c) };
  }
  function lastRange() { return ranges.length ? ranges[ranges.length - 1] : null; }
  function forEachSelected(fn) {
    var seen = {};
    ranges.forEach(function (range) {
      for (var r = range.r1; r <= range.r2; r++) {
        for (var c = range.c1; c <= range.c2; c++) {
          if (seen[r + "," + c]) continue;
          seen[r + "," + c] = true;
          var td = cellAt(r, c);
          if (selectable(td)) fn(td, r, c);
        }
      }
    });
  }
  // Repaint the selection; `takeFocus` moves keyboard focus to the active cell. A repaint after an
  // unrelated swap never steals focus from a field the user is typing in, and a drag in progress
  // does not focus each cell it crosses (that would flash the tooltip on every one).
  function paint(takeFocus) {
    document.querySelectorAll("td.sel-cell").forEach(function (td) { td.classList.remove("sel-cell"); });
    document.querySelectorAll("th.sel-col").forEach(function (th) { th.classList.remove("sel-col"); });
    forEachSelected(function (td) { td.classList.add("sel-cell"); });
    if (COARSE) setTimeout(placeHandle, 0);  // after the focus and the layout settle
    // A column selected THROUGH ITS HEADER shows it on the header cell too; the same cells selected by hand do not ("if I select
    // all cells in a column I do not want to automatically select the column name too").
    var t = table(), rows = t && t.tBodies[0] ? t.tBodies[0].rows.length : 0, head = t && t.tHead ? t.tHead.rows[0] : null;
    if (head && rows) ranges.forEach(function (range) {
      // range.all: select-all on a table whose body holds hidden details rows (Activity) makes
      // one range PER visible row, so no range spans the body -- the flag says it was the whole
      // table all the same.
      if (!range.head) return;
      if (!range.all && (range.r1 !== 0 || range.r2 !== rows - 1)) return;
      for (var c = range.c1; c <= range.c2; c++) {
        var th = head.cells[c];
        if (th && !th.classList.contains("rownum")) th.classList.add("sel-col");
      }
    });
    updateSelStats();  // the bottom-right Sum / Count pill follows every selection change
    var td = active ? cellAt(active.r, active.c) : null;
    if (!td || dragging || document.activeElement === td || document.querySelector("input.cell-input")) return;
    var free = document.activeElement === document.body || inGrid(document.activeElement);
    // A cell that is not editable (Item Name, a view-only table) has no tabindex, so focus() did
    // nothing and the keyboard handler -- gated on focus being inside the grid -- ignored Ctrl+C
    // on it. -1: focusable, not in the tab order.
    if (!td.hasAttribute("tabindex")) td.setAttribute("tabindex", "-1");
    if (takeFocus || free) td.focus({ preventScroll: true });
  }
  // ---- touch: the selection's corner handle. A finger on a cell scrolls the table, so a range is
  // dragged from the handle at the selection's bottom-right corner, as in Sheets on a phone: the
  // range runs from the last range's top-left to the cell under the finger. ---------------------
  var handle = null;
  function selHandle() {
    if (handle) return handle;
    handle = document.createElement("div");
    handle.className = "sel-handle";
    handle.setAttribute("aria-hidden", "true");
    document.body.appendChild(handle);
    var from = null;
    handle.addEventListener("touchstart", function (e) {
      var r = lastRange();
      if (e.touches.length !== 1 || !r) return;
      e.preventDefault();  // no scroll, no emulated mouse events after
      from = { r: r.r1, c: r.c1 };
      anchor = from;
      dragging = true;
      handle.classList.add("dragging");  // no pointer events: elementFromPoint sees the cell under it
    }, { passive: false });
    handle.addEventListener("touchmove", function (e) {
      if (!dragging || !from) return;
      e.preventDefault();
      var t = e.touches[0];
      var el = document.elementFromPoint(t.clientX, t.clientY);
      var td = el && el.closest ? el.closest(GRID_TD) : null;
      if (!selectable(td) || td.closest(GRID) !== table()) return;
      var p = coordsOf(td);
      if (!active || p.r !== active.r || p.c !== active.c) { active = p; setSelection(from, p); }
    }, { passive: false });
    function end(e) {
      if (!dragging) return;
      e.preventDefault();
      dragging = false; from = null;
      handle.classList.remove("dragging");
      paint(true);
    }
    handle.addEventListener("touchend", end, { passive: false });
    handle.addEventListener("touchcancel", end, { passive: false });
    return handle;
  }
  function placeHandle() {
    if (!COARSE) return;
    var h = selHandle(), r = lastRange(), t = table();
    var td = r && t ? cellAt(r.r2, r.c2) : null;
    if (!td || document.querySelector("input.cell-input")) { h.classList.remove("on"); return; }
    var box = td.getBoundingClientRect();
    var seen = box.right > 0 && box.bottom > 0 && box.left < window.innerWidth && box.top < window.innerHeight;
    if (!seen) { h.classList.remove("on"); return; }
    // centred on the cell's corner (the box is 28px with its finger padding), kept inside the
    // viewport when the cell runs off it
    h.style.left = (Math.min(box.right, window.innerWidth - 6) - 14) + "px";
    h.style.top = (Math.min(box.bottom, window.innerHeight - 6) - 14) + "px";
    h.classList.add("on");
  }
  if (COARSE) {
    window.addEventListener("scroll", placeHandle, true);
    window.addEventListener("resize", placeHandle);
    document.addEventListener("focusin", function () { setTimeout(placeHandle, 0); });  // the editor opening hides it
    document.addEventListener("focusout", function () { setTimeout(placeHandle, 0); });
  }
  function rangeCount() { var n = 0; forEachSelected(function () { n++; }); return n; }
  function setSelection(a, b) {  // the last range becomes a..b (a first one is made)
    if (ranges.length) ranges[ranges.length - 1] = rect(a, b); else ranges.push(rect(a, b));
    paint(true);
  }
  // A plain click starts over with this cell; with `add` (Ctrl) the cell joins the selection.
  function selectOne(td, add) {
    var t = td.closest(GRID);
    if (!add || t !== grid) ranges = [];
    grid = t;
    var p = coordsOf(td);
    anchor = p; active = p;
    ranges.push(rect(p, p));
    paint(true);
  }
  // Ctrl-click: a selected single cell comes out of the selection; anything else joins it.
  // Returns whether a range was added (a drag can then extend it).
  function toggleOne(td) {
    var p = coordsOf(td);
    for (var i = 0; i < ranges.length; i++) {
      var g = ranges[i];
      if (g.r1 === p.r && g.r2 === p.r && g.c1 === p.c && g.c2 === p.c) {
        ranges.splice(i, 1);
        anchor = p; active = p;
        paint(true);
        return false;
      }
    }
    selectOne(td, true);
    return true;
  }
  function clearSelection() { ranges = []; anchor = null; paint(false); }
  function selectAll() {
    var t = table() || document.querySelector(GRID);
    if (!t || !t.tBodies[0] || !t.tBodies[0].rows.length) return;
    var all = t.querySelector("#sel-all");
    if (all) { all.checked = true; all.dispatchEvent(new Event("change", { bubbles: true })); return; }
    var rows = t.tBodies[0].rows.length, cols = t.tBodies[0].rows[0].cells.length;
    grid = t;
    ranges = [rect({ r: 0, c: 0 }, { r: rows - 1, c: cols - 1 })];
    ranges[0].head = true;
    active = active && cellAt(active.r, active.c) ? active : { r: 0, c: 0 };
    anchor = active;
    paint(true);
  }
  // The ticked rows ARE the cell selection, as in Sheets: a press on a row number selects its cells
  // too, so the menu's actions and the keys act on the row. Consecutive rows make one range; column 0 is the number.
  document.addEventListener("rows:changed", function (e) {
    var boxes = document.querySelectorAll('table.sheetlike tbody input[name="sel"]:checked');
    var t = boxes.length ? boxes[0].closest(GRID) : null;
    ranges = [];
    if (!t) { anchor = null; paint(false); return; }
    grid = t;
    var cols = t.tBodies[0].rows[0].cells.length, idx = [];
    boxes.forEach(function (box) { idx.push(box.closest("tr").sectionRowIndex); });
    idx.sort(function (a, b) { return a - b; });
    for (var i = 0; i < idx.length; i++) {
      var last = lastRange();
      if (last && last.r2 === idx[i] - 1) last.r2 = idx[i];
      else ranges.push(rect({ r: idx[i], c: 1 }, { r: idx[i], c: cols - 1 }));
    }
    // Select all (Ctrl+A, the # corner) is the whole table: the headers show it too; the same rows ticked by a drag or
    // by hand do not ("that should only be for ctrl a")
    if (e.detail && e.detail.all) ranges.forEach(function (range) { range.head = true; range.all = true; });
    var tr = e.detail && e.detail.tr;
    active = { r: tr ? tr.sectionRowIndex : idx[0], c: 1 };
    anchor = active;
    paint(true);
  });

  // ---- undo / redo -------------------------
  // Every write the server ACCEPTED goes on the undo stack with what the cell showed before; one
  // user action (a range fill, a paste, Ctrl+;) is one step. Undoing a step writes its old values
  // back, in reverse, through the same conflict-checked POST -- so a cell someone else changed
  // meanwhile is refused, not clobbered -- and the writes that succeed form the redo step. A cell
  // no longer on the page (the filter changed, the row went) is skipped. Added and deleted rows are
  // not undoable here. The stacks are this page load's: a full reload starts empty.
  var undoStack = [], redoStack = [], UNDO_LIMIT = 100;
  function newStep(mode) { return { mode: mode || "edit", entries: [] }; }
  // A row's key: the ledger's four-part upsert key on the Orders grid, an entry id on the Taxes
  // page's expenses grid (data-entry-id). A grid names where its cells post in data-cell-url.
  function keyOf(td) {
    return { order_id: td.getAttribute("data-order-id") || "", order_date: td.getAttribute("data-order-date") || "",
             item_name: td.getAttribute("data-item-name") || "", shipment: td.getAttribute("data-shipment") || "",
             entry_id: td.getAttribute("data-entry-id") || "", field: td.getAttribute("data-field") };
  }
  function cellUrl(td) {
    var t = td.closest ? td.closest("table") : null;
    return (t && t.getAttribute("data-cell-url")) || "/orders/cell";
  }
  function findCell(k) {  // the cell for a key + field as it is NOW (swaps replace the element)
    var tds = document.querySelectorAll('td[data-field="' + k.field + '"]');
    for (var i = 0; i < tds.length; i++) {
      var td = tds[i];
      if (!td.hasAttribute("data-order-id") && !td.hasAttribute("data-entry-id")) continue;
      var mine = keyOf(td);
      if (mine.order_id === k.order_id && mine.order_date === k.order_date && mine.item_name === k.item_name &&
          mine.shipment === k.shipment && mine.entry_id === k.entry_id) return td;
    }
    return null;
  }
  function recorded(step, entry) {
    if (!step.entries.length) {  // the step's first accepted write puts it on its stack
      if (step.mode === "undo") { redoStack.push(step); }
      else {
        undoStack.push(step);
        if (step.mode === "edit") redoStack = [];  // a fresh edit forks the history, as everywhere
        if (undoStack.length > UNDO_LIMIT) undoStack.shift();
      }
    }
    step.entries.push(entry);
  }
  function replay(from, mode) {  // revert one step: write each entry's `before` back, last first
    var step = from.pop();
    if (!step) return;
    var next = newStep(mode), first = null;
    step.entries.slice().reverse().forEach(function (en) {
      var td = findCell(en);
      if (!td || !editable(td)) return;
      if (!first) first = en;
      writeCell(td, en.before, next);
    });
    if (first) queue = queue.then(function () { var td = findCell(first); if (td) selectOne(td, true); });
  }
  function undo() { replay(undoStack, "undo"); }
  function redo() { replay(redoStack, "redo"); }

  // ---- writes, one after another ----------------------------------------------------------------
  var queue = Promise.resolve();
  function writeCell(td, value, step) {
    if (!editable(td)) return;
    var raw = td.getAttribute("data-raw") || "";
    if (value === raw) return;
    var key = keyOf(td);
    var values = {
      order_id: key.order_id,
      order_date: key.order_date,
      item_name: key.item_name,
      shipment: key.shipment,
      entry_id: key.entry_id,
      field: key.field,
      value: value,
      expected: raw,
      // the count line's switch: on = a hand edit the runs keep; off = a correction they may overwrite
      protect: (function () { var box = document.getElementById("protect-edits"); return box && !box.checked ? "0" : "1"; })()
    };
    var mine = step || newStep("edit");
    var url = cellUrl(td);
    td.classList.add("saving");
    queue = queue.then(function () {
      return htmx.ajax("POST", url, { target: td, swap: "outerHTML", values: values });
    }).then(function () {
      var fresh = findCell(key);
      if (!fresh || fresh.hasAttribute("data-error")) return;  // refused: nothing to undo
      recorded(mine, { order_id: key.order_id, order_date: key.order_date, item_name: key.item_name,
                       shipment: key.shipment, entry_id: key.entry_id, field: key.field, before: raw,
                       after: fresh.getAttribute("data-raw") || "" });
    }).catch(function () {});
  }
  function fillSelection(value) {
    var targets = [], step = newStep("edit");
    forEachSelected(function (td) { if (editable(td)) targets.push(td); });
    targets.forEach(function (td) { writeCell(td, value, step); });
  }
  function today() {
    var d = new Date();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  // Ctrl+Shift+H (or the right-click menu) TOGGLES the hand-edit mark on the selection: with hand-edited cells selected they are released -- value
  // kept, the runs may write them again; with none, every selected cell that holds a value is
  // marked as a hand edit. Not an undo step: no value changes.
  function handSelected() {
    var out = [];
    forEachSelected(function (td) { if (editable(td) && td.classList.contains("hand")) out.push(td); });
    return out;
  }
  function markable() {  // the selected cells a mark could go on: editable, with a value, not marked
    var out = [];
    forEachSelected(function (td) {
      if (editable(td) && !td.classList.contains("hand") && (td.getAttribute("data-raw") || "") !== "") out.push(td);
    });
    return out;
  }
  function postMark(tds, path) {
    tds.forEach(function (td) {
      var key = keyOf(td);
      td.classList.add("saving");
      queue = queue.then(function () {
        return htmx.ajax("POST", path, { target: td, swap: "outerHTML", values: key });
      }).catch(function () {});
    });
  }
  function releaseSelection() { postMark(handSelected(), "/orders/cell/release"); }
  function markSelection() { postMark(markable(), "/orders/cell/protect"); }
  function toggleHandSelection() { if (handSelected().length) releaseSelection(); else markSelection(); }
  function fillToday() {  // Ctrl+; -- every selected DATE cell gets today's date (one undo step)
    var targets = [], step = newStep("edit");
    forEachSelected(function (td) { if (editable(td) && td.getAttribute("data-kind") === "date") targets.push(td); });
    targets.forEach(function (td) { writeCell(td, today(), step); });
  }

  // ---- the editor --------------------------------------------------------------------------------
  // The column's previous answers come from static/picker.js (#cell-choices); the row's other
  // card field narrows Card Name / Card Last 4.
  function siblingRaw(td, field) {
    var tr = td.parentElement;
    var other = tr ? tr.querySelector('td[data-field="' + field + '"]') : null;
    return other ? (other.getAttribute("data-raw") || other.textContent || "").trim() : "";
  }
  function choicesFor(field, td) {
    if (!window.Picker) return [];
    return Picker.choicesFor(field, function (other) { return siblingRaw(td, other); });
  }
  function armed(td) {
    if (!td.getAttribute("data-original-html")) td.setAttribute("data-original-html", td.innerHTML);
    return td;
  }
  // Tracking Submitted is a real checkbox: a click, Space or Enter toggles it and
  // the cell writes TRUE / FALSE; there is no text editor to open on it.
  function toggleCheck(td) {
    var box = td.querySelector("input.cell-check");
    if (!box || !editable(td)) return false;
    writeCell(td, box.checked ? "FALSE" : "TRUE");
    return true;
  }
  document.addEventListener("change", function (e) {
    var box = e.target;
    if (!box || !box.classList || !box.classList.contains("cell-check")) return;
    var td = box.closest("td");
    if (td && editable(td)) writeCell(td, box.checked ? "TRUE" : "FALSE");
  });
  function startEdit(td, initial) {
    if (td.querySelector("input.cell-input")) return;
    if (td.getAttribute("data-kind") === "check") { toggleCheck(td); return; }
    armed(td);
    var raw = td.getAttribute("data-raw") || "";
    var field = td.getAttribute("data-field");
    var input = document.createElement("input");
    input.type = "text";
    input.value = initial !== undefined ? initial : raw;
    input.className = "cell-input";
    input.setAttribute("aria-label", "edit " + field);
    if (field === "status") input.placeholder = "ordered, shipped, delivered, cancelled, paid, return, superseded";
    td.setAttribute("data-editing", "1");
    td.innerHTML = "";
    td.appendChild(input);
    input.focus();
    if (initial === undefined) input.select();
    // The in-page picker for the cell's kind (static/picker.js): a calendar on a date cell, the
    // column's previous answers on a choice cell. A pick saves the cell, as Enter would.
    var kind = td.getAttribute("data-kind");
    if (window.Picker && kind === "date") Picker.date(input, function () { save(false); });
    else if (window.Picker && kind === "choice") Picker.choices(input, choicesFor(field, td), function () { save(false); }, initial);

    var done = false;
    function restore() {
      td.removeAttribute("data-editing");
      td.innerHTML = td.getAttribute("data-original-html") || "";
    }
    function cancel() {
      if (done) return;
      done = true;
      restore();
      td.focus({ preventScroll: true });
    }
    function save(thenDown) {
      if (done) return;
      done = true;
      var value = input.value;
      restore();
      // A range selected around this cell: the value goes into every editable cell of it.
      if (rangeCount() > 1) { fillSelection(value); return; }
      writeCell(td, value);
      if (thenDown) move(1, 0, false);  // Enter saves and steps down, as in Sheets
    }
    input.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === ";" && kind === "date") { e.preventDefault(); input.value = today(); return; }
      if (e.key === "Enter") { e.preventDefault(); save(true); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
      else if (e.key === "Tab") { e.preventDefault(); save(); move(0, e.shiftKey ? -1 : 1, false); }
    });
    input.addEventListener("blur", function () {
      if (input.value === raw) cancel(); else save();
    });
  }

  // A click on a column's HEADER CELL, its name included, selects the whole column, as in Sheets;
  //
  // Ctrl adds the column, Shift extends from the anchor's column. The arrow at the header's right
  // is the sort link (one click a step: ascending, descending, clear: "it takes
  // a lot of clicks to adjust the sort"); the menu's Sort items name a direction outright.
  function headerOf(el) {  // the grid's header cell under el, or null (the row-number corner is none)
    var th = el && el.closest ? el.closest("th") : null;
    return th && th.closest(GRID) && !th.classList.contains("rownum") ? th : null;
  }
  function headerAbove(td) {
    var t = td && td.closest(GRID);
    return t && t.tHead ? t.tHead.rows[0].cells[td.cellIndex] || null : null;
  }
  function sortLink(th) { return th ? th.querySelector("a.sort") : null; }
  // The header's own link, with the direction asked for ("" clears the sort). The Orders table
  // sorts by sort / dir through htmx, the expenses grid by esort / edir with a plain link: the
  // names are read off the header's links (one of them always carries them), the request goes
  // the way the link's own click would.
  function sortBy(th, dir) {
    var a = sortLink(th), m = th ? /(?:^|\s)col-([A-Za-z0-9_]+)/.exec(th.className) : null;
    if (!a || !m) return;
    var names = null;
    th.parentElement.querySelectorAll("a[href]").forEach(function (link) {
      if (names) return;
      var q = new URL(link.getAttribute("href"), window.location.href).searchParams;
      if (q.has("esort")) names = ["esort", "edir"]; else if (q.has("sort")) names = ["sort", "dir"];
    });
    if (!names) return;
    var viaHtmx = a.hasAttribute("hx-get");
    var u = new URL(a.getAttribute(viaHtmx ? "hx-get" : "href"), window.location.href);
    if (dir) { u.searchParams.set(names[0], m[1]); u.searchParams.set(names[1], dir); }
    else { u.searchParams.delete(names[0]); u.searchParams.delete(names[1]); }
    if (viaHtmx) htmx.ajax("GET", u.pathname + u.search, { source: a, target: a.getAttribute("hx-target") || "#orders-table" });
    else window.location.assign(u.href);
  }
  function selectColumn(th, add, extend) {
    var t = th.closest(GRID);
    var rows = t && t.tBodies[0] ? t.tBodies[0].rows.length : 0;
    if (!rows) return;
    var c = th.cellIndex;
    if (extend && anchor && t === grid) { ranges = []; grid = t; ranges.push(rect({ r: 0, c: anchor.c }, { r: rows - 1, c: c })); }
    else {
      if (!add || t !== grid) ranges = [];
      grid = t;
      anchor = { r: 0, c: c };
      ranges.push(rect({ r: 0, c: c }, { r: rows - 1, c: c }));
    }
    lastRange().head = true;  // selected through the header: paint marks the header cell
    active = { r: 0, c: c };
    document.dispatchEvent(new Event("rows:clear"));
    paint(true);
  }
  // ---- mouse: click selects, shift-click / drag extend ---------------------------------------------
  // A link inside a cell (an order id, a tracking number, "order ↗"): a plain click SELECTS the
  // cell -- so Ctrl+C copies it -- and Ctrl-click, the middle button or a double-click open the link.
  function cellLink(e) {
    var link = e.target.closest ? e.target.closest("a") : null;
    var td = link ? link.closest(GRID_TD) : null;
    return link && td && selectable(td) ? { link: link, td: td } : null;
  }
  function openLink(link) {
    var href = link.getAttribute("href") || "";
    if (!href) return;
    if (href.charAt(0) === "/") window.location.assign(href); else window.open(href, "_blank", "noopener");
  }
  document.addEventListener("click", function (e) {
    var hit = cellLink(e);
    if (hit && !(e.ctrlKey || e.metaKey || e.shiftKey) && e.button === 0) e.preventDefault();  // selected, not followed
  });
  document.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    var hit = cellLink(e);
    if (hit && !(e.ctrlKey || e.metaKey || e.shiftKey)) {
      e.preventDefault();
      selectOne(hit.td, false);
      document.dispatchEvent(new Event("rows:clear"));
      return;
    }
    var th = headerOf(e.target);
    if (th) {
      if (e.target.closest("a.sort")) return;  // the sort arrow: its own click, through htmx or the link
      e.preventDefault();
      selectColumn(th, e.ctrlKey || e.metaKey, e.shiftKey);
      colDrag = !(e.ctrlKey || e.metaKey);  // a drag across the headers extends from this column
      return;
    }
    if (e.target.closest && e.target.closest("a, button, input, .cell-edit, .cell-empty")) return;
    var td = e.target.closest ? e.target.closest(GRID_TD) : null;
    if (!selectable(td)) return;
    if (td.hasAttribute("data-editing")) return;
    var open = document.querySelector("input.cell-input");
    if (open) open.blur();  // commits or cancels the open editor first
    var same = td.closest(GRID) === table();
    // On a touch screen a tap on the cell that is ALREADY selected opens its editor (there is no
    // keyboard to "just type" with, and a double-tap zooms), as Sheets does on a phone.
    var again = COARSE && same && active && td === cellAt(active.r, active.c) && !e.shiftKey && !(e.ctrlKey || e.metaKey);
    var added = true;
    if (e.shiftKey && anchor && same) { active = coordsOf(td); setSelection(anchor, active); }
    else if ((e.ctrlKey || e.metaKey) && same) { added = toggleOne(td); }
    else { selectOne(td, false); document.dispatchEvent(new Event("rows:clear")); }
    dragging = added;  // set after the first paint, which focuses the clicked cell
    e.preventDefault();  // no text selection while dragging a range
    if (again && editable(td)) { dragging = false; startEdit(td); }
  });
  // A drag across the header cells selects the columns crossed, from the pressed column to the one under the pointer.
  var colDrag = false;
  document.addEventListener("mousemove", function (e) {
    if (!colDrag) return;
    var th = headerOf(e.target);
    if (!th || th.closest(GRID) !== table() || (active && th.cellIndex === active.c)) return;
    selectColumn(th, false, true);
  });
  document.addEventListener("mouseup", function () { colDrag = false; });
  // The same with a finger along the header row: the headers' touch-action keeps the vertical scroll and gives up the
  // sideways one, so a sideways drag there selects columns; a downward start is left to the scroll.
  var colFinger = null;
  document.addEventListener("touchstart", function (e) {
    var th = headerOf(e.target);
    colFinger = th && e.touches.length === 1 && !e.target.closest("a.sort") ? { th: th, x: e.touches[0].clientX, y: e.touches[0].clientY, on: false } : null;
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!colFinger) return;
    var t = e.touches[0];
    if (!colFinger.on) {
      var dx = Math.abs(t.clientX - colFinger.x), dy = Math.abs(t.clientY - colFinger.y);
      if (dx < 8) return;
      if (dy > dx) { colFinger = null; return; }  // downward: the page scrolls
      colFinger.on = true;
      selectColumn(colFinger.th, false, false);
    }
    e.preventDefault();
    var el = document.elementFromPoint(t.clientX, t.clientY);
    var th = headerOf(el);
    if (th && th.closest(GRID) === table() && !(active && th.cellIndex === active.c)) selectColumn(th, false, true);
  }, { passive: false });
  function colFingerUp(e) {
    if (!colFinger) return;
    if (colFinger.on) e.preventDefault();  // no emulated click after the drag
    colFinger = null;
  }
  document.addEventListener("touchend", colFingerUp, { passive: false });
  document.addEventListener("touchcancel", colFingerUp, { passive: false });
  document.addEventListener("mousemove", function (e) {
    if (!dragging) return;
    var td = e.target.closest ? e.target.closest(GRID_TD) : null;
    if (!selectable(td) || td.closest(GRID) !== table()) return;
    var p = coordsOf(td);
    if (!active || p.r !== active.r || p.c !== active.c) { active = p; setSelection(anchor, p); }
  });
  document.addEventListener("mouseup", function () { if (dragging) { dragging = false; paint(true); } });
  document.addEventListener("dblclick", function (e) {
    var td = e.target.closest ? e.target.closest("td.edit") : null;
    if (td && !td.hasAttribute("data-editing")) { startEdit(td); return; }
    var cell = e.target.closest ? e.target.closest(GRID_TD) : null;  // a read-only link cell: open it
    var link = cell && !cell.classList.contains("edit") ? cell.querySelector("a") : null;
    if (link) { e.preventDefault(); openLink(link); }
  });
  // A cell that shows a link cannot be double-clicked into (the first click follows the link),
  // so it carries a pencil; a blank link cell shows "add" and edits on a single click.
  document.addEventListener("click", function (e) {
    var handle = e.target.closest ? e.target.closest(".cell-edit, .cell-empty") : null;
    var td = handle ? handle.closest("td.edit") : null;
    if (td && !td.hasAttribute("data-editing")) { e.preventDefault(); selectOne(td, false); startEdit(td); }
  });

  // ---- keyboard ----------------------------------------------------------------------------------
  function move(dr, dc, extend) {
    if (!active) return;
    var r = active.r + dr, c = active.c + dc;
    var td = cellAt(r, c);
    if (!selectable(td)) return;
    active = { r: r, c: c };
    if (extend && anchor) setSelection(anchor, active);
    else { anchor = active; ranges = []; setSelection(active, active); }
    td.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function selectionTsv() {  // each range as a block of lines; several ranges one after another
    var lines = [];
    // Headers that are part of the selection (marked th.sel-col: select-all, or a column picked
    // through its header) copy as the first line; a hand-made selection has no marked header and
    // copies values alone.
    var headRow = table() && table().tHead ? table().tHead.rows[0] : null;
    if (headRow && headRow.querySelector("th.sel-col") && ranges.length) {
      var names = [];
      for (var hc = ranges[0].c1; hc <= ranges[0].c2; hc++) {
        var th = headRow.cells[hc], label = th ? th.querySelector(".name") : null;
        names.push(th && th.classList.contains("sel-col") ? (label ? label.textContent : th.textContent).trim() : "");
      }
      lines.push(names.join("\t"));
    }
    ranges.forEach(function (range) {
      for (var r = range.r1; r <= range.r2; r++) {
        var row = table() && table().tBodies[0] ? table().tBodies[0].rows[r] : null;
        if (row && row.hidden) continue;  // a folded details row
        var cells = [];
        for (var c = range.c1; c <= range.c2; c++) {
          var td = cellAt(r, c);
          cells.push(td ? (td.hasAttribute("data-raw") ? td.getAttribute("data-raw") : td.textContent.trim()) : "");
        }
        lines.push(cells.join("\t"));
      }
    });
    return lines.join("\n");
  }
  function pasteText(text) {
    var sel = lastRange();
    if (!sel || !text) return;
    var rows = text.replace(/\r/g, "").replace(/\n$/, "").split("\n").map(function (l) { return l.split("\t"); });
    if (rows.length === 1 && rows[0].length === 1) { fillSelection(rows[0][0]); return; }
    // A block: cell by cell from the top-left of the last range, as far as the table goes (one
    // undo step).
    var step = newStep("edit");
    rows.forEach(function (line, dr) {
      line.forEach(function (value, dc) {
        var td = cellAt(sel.r1 + dr, sel.c1 + dc);
        if (editable(td)) writeCell(td, value, step);
      });
    });
  }
  // ---- the clipboard ------------------------------------------------------------------------------
  // A hidden textarea carries Ctrl+C / Ctrl+V: the browser's own copy and paste act on it, which
  // works on plain http (the dashboard is reached over the LAN or the VPN) where navigator.clipboard
  // does not exist. Copy fills it and runs the copy command; paste focuses it so the browser pastes
  // there, and its paste event hands the text to the grid.
  var clip = null;
  function clipboard() {
    if (clip) return clip;
    clip = document.createElement("textarea");
    clip.id = "grid-clipboard";
    clip.setAttribute("aria-hidden", "true");
    clip.tabIndex = -1;
    clip.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none;";
    document.body.appendChild(clip);
    clip.addEventListener("paste", function (e) {
      var text = e.clipboardData ? e.clipboardData.getData("text/plain") : "";
      e.preventDefault();
      clip.value = "";
      hidePasteBox();
      pasteText(text);
      paint(true);
    });
    clip.addEventListener("input", function () {  // a keyboard that inserts without a paste event
      if (!clip.classList.contains("paste-box") || !clip.value) return;
      var text = clip.value;
      clip.value = "";
      hidePasteBox();
      pasteText(text);
      paint(true);
    });
    clip.addEventListener("keydown", function (e) { if (e.key === "Escape") { hidePasteBox(); paint(true); } });
    return clip;
  }
  // ---- the selection's stats. One fixed pill, bottom right,
  // shown from two selected cells: money and quantity columns SUM (with the average and how many
  // numbers), a *_rate column AVERAGES as a percent, Tracking Submitted counts its ticks, and
  // everything else counts non-empty cells. Hidden rows (folded details) stay out, like a copy. --
  var statsBox = null;
  function selStats() {
    if (statsBox) return statsBox;
    statsBox = document.createElement("div");
    statsBox.className = "sel-stats";
    statsBox.setAttribute("role", "status");
    document.body.appendChild(statsBox);
    return statsBox;
  }
  function fmtNum(value) {
    return value.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  }
  function updateSelStats() {
    var box = selStats(), t = table();
    var head = t && t.tHead ? t.tHead.rows[0] : null;
    var cells = 0, filled = 0, byField = {}, order = [];
    if (t && t.tBodies[0]) ranges.forEach(function (range) {
      for (var r = range.r1; r <= range.r2; r++) {
        var row = t.tBodies[0].rows[r];
        if (!row || row.hidden) continue;
        for (var c = range.c1; c <= range.c2; c++) {
          var td = cellAt(r, c);
          if (!td || td.classList.contains("rownum")) continue;
          cells++;
          var field = td.dataset.field || String(c);
          var check = field === "tracking_submitted" ? td.querySelector("input.cell-check") : null;
          if (check) { if (check.checked) filled++; continue; }
          var raw = td.hasAttribute("data-raw") ? td.getAttribute("data-raw") : (td.textContent || "").trim();
          if (raw) filled++;
          var isRate = field.slice(-5) === "_rate", value = NaN;
          if (isRate) {
            value = parseFloat(String(raw).replace("%", ""));
            if (!isNaN(value) && String(raw).indexOf("%") < 0 && value <= 1) value *= 100;
          } else if (td.classList.contains("num") && field !== "shipment") {
            var m = String(raw).replace(/,/g, "").match(/-?\d+(\.\d+)?/);  // "500.00 proj." reads 500
            if (m) value = parseFloat(m[0]);
          }
          if (isNaN(value)) continue;
          if (!byField[field]) {
            var th = head && head.cells[c], name = th && th.querySelector(".name");
            byField[field] = { label: (name ? name.textContent : th ? th.textContent : field).trim(),
                               sum: 0, n: 0, rate: isRate };
            order.push(field);
          }
          byField[field].sum += value;
          byField[field].n++;
        }
      }
    });
    if (cells < 2) { box.classList.remove("on"); return; }
    // Sum only where summing means ONE thing: one numeric column keeps Sum / Avg / Count, a
    // few columns show each column's own figure by name, more than three fall back to the count.
    var parts, group;
    if (order.length === 1) {
      group = byField[order[0]];
      parts = group.rate ? ["Avg " + fmtNum(group.sum / group.n) + "%", "Count " + group.n]
                         : ["Sum " + fmtNum(group.sum), "Avg " + fmtNum(group.sum / group.n), "Count " + group.n];
    } else if (order.length && order.length <= 3) {
      parts = order.map(function (field) {
        var g = byField[field];
        return g.label + " " + (g.rate ? fmtNum(g.sum / g.n) + "%" : fmtNum(g.sum));
      });
    } else {
      parts = ["Count " + filled];
    }
    box.textContent = parts.join(" · ");
    box.classList.add("on");
  }
  function copySelection() {
    var c = clipboard();
    c.value = selectionTsv();
    c.focus();
    c.select();
    try { document.execCommand("copy"); } catch (err) { /* nothing to do: the text stays in the box */ }
    paint(true);
  }
  function armPaste() {
    var c = clipboard();
    c.value = "";
    c.focus();  // the paste that follows the keystroke lands here
    setTimeout(function () {
      if (document.activeElement !== c) return;  // the paste event already handled it
      if (c.value) { pasteText(c.value); c.value = ""; }
      paint(true);
    }, 150);
  }
  document.addEventListener("keydown", function (e) {
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    var ctrl = e.ctrlKey || e.metaKey;
    // Undo / redo need no selection: they act on this page's last accepted write.
    if (ctrl && (e.key === "z" || e.key === "Z") && document.querySelector(GRID)) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if (ctrl && (e.key === "y" || e.key === "Y") && document.querySelector(GRID)) { e.preventDefault(); redo(); return; }
    // Ctrl+A selects every row, as the # corner does; a grid without row ticks
    // (a view-only table) gets every cell. It needs no selection first -- with a grid on the page
    // and the focus outside a text field, it is the grid's, never the browser's ("ctrl a still
    // highlights the page instead of selecting all visible cells").
    if (ctrl && (e.key === "a" || e.key === "A") && document.querySelector(GRID)) { e.preventDefault(); selectAll(); return; }
    var td = active ? cellAt(active.r, active.c) : null;
    // A click on blank page moved the focus off the grid; Esc still clears what is selected.
    //
    if (e.key === "Escape" && ranges.length && !inGrid(document.activeElement)) { clearSelection(); return; }
    if (!td || !inGrid(document.activeElement)) return;
    if (e.key === "ArrowUp") { e.preventDefault(); move(-1, 0, e.shiftKey); }
    else if (e.key === "ArrowDown") { e.preventDefault(); move(1, 0, e.shiftKey); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); move(0, -1, e.shiftKey); }
    else if (e.key === "ArrowRight") { e.preventDefault(); move(0, 1, e.shiftKey); }
    else if (e.key === "Tab") { e.preventDefault(); move(0, e.shiftKey ? -1 : 1, false); }
    else if (e.key === "Enter") { e.preventDefault(); if (editable(td)) startEdit(td); }
    else if (e.key === " " && td.getAttribute("data-kind") === "check") { e.preventDefault(); toggleCheck(td); }
    else if (e.key === "Escape") { e.preventDefault(); clearSelection(); }
    else if ((e.key === "Delete" || e.key === "Backspace") && rowsChecked()) { /* the row selection owns them: see below */ }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); fillSelection(""); }
    else if (ctrl && e.key === ";") { e.preventDefault(); fillToday(); }
    else if (ctrl && e.shiftKey && (e.key === "h" || e.key === "H")) { e.preventDefault(); toggleHandSelection(); }
    else if (ctrl && (e.key === "c" || e.key === "C")) { e.preventDefault(); copySelection(); }
    else if (ctrl && (e.key === "v" || e.key === "V")) { armPaste(); }  // not prevented: the paste must happen
    else if (!ctrl && !e.altKey && e.key.length === 1 && editable(td)) {
      e.preventDefault();
      startEdit(td, e.key);  // typing replaces the value, as in Sheets
    }
  });
  // After a swap: a refused edit is surfaced where it happened; the selection is repainted onto
  // the new cells (a whole-table swap on a filter change starts with nothing selected).
  document.addEventListener("htmx:afterSwap", function (e) {
    var td = e.target && e.target.matches && e.target.matches("td[data-error]") ? e.target : null;
    if (td) { td.focus(); }
    if (e.target && e.target.id === "orders-table") { ranges = []; anchor = null; active = null; }
    if (window.Picker) Picker.forget();  // #cell-choices may just have been re-rendered out of band
    paint(false);
  });

  // ---- a receipt uploaded from the table ---------------------------------------
  // The receipt cell's ⤒ opens the file dialog; the chosen file posts to the order's upload route
  // through a hidden htmx form (multipart, with the page's filters), which answers with the table
  // re-rendered and a notice, exactly as a cell edit does.
  document.addEventListener("click", function (e) {
    var button = e.target.closest ? e.target.closest(".cell-upload") : null;
    if (!button) return;
    e.preventDefault();
    var plain = button.getAttribute("data-upload-url");  // an expense's receipt: a plain POST, the page reloads
    var orderId = button.getAttribute("data-order-id");
    var form = document.createElement("form");
    form.hidden = true;
    if (plain) {
      form.method = "post";
      form.action = plain;
      form.enctype = "multipart/form-data";
      form.innerHTML = '<input type="file" name="receipt_file" accept=".pdf,.png,.jpg,.jpeg,.webp,image/*,application/pdf">';
      document.body.appendChild(form);
      var chosen = form.querySelector("input[type=file]");
      chosen.addEventListener("change", function () { if (chosen.files.length) form.submit(); else form.remove(); });
      chosen.click();
      return;
    }
    form.setAttribute("hx-post", "/orders/" + encodeURIComponent(orderId) + "/receipt");
    form.setAttribute("hx-target", "#orders-table");
    form.setAttribute("hx-swap", "outerHTML");
    form.setAttribute("hx-encoding", "multipart/form-data");
    form.setAttribute("hx-include", "#filters");
    form.innerHTML = '<input type="hidden" name="next" value="table">' +
      '<input type="file" name="receipt_file" accept=".pdf,.png,.jpg,.jpeg,.webp,image/*,application/pdf">';
    document.body.appendChild(form);
    var file = form.querySelector("input[type=file]");
    file.addEventListener("change", function () {
      if (!file.files.length) { form.remove(); return; }
      htmx.process(form);
      form.addEventListener("htmx:afterRequest", function () { form.remove(); });
      htmx.trigger(form, "submit");
    });
    file.click();
  });

  // ---- the right-click menu: the grid's actions, each with its key, on any cell or
  // header cell; a cell outside the selection is selected first, as in Sheets. The editor's own
  // input keeps the browser's menu. -------------------------------------------------------------
  var ctx = null;
  function ctxMenu() {
    if (ctx) return ctx;
    ctx = document.createElement("div");
    ctx.className = "ctx";
    ctx.setAttribute("role", "menu");
    document.body.appendChild(ctx);
    ctx.addEventListener("click", function (e) {
      var b = e.target.closest ? e.target.closest("button[data-act]") : null;
      if (!b || b.disabled) return;
      // A long press opens the menu UNDER the finger (clamped into the window, so a low row's
      // menu sits over the finger); the release's synthetic click then lands on whichever item is
      // there. That click is the one that opened the menu, not a choice (2026-09-22: the staging
      // sheet's third row, near the bottom of a phone, ran Select all and closed the menu at once).
      if (ctxByTouch && Date.now() - ctxOpenedAt < 600) return;
      var act = b.getAttribute("data-act");
      hideCtx();
      runCtx(act);
    });
    return ctx;
  }
  function hideCtx() { if (ctx) ctx.classList.remove("on"); }
  function ctxItem(act, label, keys, disabled) {
    return '<button type="button" role="menuitem" data-act="' + act + '"' + (disabled ? " disabled" : "") + ">" +
      "<span>" + label + "</span>" + (keys ? '<span class="kbd">' + keys + "</span>" : "") + "</button>";
  }
  function pasteFromClipboard() {  // the menu's Paste: the clipboard API where the page may read it (https), else the Ctrl+V box
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function (text) { pasteText(text); paint(true); }, function () { COARSE ? showPasteBox() : armPaste(); });
    } else if (COARSE) { showPasteBox(); } else { armPaste(); }
  }
  // A phone with no keyboard shortcut and no clipboard API (http): the hidden clipboard textarea
  // is shown as a box to long-press and Paste into; its paste event (or the text arriving) fills
  // the selection, and the box goes away.
  function showPasteBox() {
    var c = clipboard();
    c.value = "";
    c.classList.add("paste-box");
    c.setAttribute("placeholder", "long-press here, then Paste");
    c.style.cssText = "";
    c.focus();
  }
  function hidePasteBox() {
    if (!clip || !clip.classList.contains("paste-box")) return;
    clip.classList.remove("paste-box");
    clip.removeAttribute("placeholder");
    clip.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none;";
  }
  function selectRowOf(td) {  // the row-number cell's click, as the row selection understands it
    var num = td.parentElement ? td.parentElement.querySelector("td.rownum") : null;
    if (!num) return;
    num.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 0 }));
    document.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  }
  var ctxHead = null;  // the header cell the open menu's Sort items act on
  function runCtx(act) {
    var td = active ? cellAt(active.r, active.c) : null;
    if (act === "sort-asc") { sortBy(ctxHead, "asc"); return; }
    if (act === "sort-desc") { sortBy(ctxHead, "desc"); return; }
    if (act === "sort-clear") { sortBy(ctxHead, ""); return; }
    if (act === "open") { var a = td && td.querySelector("a"); if (a) openLink(a); }
    else if (act === "edit") { if (td && editable(td)) startEdit(td); }
    else if (act === "copy") copySelection();
    else if (act === "paste") pasteFromClipboard();
    else if (act === "clear") fillSelection("");
    else if (act === "today") fillToday();
    else if (act === "undo") undo();
    else if (act === "redo") redo();
    else if (act === "hand") toggleHandSelection();
    else if (act === "column") { var t = td && td.closest(GRID); var th = t && t.tHead ? t.tHead.rows[0].cells[td.cellIndex] : null; if (th) selectColumn(th, false, false); }
    else if (act === "all") selectAll();
    else if (act === "row") { if (td) selectRowOf(td); }
    else if (act === "delete-rows") { var button = document.getElementById("delete-selected"); if (button) button.click(); }
  }
  var ctxOpenedAt = 0, ctxByTouch = false;  // ctxByTouch: the long-press path sets it before opening, the contextmenu path clears it
  function openMenuAt(target, x, y) {  // the menu for the cell or header cell under (x, y); false when none applies
    var td = target.closest ? target.closest(GRID_TD) : null;
    var th = td ? null : headerOf(target);
    if (!td && !th) { hideCtx(); return false; }
    var onRow = !!(td && td.classList.contains("rownum"));
    if (td && !onRow && !td.classList.contains("sel-cell")) { selectOne(td, false); document.dispatchEvent(new Event("rows:clear")); }
    if (th && !th.classList.contains("sel-col")) selectColumn(th, false, false);
    if (onRow) { var box = td.querySelector('input[name="sel"]'); if (box && !box.checked) selectRowOf(td); }
    // The row number's menu is the cell menu over the row's cells, plus the delete.
    var cur = active ? cellAt(active.r, active.c) : null;
    var rows = rowsChecked();  // with rows ticked the Delete key deletes them, so Clear shows no key
    var anyDate = false, anyEditable = false;
    forEachSelected(function (c) { if (editable(c)) { anyEditable = true; if (c.getAttribute("data-kind") === "date") anyDate = true; } });
    var grid_ = (td || th).closest(GRID);
    var ledger = !!(grid_ && grid_.querySelector("td[data-order-id]"));
    var marked = handSelected().length, plain = markable().length;
    ctxHead = th || headerAbove(cur);
    var sorted = !!(ctxHead && ctxHead.classList.contains("sorted"));
    var html = "";
    html += ctxItem("edit", "Edit", "Enter", !(cur && editable(cur)));
    html += ctxItem("open", "Open link", "Ctrl+click", !(cur && cur.querySelector("a")));
    html += ctxItem("copy", "Copy", "Ctrl+C") + ctxItem("paste", "Paste", "Ctrl+V", !anyEditable);
    html += ctxItem("clear", "Clear", rows ? "" : "Delete", !anyEditable) + ctxItem("today", "Fill with today", "Ctrl+;", !anyDate);
    html += "<hr>" + ctxItem("undo", "Undo", "Ctrl+Z", !undoStack.length) + ctxItem("redo", "Redo", "Ctrl+Y", !redoStack.length);
    if (ledger) html += "<hr>" + ctxItem("hand", marked ? "Release hand edits" : "Mark as hand edits", "Ctrl+Shift+H", !marked && !plain);
    if (sortLink(ctxHead)) html += "<hr>" + ctxItem("sort-asc", "Sort ascending", "") + ctxItem("sort-desc", "Sort descending", "") + ctxItem("sort-clear", "Clear sort", "", !sorted);
    html += "<hr>" + ctxItem("column", "Select column", "", !cur) + ctxItem("row", "Select row", "", !(cur && cur.parentElement.querySelector("td.rownum")));
    html += ctxItem("all", "Select all", "Ctrl+A");
    if (rows && document.getElementById("delete-selected")) html += "<hr>" + ctxItem("delete-rows", "Delete selected row(s)", "Delete");
    var m = ctxMenu();
    m.innerHTML = html;
    m.classList.add("on");
    ctxOpenedAt = Date.now();
    var w = m.offsetWidth, h = m.offsetHeight, pad = 6;
    m.style.left = Math.max(pad, Math.min(x, window.innerWidth - w - pad)) + "px";
    m.style.top = Math.max(pad, Math.min(y, window.innerHeight - h - pad)) + "px";
    return true;
  }
  document.addEventListener("contextmenu", function (e) {
    if (!e.target.closest) return;
    if (e.target.closest("input, textarea")) return;                 // the editor: the browser's menu
    if (e.target.closest("a") && !headerOf(e.target)) return;         // a link too; the header's sort link is ours
    ctxByTouch = false;
    if (openMenuAt(e.target, e.clientX, e.clientY)) e.preventDefault();
  });
  // A long press on a touch screen opens the same menu (iOS never fires contextmenu); the touch's
  // own synthetic click and mousedown, which follow the release, must not close it at once.
  var press = null;
  document.addEventListener("touchstart", function (e) {
    if (e.touches.length !== 1) { press = null; return; }
    var t = e.touches[0], target = e.target;
    if (!(target.closest && (target.closest(GRID_TD) || target.closest("th")))) return;
    press = { x: t.clientX, y: t.clientY, target: target, timer: setTimeout(function () {
      press = null;
      ctxByTouch = true;
      openMenuAt(target, t.clientX, t.clientY);
    }, 550) };
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!press) return;
    var t = e.touches[0];
    if (Math.abs(t.clientX - press.x) > 8 || Math.abs(t.clientY - press.y) > 8) { clearTimeout(press.timer); press = null; }
  }, { passive: true });
  document.addEventListener("touchend", function () { if (press) { clearTimeout(press.timer); press = null; } }, { passive: true });
  document.addEventListener("touchcancel", function () { if (press) { clearTimeout(press.timer); press = null; } }, { passive: true });
  document.addEventListener("mousedown", function (e) {
    if (!ctx || !ctx.classList.contains("on") || ctx.contains(e.target)) return;
    if (Date.now() - ctxOpenedAt < 600) return;  // the tap that opened it, echoed as a mouse event
    hideCtx();
  }, true);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideCtx(); }, true);
  // A scroll closes the menu -- unless it is the one the opening itself caused: the browser nudges
  // a focused cell into view on the mousedown that opened the menu when the page can scroll
  // (2026-09-21: the Import page grew a directory, its main became scrollable, and the row
  // number's menu closed the instant it opened). The mousedown path keeps the same grace.
  window.addEventListener("scroll", function () { if (Date.now() - ctxOpenedAt < 600) return; hideCtx(); }, true);
  window.addEventListener("resize", hideCtx);
})();

// Row selection for delete: the header checkbox ticks every row shown, the toolbar's counter
// follows the ticks (it lives in the filter bar, outside the swapped table) and words the one
// confirmation, and the Delete key with rows selected asks it.
(function () {
  "use strict";
  function count() {
    var n = document.querySelectorAll('input[name="sel"]:checked').length;
    var out = document.getElementById("sel-count");
    if (out) out.textContent = String(n);
    var button = document.getElementById("delete-selected");
    if (button) {
      // The wording is the button's own (data-confirm-one / -many, {n} = the count); the Orders
      // grid asks through htmx's hx-confirm, a plain form (the expenses grid) through its
      // data-confirm and the page's dialog.
      var one = button.getAttribute("data-confirm-one") || "Delete the selected row from the ledger? This cannot be undone here.";
      var many = button.getAttribute("data-confirm-many") || "Delete the {n} selected rows from the ledger? This cannot be undone here.";
      var text = n === 1 ? one : many.replace("{n}", String(n));
      button.setAttribute("hx-confirm", text);
      if (button.form && button.form.hasAttribute("data-confirm")) button.form.setAttribute("data-confirm", text);
    }
  }
  function clearRows() {
    var boxes = document.querySelectorAll('input[name="sel"]:checked');
    if (!boxes.length) return false;
    boxes.forEach(function (box) { box.checked = false; });
    var all = document.getElementById("sel-all");
    if (all) all.checked = false;
    document.dispatchEvent(new Event("rows:cleared"));
    count();
    return true;
  }
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "Escape") { if (clearRows()) e.preventDefault(); return; }
    if (e.key !== "Delete" && e.key !== "Backspace") return;
    var button = document.getElementById("delete-selected");
    if (!button || !document.querySelector('input[name="sel"]:checked')) return;
    e.preventDefault();
    button.click();  // htmx asks hx-confirm through the page's own dialog, then posts
  });
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "sel-all") {
      document.querySelectorAll('input[name="sel"]').forEach(function (box) { box.checked = e.target.checked; });
    }
    if (e.target && (e.target.id === "sel-all" || e.target.name === "sel")) {
      count();
      // the cell selection follows the ticks (edit.js's grid listens)
      document.dispatchEvent(new CustomEvent("rows:changed", { detail: { tr: e.target.name === "sel" ? e.target.closest("tr") : null,
                                                                          all: e.target.id === "sel-all" && e.target.checked } }));
    }
  });
  document.addEventListener("rows:clear", clearRows);  // a plain click on a cell
  document.addEventListener("htmx:afterSwap", count);
  document.addEventListener("DOMContentLoaded", count);
})();

// Sheets-style row selection: press a row number to select that row (its checkbox ticks and the
// row tints) and nothing else; Ctrl-press adds it (or takes a selected one out) and keeps the rest;
// shift-press selects the range from the last press; a drag down the numbers selects every row the
// pointer crosses.
(function () {
  "use strict";
  var last = null;
  var press = null;  // the drag in progress: {tr, moved, rows, on: [rows this drag turned on]}
  function rowsShown() { return Array.prototype.slice.call(document.querySelectorAll("table.sheetlike tbody tr")); }
  function setRow(tr, on) {
    var box = tr.querySelector('input[name="sel"]');
    if (!box) return;
    box.checked = on;
    tr.classList.toggle("selected", on);
  }
  function syncClasses() {
    rowsShown().forEach(function (tr) {
      var box = tr.querySelector('input[name="sel"]');
      tr.classList.toggle("selected", !!(box && box.checked));
    });
  }
  function changed(tr) {
    var box = tr.querySelector('input[name="sel"]');
    if (box) box.dispatchEvent(new Event("change", { bubbles: true }));
  }
  document.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    var td = e.target.closest ? e.target.closest("td.rownum") : null;
    if (!td) return;
    var tr = td.parentElement;
    var box = tr.querySelector('input[name="sel"]');
    if (!box) return;
    e.preventDefault();  // no text selection while dragging down the numbers
    var rows = rowsShown();
    if (e.shiftKey && last && rows.indexOf(last) >= 0) {
      var a = rows.indexOf(last), b = rows.indexOf(tr);
      var from = Math.min(a, b), to = Math.max(a, b);
      for (var i = from; i <= to; i++) setRow(rows[i], true);
      press = null;
    } else if (e.ctrlKey || e.metaKey) {
      setRow(tr, !box.checked);  // joins, or leaves, the selection; the rest stays
      press = box.checked ? { tr: tr, moved: false, rows: rows, on: [tr] } : null;
    } else {
      // Clicking the ONLY selected row again deselects it; with others selected
      // a plain click narrows to this row, as in Sheets, and the next click deselects it. The
      // deselect waits for mouseup so a drag that starts on it still selects a range.
      var alone = box.checked && document.querySelectorAll('input[name="sel"]:checked').length === 1;
      document.dispatchEvent(new Event("rows:clear"));   // this row, and nothing else (its cells follow the tick)
      setRow(tr, true);
      press = { tr: tr, moved: false, rows: rows, on: [tr], toggleOff: alone };
    }
    last = tr;
    changed(tr);
  });
  document.addEventListener("mousemove", function (e) {
    if (!press) return;
    var td = e.target.closest ? e.target.closest("td.rownum") : null;
    var tr = td ? td.parentElement : null;
    if (!tr || tr === press.tr && !press.moved) return;
    var rows = press.rows;
    var a = rows.indexOf(press.tr), b = rows.indexOf(tr);
    if (a < 0 || b < 0) return;
    press.moved = true;
    var from = Math.min(a, b), to = Math.max(a, b);
    // Rows this drag turned on that the pointer has left go back off; the range comes on.
    press.on = press.on.filter(function (row) {
      var i = rows.indexOf(row);
      if (i >= from && i <= to) return true;
      setRow(row, false);
      return false;
    });
    for (var i = from; i <= to; i++) {
      var box = rows[i].querySelector('input[name="sel"]');
      if (box && !box.checked) { setRow(rows[i], true); press.on.push(rows[i]); }
    }
    last = tr;
    changed(tr);
  });
  document.addEventListener("mouseup", function () {
    if (press && press.toggleOff && !press.moved) { setRow(press.tr, false); changed(press.tr); }
    press = null;
  });
  // A finger dragged DOWN the row numbers selects the rows it crosses: the numbers' touch-action keeps sideways scrolling and gives up the
  // vertical one, and the drag is replayed as the mouse events above. A tap stays a tap.
  var finger = null;
  document.addEventListener("touchstart", function (e) {
    var td = e.target.closest ? e.target.closest("table.sheetlike td.rownum") : null;
    finger = td && e.touches.length === 1 ? { td: td, x: e.touches[0].clientX, y: e.touches[0].clientY, on: false } : null;
  }, { passive: true });
  document.addEventListener("touchmove", function (e) {
    if (!finger) return;
    var t = e.touches[0];
    if (!finger.on) {
      var dx = Math.abs(t.clientX - finger.x), dy = Math.abs(t.clientY - finger.y);
      if (dy < 8) return;
      if (dx > dy) { finger = null; return; }  // sideways: the table scrolls
      finger.on = true;
      finger.td.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 0 }));
    }
    e.preventDefault();
    var el = document.elementFromPoint(t.clientX, t.clientY);
    var over = el && el.closest ? el.closest("table.sheetlike td.rownum") : null;
    if (over) over.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
  }, { passive: false });
  function fingerUp(e) {
    if (!finger) return;
    if (finger.on) { document.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })); e.preventDefault(); }
    finger = null;
  }
  document.addEventListener("touchend", fingerUp, { passive: false });
  document.addEventListener("touchcancel", fingerUp, { passive: false });
  document.addEventListener("change", function (e) {
    if (e.target && (e.target.name === "sel" || e.target.id === "sel-all")) syncClasses();
  });
  document.addEventListener("htmx:afterSwap", function () { last = null; syncClasses(); });
  document.addEventListener("rows:cleared", syncClasses);
})();

// The scroll region's visible width, for the blocks above a wide table that stick to its left
// edge: they must be exactly as wide as the scrollport, which 100vw is not when a classic
// vertical scrollbar takes its 15px. Measured on load, resize and every swap.
(function () {
  "use strict";
  function measure() {
    var main = document.querySelector("body.wide main");
    if (main) document.documentElement.style.setProperty("--scrollport", main.clientWidth + "px");
  }
  document.addEventListener("DOMContentLoaded", measure);
  window.addEventListener("resize", measure);
  document.addEventListener("htmx:afterSwap", measure);
  measure();
})();

// The header's tabs fold into one dropdown when they do not fit the window: the tabs row is
// measured with the class off, and header.compact shows the pages dropdown instead.
(function () {
  "use strict";
  var head = document.querySelector("header.top");
  var tabs = head ? head.querySelector("nav .tabs") : null;
  if (!tabs) return;
  function fit() {
    head.classList.remove("compact");
    if (tabs.scrollWidth > tabs.clientWidth + 1) head.classList.add("compact");
  }
  fit();
  window.addEventListener("resize", fit);
  window.addEventListener("orientationchange", fit);
})();

// The # header is the select-all handle (the header checkbox is hidden, like the row ones).
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var th = e.target.closest ? e.target.closest("th.rownum") : null;
    if (!th) return;
    var all = document.getElementById("sel-all");
    if (!all) return;
    e.preventDefault();
    // Like Sheets' corner: anything selected -> clear the selection; nothing -> select every row.
    var any = document.querySelector('input[name="sel"]:checked');
    all.checked = !any;
    all.dispatchEvent(new Event("change", { bubbles: true }));
  });
})();

// In-page confirmation: every hx-confirm question is routed to the <dialog id="confirm"> instead
// of the browser's own prompt. htmx fires
// htmx:confirm before any request that carries hx-confirm; cancel it, show the dialog, and issue
// the request only on Confirm. Pages without the dialog fall back to htmx's default.
(function () {
  "use strict";
  document.addEventListener("htmx:confirm", function (e) {
    var question = e.detail && e.detail.question;
    var dialog = document.getElementById("confirm");
    if (!question || !dialog || typeof dialog.showModal !== "function") return;
    e.preventDefault();
    document.getElementById("confirm-text").textContent = question;
    var ok = document.getElementById("confirm-ok");
    var cancel = document.getElementById("confirm-cancel");
    function done(confirmed) {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onClose);
      if (dialog.open) dialog.close();
      if (confirmed) e.detail.issueRequest(true);
    }
    function onOk() { done(true); }
    function onCancel() { done(false); }
    function onClose() { done(false); }
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.showModal();
    ok.focus();
  });
})();

// Switching the view (table / cards) or the page size reloads the whole page rather than swapping
// the table: the bulk bar and the per-page control live outside the swapped region.
(function () {
  "use strict";
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.form || (t.form.id !== "filters" && t.form.id !== "eper-form" && t.form.id !== "iper-form")) return;
    if (t.name !== "view" && t.name !== "per" && t.name !== "eper" && t.name !== "iper") return;
    e.stopPropagation();
    var page = t.form.querySelector('input[name="page"]');
    if (page) page.value = "";
    t.form.submit();
  }, true);
})();

// Multi-select filter dropdowns: "All" clears every value box; ticking a value unticks "All";
// unticking the last value re-ticks "All". The summary in the closed dropdown follows. The form's
// own change trigger (htmx) re-queries the table after each tick.
(function () {
  "use strict";
  function refresh(details) {
    var all = details.querySelector(".all-box");
    var boxes = Array.prototype.slice.call(details.querySelectorAll('input[name]'));
    // the summary words a pick by its shown name where the box carries one (data-text: the page's
    // pick_many dropdowns), else by its value
    var picked = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.dataset.text || b.value; });
    if (all) all.checked = picked.length === 0;
    var text = details.querySelector(".summary-value") || details.querySelector(".summary-text");
    var empty = details.dataset.empty || "all";  // a "Hide" dropdown reads "none" when nothing is ticked
    if (text) text.textContent = picked.length === 0 ? empty : (picked.length <= 2 ? picked.join(", ") : picked.length + " selected");
  }
  document.addEventListener("change", function (e) {
    var details = e.target.closest ? e.target.closest("details.multi") : null;
    if (!details) return;
    if (details.classList.contains("single")) {
      // A single-choice dropdown (radios): show the pick, close the menu; the form's own change
      // trigger does the rest.
      var label = e.target.closest("label");
      var value = details.querySelector(".summary-value");
      if (label && value) value.textContent = label.textContent.trim();
      details.removeAttribute("open");
      return;
    }
    if (e.target.classList.contains("all-box")) {
      details.querySelectorAll('input[name]').forEach(function (b) { b.checked = false; });
      e.target.checked = true;
    }
    refresh(details);
  }, true);
  // Close an open dropdown when clicking elsewhere.
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.multi[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  // Type to narrow: with a dropdown open, printable keys build a filter shown at the top of the menu and
  // the labels that do not contain it hide; Backspace edits it, Escape clears it (a second Escape
  // closes the menu). Closing forgets it. No prompt is shown before anything is typed.
  function narrowLine(details, wanted) {  // the "narrow: …" line exists only while something is typed
    var menu = details.querySelector(".menu");
    var line = menu ? menu.querySelector(".narrow") : null;
    if (!wanted) { if (line) line.remove(); return null; }
    if (menu && !line) {
      line = document.createElement("div");
      line.className = "narrow muted small";
      menu.insertBefore(line, menu.firstChild);
    }
    return line;
  }
  function applyNarrow(details) {
    var typed = (details.dataset.narrow || "").toLowerCase();
    var line = narrowLine(details, !!typed);
    if (line) line.textContent = "narrow: " + details.dataset.narrow;
    details.querySelectorAll(".menu label").forEach(function (label) {
      if (label.classList.contains("all")) return;
      label.hidden = !!typed && label.textContent.toLowerCase().indexOf(typed) < 0;
    });
  }
  document.addEventListener("toggle", function (e) {
    var details = e.target;
    if (!details.matches || !details.matches("details.multi") || details.classList.contains("nav-menu")) return;
    details.dataset.narrow = "";
    applyNarrow(details);
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var details = e.target.closest ? e.target.closest("details.multi[open]") : null;
    if (!details || details.classList.contains("nav-menu")) return;  // the Tools menu is links, not choices
    if (e.key === " " && e.target.tagName === "INPUT") return;  // Space still ticks a focused box
    if (e.target.tagName === "INPUT" && e.target.type === "text" && e.key !== "Escape") return;  // the Add-a-retailer box keeps its keys; Escape still closes
    var typed = details.dataset.narrow || "";
    if (e.key === "Escape") {
      if (!typed) { details.removeAttribute("open"); return; }
      details.dataset.narrow = "";
    } else if (e.key === "Backspace") {
      if (!typed) return;
      details.dataset.narrow = typed.slice(0, -1);
    } else if (e.key.length === 1) {
      details.dataset.narrow = typed + e.key;
    } else {
      return;
    }
    e.preventDefault();
    applyNarrow(details);
  });
})();

// Times in the viewer's own zone: every <time class="local" datetime="..."> is re-rendered from its UTC stamp,
// as "YYYY-MM-DD HH:MM:SS" (or "MM-DD HH:MM" for a run stamp). The server text stays for no-JS.
(function () {
  "use strict";
  function two(n) { return (n < 10 ? "0" : "") + n; }
  function render(el) {
    var d = new Date(el.getAttribute("datetime"));
    if (isNaN(d.getTime())) return;
    var date = d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate());
    var time = two(d.getHours()) + ":" + two(d.getMinutes());
    el.textContent = el.classList.contains("run") ? two(d.getMonth() + 1) + "-" + two(d.getDate()) + " " + time
                                                  : date + " " + time + ":" + two(d.getSeconds());
    el.title = el.getAttribute("datetime") + " (UTC)";
  }
  function all() { document.querySelectorAll("time.local").forEach(render); }
  document.addEventListener("DOMContentLoaded", all);
  document.addEventListener("htmx:afterSwap", all);
  if (document.readyState !== "loading") all();
})();

// File drop zones (templates/_dropzone.html): a drop puts the file into the hidden input, the
// chosen name shows in place, and a zone marked data-autosubmit submits its form at once.
(function () {
  "use strict";
  function show(zone, input) {
    var out = zone.querySelector(".dz-file");
    var name = input.files && input.files.length ? Array.prototype.map.call(input.files, function (f) { return f.name; }).join(", ") : "";
    if (out) out.textContent = name;
    zone.classList.toggle("has-file", !!name);
  }
  document.addEventListener("change", function (e) {
    var input = e.target;
    var zone = input && input.closest ? input.closest(".dropzone") : null;
    if (!zone || input.type !== "file") return;
    show(zone, input);
    if (zone.dataset.autosubmit === "1" && input.files && input.files.length && input.form) {
      if (input.form.requestSubmit) input.form.requestSubmit(); else input.form.submit();
    }
  });
  ["dragenter", "dragover"].forEach(function (name) {
    document.addEventListener(name, function (e) {
      var zone = e.target.closest ? e.target.closest(".dropzone") : null;
      if (!zone) return;
      e.preventDefault();
      zone.classList.add("dragover");
    });
  });
  document.addEventListener("dragleave", function (e) {
    var zone = e.target.closest ? e.target.closest(".dropzone") : null;
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("dragover");
  });
  document.addEventListener("drop", function (e) {
    var zone = e.target.closest ? e.target.closest(".dropzone") : null;
    if (!zone) return;
    e.preventDefault();
    zone.classList.remove("dragover");
    var input = zone.querySelector('input[type="file"]');
    if (!input || !e.dataTransfer || !e.dataTransfer.files.length) return;
    try { input.files = e.dataTransfer.files; } catch (err) { return; }
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
})();

// The profile session's countdown on the Tools page.
(function () {
  "use strict";
  var el = document.querySelector("time.countdown");
  if (!el) return;
  var left = parseInt(el.dataset.seconds || "0", 10);
  function show() {
    var m = Math.floor(left / 60), s = left % 60;
    el.textContent = left <= 0 ? "now" : (m + " min " + (s < 10 ? "0" : "") + s + " s");
  }
  show();
  setInterval(function () { if (left > 0) { left -= 1; show(); if (left === 0) location.reload(); } }, 1000);
})();

// New content gets the page's entrance (style.css, "Motion"): whatever an htmx swap put on the
// page is tagged .just-in for one animation, then untagged so the next swap into the same target
// runs it again. A polled refresh (hx-trigger="every ...", the running tool job) is left alone --
// it would fade every two seconds -- and so is an out-of-band button.
(function () {
  "use strict";
  document.addEventListener("htmx:afterSwap", function (e) {
    var el = e.target;
    if (!el || !el.classList || el.matches('[hx-trigger*="every"]')) return;
    el.classList.remove("just-in");
    void el.offsetWidth;  // restart the animation when the same element is swapped again
    el.classList.add("just-in");
    el.addEventListener("animationend", function done(ev) {
      if (ev.target !== el) return;
      el.classList.remove("just-in");
      el.removeEventListener("animationend", done);
    });
  });
})();

// --- The dated log (web/templates/_amount_log.html): the summary total follows
// the rows as they are typed (only rows inside the period when the log names one, never a row
// ticked for removal); once the row being added has an amount it becomes a numbered row and a
// fresh one follows; an outside click or Escape closes the log. The page's own calendar
// (picker.js, `data-date`) serves the date boxes, so a click inside it is not "outside".
(function () {
  "use strict";
  function within(details, day) {
    var start = details.dataset.periodStart, end = details.dataset.periodEnd;
    if (!start || !end || !day) return true;
    return day >= start && day <= end;
  }
  function money(sum) {
    return (sum < 0 ? "-$" : "$") + Math.abs(sum).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function amountOf(tr) {  // a row's amount, or null when it is blank, not a number, or ticked away
    var remove = tr.querySelector("input[name$='.remove']");
    if (remove && remove.checked) return null;
    var amount = tr.querySelector("input[name$='.amount']");
    var value = parseFloat((amount ? amount.value : "").replace(/[$,\s]/g, ""));
    return isNaN(value) ? null : value;
  }
  function total(details) {
    var sum = 0, byMonth = {};
    details.querySelectorAll("tr.entry").forEach(function (tr) {
      var day = tr.querySelector("input[name$='.date']"), when = day ? day.value.trim() : "";
      tr.dataset.month = when.slice(0, 7);  // a re-dated row moves month
      var value = amountOf(tr);
      if (value === null) return;
      byMonth[when.slice(0, 7)] = (byMonth[when.slice(0, 7)] || 0) + value;
      if (within(details, when)) sum += value;
    });
    var out = details.querySelector(".summary-value");
    if (out) out.textContent = money(sum);
    var byYear = {};
    Object.keys(byMonth).forEach(function (month) { byYear[month.slice(0, 4)] = (byYear[month.slice(0, 4)] || 0) + byMonth[month]; });
    details.querySelectorAll("tr.month").forEach(function (tr) {  // the month folds' subtotals
      var cell = tr.querySelector(".sum");
      if (cell) cell.textContent = money(byMonth[tr.dataset.month] || 0);
    });
    details.querySelectorAll("tr.year").forEach(function (tr) {  // and the years'
      var cell = tr.querySelector(".sum");
      if (cell) cell.textContent = money(byYear[tr.dataset.year] || 0);
    });
  }
  function applyFolds(details) {  // a folded year hides its months and entries; a folded month its entries
    var years = {}, months = {};
    details.querySelectorAll("tr.year.folded").forEach(function (tr) { years[tr.dataset.year] = true; });
    details.querySelectorAll("tr.month").forEach(function (tr) {
      tr.hidden = !!years[tr.dataset.year];
      if (tr.classList.contains("folded")) months[tr.dataset.month] = true;
    });
    details.querySelectorAll("tr.entry").forEach(function (tr) {
      if (tr.classList.contains("new")) return;
      var month = tr.dataset.month || "";
      tr.hidden = !!(years[month.slice(0, 4)] || months[month]);
    });
  }
  function grow(details, tr) {
    var n = parseInt(tr.dataset.index || "0", 10), prefix = details.dataset.log;
    var fresh = tr.cloneNode(true);
    tr.classList.remove("new");
    tr.removeAttribute("data-index");
    tr.querySelectorAll("input").forEach(function (input) { input.name = input.name.replace(prefix + ".new.", prefix + "." + n + "."); });
    var cell = tr.lastElementChild, label = document.createElement("label"), box = document.createElement("input");
    cell.textContent = "";
    label.className = "remove"; label.title = "remove this entry on save";
    box.type = "checkbox"; box.name = prefix + "." + n + ".remove"; box.value = "on";
    label.appendChild(box);
    var mark = document.createElement("span"); mark.textContent = "\u2715"; label.appendChild(mark);
    cell.appendChild(label);
    fresh.dataset.index = String(n + 1);
    fresh.querySelectorAll("input").forEach(function (input) { if (/\.(amount|note)$/.test(input.name)) input.value = ""; });
    tr.after(fresh);
  }
  document.addEventListener("input", function (e) {
    var details = e.target.closest ? e.target.closest("details.log") : null;
    if (!details) return;
    var tr = e.target.closest("tr.entry");
    if (tr && tr.classList.contains("new") && /\.amount$/.test(e.target.name || "") && e.target.value.trim()) grow(details, tr);
    total(details);
  });
  document.addEventListener("change", function (e) {
    var details = e.target.closest ? e.target.closest("details.log") : null;
    if (details) total(details);
  });
  document.addEventListener("click", function (e) {
    // the calendar serving a date box: a click inside it -- or on one of its month / year buttons,
    // which it re-renders (the button is detached by now) -- is not a click outside the log
    //
    if (!e.target.isConnected || (e.target.closest && e.target.closest(".pop"))) return;
    var fold = e.target.closest ? e.target.closest("details.log tr.month, details.log tr.year") : null;
    if (fold) {  // a month's or a year's row folds what is under it away and back
      fold.classList.toggle("folded");
      applyFolds(fold.closest("details.log"));
      return;
    }
    document.querySelectorAll("details.log[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (document.querySelector(".pop.on")) return;  // the calendar is up: Escape closes it first
    var details = e.target.closest ? e.target.closest("details.log[open]") : null;
    if (details) { details.removeAttribute("open"); e.preventDefault(); }
  }, true);  // capture: before picker.js has closed the calendar on the same key
  // The menu is position: fixed (a sideways-scrolling table or the page's right edge would clip an
  // absolute one): on open it hangs under the summary, pulled left to stay inside the window, and
  // a scroll anywhere but inside the menu places it again, since a fixed box does not follow the page.
  function place(details) {
    var menu = details.querySelector(".menu"), summary = details.querySelector("summary");
    if (!menu || !summary) return;
    var at = summary.getBoundingClientRect();
    // measured, not assumed: an animated ancestor (a card htmx just swapped in) makes a fixed box
    // position against it, so the menu is put at 0,0 first and moved by the difference
    menu.style.top = "0px";
    menu.style.left = "0px";
    var zero = menu.getBoundingClientRect(), width = zero.width;
    var left = Math.max(8, Math.min(at.left, window.innerWidth - width - 8));
    menu.style.left = Math.round(left - zero.left) + "px";
    menu.style.top = Math.round(at.bottom + 4 - zero.top) + "px";
    menu.style.maxHeight = Math.max(120, Math.min(340, window.innerHeight - at.bottom - 12)) + "px";
  }
  document.addEventListener("toggle", function (e) {
    var details = e.target;
    if (!details.matches || !details.matches("details.log")) return;
    if (details.open) place(details);
  }, true);
  document.addEventListener("scroll", function (e) {
    document.querySelectorAll("details.log[open]").forEach(function (d) {
      var menu = d.querySelector(".menu");
      if (e.target === document || !(menu && menu.contains(e.target))) place(d);
    });
  }, true);
  window.addEventListener("resize", function () {
    document.querySelectorAll("details.log[open]").forEach(place);
  });
})();

// ---- Printing (2026-09-21): a printed page shows everything its panels hold. The folded
// panels open for the print and fold back after; the print stylesheet hides the chrome.
(function () {
  window.addEventListener("beforeprint", function () {
    document.querySelectorAll("details.panel:not([open])").forEach(function (d) {
      d.dataset.printOpened = "1";
      d.open = true;
    });
  });
  window.addEventListener("afterprint", function () {
    document.querySelectorAll("details.panel[data-print-opened]").forEach(function (d) {
      delete d.dataset.printOpened;
      d.open = false;
    });
  });
})();
