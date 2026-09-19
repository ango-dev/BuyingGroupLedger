// The Orders table edits like a spreadsheet. No build step, no framework.
//
//   click            selects a cell (the active cell, outlined); shift-click or drag selects a range
//   double-click     opens the editor (or press Enter, or just start typing: the keystroke replaces
//                    the value, as in Sheets)
//   Enter            saves; with a RANGE selected, fills every editable cell in it with the value
//   Esc              cancels the editor, or clears the selection (cells and rows both)
//   Delete/Backspace clears every editable selected cell
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
  function selectable(td) { return !!td && td.tagName === "TD" && !td.classList.contains("rownum"); }
  function rowsChecked() { return !!document.querySelector('input[name="sel"]:checked'); }
  function editable(td) { return !!td && td.classList.contains("edit") && !td.hasAttribute("data-editing"); }

  var sel = null;      // {r1, c1, r2, c2} or null
  var active = null;   // {r, c} or null
  var anchor = null;   // {r, c}: where a shift-click / drag range starts
  var dragging = false;

  function forEachSelected(fn) {
    if (!sel) return;
    for (var r = sel.r1; r <= sel.r2; r++) {
      for (var c = sel.c1; c <= sel.c2; c++) {
        var td = cellAt(r, c);
        if (selectable(td)) fn(td, r, c);
      }
    }
  }
  // Repaint the selection; `takeFocus` moves keyboard focus to the active cell. A repaint after an
  // unrelated swap never steals focus from a field the user is typing in, and a drag in progress
  // does not focus each cell it crosses (that would flash the tooltip on every one).
  function paint(takeFocus) {
    document.querySelectorAll("td.sel-cell").forEach(function (td) { td.classList.remove("sel-cell"); });
    forEachSelected(function (td) { td.classList.add("sel-cell"); });
    var td = active ? cellAt(active.r, active.c) : null;
    if (!td || dragging || document.activeElement === td || document.querySelector("input.cell-input")) return;
    var free = document.activeElement === document.body || inGrid(document.activeElement);
    if (takeFocus || free) td.focus({ preventScroll: true });
  }
  function rangeCount() { return sel ? (sel.r2 - sel.r1 + 1) * (sel.c2 - sel.c1 + 1) : 0; }
  function setSelection(a, b) {
    sel = { r1: Math.min(a.r, b.r), c1: Math.min(a.c, b.c), r2: Math.max(a.r, b.r), c2: Math.max(a.c, b.c) };
    paint(true);
  }
  function selectOne(td) {
    grid = td.closest(GRID);
    var p = coordsOf(td);
    anchor = p; active = p;
    setSelection(p, p);
  }
  function clearSelection() { sel = null; anchor = null; paint(false); }

  // ---- writes, one after another ----------------------------------------------------------------
  var queue = Promise.resolve();
  function writeCell(td, value) {
    if (!editable(td)) return;
    var raw = td.getAttribute("data-raw") || "";
    if (value === raw) return;
    var values = {
      order_id: td.getAttribute("data-order-id"),
      order_date: td.getAttribute("data-order-date"),
      item_name: td.getAttribute("data-item-name"),
      shipment: td.getAttribute("data-shipment"),
      field: td.getAttribute("data-field"),
      value: value,
      expected: raw
    };
    td.classList.add("saving");
    queue = queue.then(function () {
      return htmx.ajax("POST", "/orders/cell", { target: td, swap: "outerHTML", values: values });
    }).catch(function () {});
  }
  function fillSelection(value) {
    var targets = [];
    forEachSelected(function (td) { if (editable(td)) targets.push(td); });
    targets.forEach(function (td) { writeCell(td, value); });
  }

  // ---- the editor --------------------------------------------------------------------------------
  function armed(td) {
    if (!td.getAttribute("data-original-html")) td.setAttribute("data-original-html", td.innerHTML);
    return td;
  }
  function startEdit(td, initial) {
    if (td.querySelector("input")) return;
    armed(td);
    var raw = td.getAttribute("data-raw") || "";
    var field = td.getAttribute("data-field");
    var input = document.createElement("input");
    input.type = "text";
    input.value = initial !== undefined ? initial : raw;
    input.className = "cell-input";
    input.setAttribute("aria-label", "edit " + field);
    if (field === "tracking_submitted") input.placeholder = "TRUE / FALSE";
    if (field === "status") input.placeholder = "ordered, shipped, delivered, cancelled, paid, return, superseded";
    td.setAttribute("data-editing", "1");
    td.innerHTML = "";
    td.appendChild(input);
    input.focus();
    if (initial === undefined) input.select();

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
      if (e.key === "Enter") { e.preventDefault(); save(true); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
      else if (e.key === "Tab") { e.preventDefault(); save(); move(0, e.shiftKey ? -1 : 1, false); }
    });
    input.addEventListener("blur", function () {
      if (input.value === raw) cancel(); else save();
    });
  }

  // ---- mouse: click selects, shift-click / drag extend ---------------------------------------------
  document.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest("a, button, input, .cell-edit, .cell-empty")) return;
    var td = e.target.closest ? e.target.closest(GRID_TD) : null;
    if (!selectable(td)) return;
    if (td.hasAttribute("data-editing")) return;
    var open = document.querySelector("input.cell-input");
    if (open) open.blur();  // commits or cancels the open editor first
    if (e.shiftKey && anchor && td.closest(GRID) === table()) { active = coordsOf(td); setSelection(anchor, active); }
    else selectOne(td);
    dragging = true;  // set after the first paint, which focuses the clicked cell
    e.preventDefault();  // no text selection while dragging a range
  });
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
    if (td && !td.hasAttribute("data-editing")) startEdit(td);
  });
  // A cell that shows a link cannot be double-clicked into (the first click follows the link),
  // so it carries a pencil; a blank link cell shows "add" and edits on a single click.
  document.addEventListener("click", function (e) {
    var handle = e.target.closest ? e.target.closest(".cell-edit, .cell-empty") : null;
    var td = handle ? handle.closest("td.edit") : null;
    if (td && !td.hasAttribute("data-editing")) { e.preventDefault(); selectOne(td); startEdit(td); }
  });

  // ---- keyboard ----------------------------------------------------------------------------------
  function move(dr, dc, extend) {
    if (!active) return;
    var r = active.r + dr, c = active.c + dc;
    var td = cellAt(r, c);
    if (!selectable(td)) return;
    active = { r: r, c: c };
    if (extend && anchor) setSelection(anchor, active);
    else { anchor = active; setSelection(active, active); }
    td.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function selectionTsv() {
    var lines = [];
    if (!sel) return "";
    for (var r = sel.r1; r <= sel.r2; r++) {
      var cells = [];
      for (var c = sel.c1; c <= sel.c2; c++) {
        var td = cellAt(r, c);
        cells.push(td ? (td.hasAttribute("data-raw") ? td.getAttribute("data-raw") : td.textContent.trim()) : "");
      }
      lines.push(cells.join("\t"));
    }
    return lines.join("\n");
  }
  function pasteText(text) {
    if (!sel || !text) return;
    var rows = text.replace(/\r/g, "").replace(/\n$/, "").split("\n").map(function (l) { return l.split("\t"); });
    if (rows.length === 1 && rows[0].length === 1) { fillSelection(rows[0][0]); return; }
    // A block: cell by cell from the top-left of the selection, as far as the table goes.
    rows.forEach(function (line, dr) {
      line.forEach(function (value, dc) {
        var td = cellAt(sel.r1 + dr, sel.c1 + dc);
        if (editable(td)) writeCell(td, value);
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
      pasteText(text);
      paint(true);
    });
    return clip;
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
    var td = active ? cellAt(active.r, active.c) : null;
    if (!td || !inGrid(document.activeElement)) return;
    var ctrl = e.ctrlKey || e.metaKey;
    if (e.key === "ArrowUp") { e.preventDefault(); move(-1, 0, e.shiftKey); }
    else if (e.key === "ArrowDown") { e.preventDefault(); move(1, 0, e.shiftKey); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); move(0, -1, e.shiftKey); }
    else if (e.key === "ArrowRight") { e.preventDefault(); move(0, 1, e.shiftKey); }
    else if (e.key === "Tab") { e.preventDefault(); move(0, e.shiftKey ? -1 : 1, false); }
    else if (e.key === "Enter") { e.preventDefault(); if (editable(td)) startEdit(td); }
    else if (e.key === "Escape") { e.preventDefault(); clearSelection(); }
    else if ((e.key === "Delete" || e.key === "Backspace") && rowsChecked()) { /* the row selection owns them: see below */ }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); fillSelection(""); }
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
    if (e.target && e.target.id === "orders-table") { sel = null; anchor = null; active = null; }
    paint(false);
  });
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
      button.setAttribute("hx-confirm", n === 1 ? "Delete the selected row from the ledger? This cannot be undone here."
                                                : "Delete the " + n + " selected rows from the ledger? This cannot be undone here.");
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
    if (e.target && (e.target.id === "sel-all" || e.target.name === "sel")) count();
  });
  document.addEventListener("htmx:afterSwap", count);
  document.addEventListener("DOMContentLoaded", count);
})();

// Sheets-style row selection: press a row number to select that row (its checkbox ticks and the
// row tints), press it again to unselect, shift-click to select the range from the last press, or
// drag down the numbers to select every row the pointer crosses.
(function () {
  "use strict";
  var last = null;
  var press = null;  // the drag in progress: {tr, wasOn, moved, rows, on: [rows this drag turned on]}
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
    } else {
      // A press selects; a press on a selected row that does not turn into a drag unselects it
      // on release (the toggle), so a drag can start from a selected row too.
      press = { tr: tr, wasOn: box.checked, moved: false, rows: rows, on: [] };
      if (!box.checked) { setRow(tr, true); press.on.push(tr); }
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
    if (!press) return;
    if (!press.moved && press.wasOn) { setRow(press.tr, false); changed(press.tr); }
    press = null;
  });
  document.addEventListener("change", function (e) {
    if (e.target && (e.target.name === "sel" || e.target.id === "sel-all")) syncClasses();
  });
  document.addEventListener("htmx:afterSwap", function () { last = null; syncClasses(); });
  document.addEventListener("rows:cleared", syncClasses);
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
    if (!t || !t.form || t.form.id !== "filters") return;
    if (t.name !== "view" && t.name !== "per") return;
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
    var picked = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
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
