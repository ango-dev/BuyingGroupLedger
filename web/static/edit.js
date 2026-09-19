// The Orders table edits like a spreadsheet. No build step, no framework.
//
//   click            selects a cell (the active cell, outlined); shift-click or drag selects a range;
//                    Ctrl-click adds a cell (or takes a selected one out) and keeps the rest selected
//   double-click     opens the editor (or press Enter, or just start typing: the keystroke replaces
//                    the value, as in Sheets)
//   Enter            saves; with a RANGE selected, fills every editable cell in it with the value
//   Esc              cancels the editor, or clears the selection (cells and rows both)
//   Delete/Backspace clears every editable selected cell
//   Ctrl+;           puts today's date into every selected date cell (as in Sheets)
//   Ctrl+Shift+H     releases the hand-edit mark on every selected hand-edited cell, value kept
//                    (the count line's "release hand edits" button does the same)
//   Space            toggles a Tracking Submitted checkbox cell (click does too)
//   Ctrl+Z / Ctrl+Y  undo / redo the last accepted write (a range fill, a paste or Ctrl+; is one
//                    step; Ctrl+Shift+Z redoes too); the old value goes back through the same
//                    conflict-checked POST, so a cell someone changed meanwhile is refused, not clobbered
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
    forEachSelected(function (td) { td.classList.add("sel-cell"); });
    var release = document.getElementById("release-hand");  // shown only when a hand-edited cell is selected
    if (release) release.hidden = !handSelected().length;
    var td = active ? cellAt(active.r, active.c) : null;
    if (!td || dragging || document.activeElement === td || document.querySelector("input.cell-input")) return;
    var free = document.activeElement === document.body || inGrid(document.activeElement);
    if (takeFocus || free) td.focus({ preventScroll: true });
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
  document.addEventListener("cells:clear", clearSelection);  // a plain click on a row number

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
  // Ctrl+Shift+H, or the count line's "release hand edits" button: every selected hand-edited
  // cell keeps its value and loses its mark, so the runs may write it again. Not
  // an undo step: nothing about the value changed.
  function handSelected() {
    var out = [];
    forEachSelected(function (td) { if (editable(td) && td.classList.contains("hand")) out.push(td); });
    return out;
  }
  function releaseSelection() {
    handSelected().forEach(function (td) {
      var key = keyOf(td);
      var t = td.closest("table");
      var url = (t && t.getAttribute("data-release-url")) || "/orders/cell/release";
      td.classList.add("saving");
      queue = queue.then(function () {
        return htmx.ajax("POST", url, { target: td, swap: "outerHTML", values: key });
      }).catch(function () {});
    });
  }
  document.addEventListener("click", function (e) {
    if (e.target && e.target.id === "release-hand") releaseSelection();
  });
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

  // ---- mouse: click selects, shift-click / drag extend ---------------------------------------------
  document.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest("a, button, input, .cell-edit, .cell-empty")) return;
    var td = e.target.closest ? e.target.closest(GRID_TD) : null;
    if (!selectable(td)) return;
    if (td.hasAttribute("data-editing")) return;
    var open = document.querySelector("input.cell-input");
    if (open) open.blur();  // commits or cancels the open editor first
    var same = td.closest(GRID) === table();
    var added = true;
    if (e.shiftKey && anchor && same) { active = coordsOf(td); setSelection(anchor, active); }
    else if ((e.ctrlKey || e.metaKey) && same) { added = toggleOne(td); }
    else { selectOne(td, false); document.dispatchEvent(new Event("rows:clear")); }
    dragging = added;  // set after the first paint, which focuses the clicked cell
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
    ranges.forEach(function (range) {
      for (var r = range.r1; r <= range.r2; r++) {
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
    var ctrl = e.ctrlKey || e.metaKey;
    // Undo / redo need no selection: they act on this page's last accepted write.
    if (ctrl && (e.key === "z" || e.key === "Z") && document.querySelector(GRID)) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if (ctrl && (e.key === "y" || e.key === "Y") && document.querySelector(GRID)) { e.preventDefault(); redo(); return; }
    var td = active ? cellAt(active.r, active.c) : null;
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
    else if (ctrl && e.shiftKey && (e.key === "h" || e.key === "H")) { e.preventDefault(); releaseSelection(); }
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
    var orderId = button.getAttribute("data-order-id");
    var form = document.createElement("form");
    form.hidden = true;
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
    if (e.target && (e.target.id === "sel-all" || e.target.name === "sel")) count();
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
      document.dispatchEvent(new Event("rows:clear"));   // this row, and nothing else --
      document.dispatchEvent(new Event("cells:clear"));  // not the cells either, as in Sheets
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
  document.addEventListener("change", function (e) {
    if (e.target && (e.target.name === "sel" || e.target.id === "sel-all")) syncClasses();
  });
  document.addEventListener("htmx:afterSwap", function () { last = null; syncClasses(); });
  document.addEventListener("rows:cleared", syncClasses);
})();

// The pinned bar's height, for the table header to stick just below it (the page scrolls as one;
// the filter bar sticks). Measured on load, resize and every swap.
(function () {
  "use strict";
  function measure() {
    var bar = document.querySelector("body.wide .pinned");
    document.documentElement.style.setProperty("--pinned-h", bar ? bar.offsetHeight + "px" : "0px");
  }
  document.addEventListener("DOMContentLoaded", measure);
  window.addEventListener("resize", measure);
  document.addEventListener("htmx:afterSwap", measure);
  measure();
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
