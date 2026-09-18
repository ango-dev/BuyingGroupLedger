// Inline cell editing for the Orders page. No build step, no framework: a <td class="edit"> turns
// into an <input> on double-click (or Enter while focused), and Enter / blur posts the value to
// /orders/cell with the row's key and what the cell showed before ("expected"), so a cell someone
// else changed meanwhile is refused as a conflict instead of overwritten. The server answers with
// the whole <td> re-rendered from a fresh read (or with data-error), and htmx swaps it in place.
(function () {
  "use strict";

  function startEdit(td) {
    if (td.querySelector("input")) return;
    var raw = td.getAttribute("data-raw") || "";
    var field = td.getAttribute("data-field");
    var input = document.createElement("input");
    input.type = "text";
    input.value = raw;
    input.className = "cell-input";
    input.setAttribute("aria-label", "edit " + field);
    if (field === "tracking_submitted") input.placeholder = "TRUE / FALSE";
    if (field === "status") input.placeholder = "ordered, shipped, delivered, cancelled, paid, return, superseded";
    td.setAttribute("data-editing", "1");
    td.innerHTML = "";
    td.appendChild(input);
    input.focus();
    input.select();

    var done = false;
    function cancel() {
      if (done) return;
      done = true;
      td.removeAttribute("data-editing");
      td.innerHTML = td.getAttribute("data-original-html") || "";
    }
    function save() {
      if (done) return;
      done = true;
      td.removeAttribute("data-editing");
      td.classList.add("saving");
      htmx.ajax("POST", "/orders/cell", {
        target: td,
        swap: "outerHTML",
        values: {
          order_id: td.getAttribute("data-order-id"),
          order_date: td.getAttribute("data-order-date"),
          item_name: td.getAttribute("data-item-name"),
          shipment: td.getAttribute("data-shipment"),
          field: field,
          value: input.value,
          expected: raw
        }
      });
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
    });
    input.addEventListener("blur", function () {
      if (input.value === raw) cancel(); else save();
    });
  }

  function armed(td) {
    if (!td.getAttribute("data-original-html")) td.setAttribute("data-original-html", td.innerHTML);
    return td;
  }

  document.addEventListener("dblclick", function (e) {
    var td = e.target.closest("td.edit");
    if (td && !td.hasAttribute("data-editing")) startEdit(armed(td));
  });
  // A cell that shows a link cannot be double-clicked into (the first click follows the link),
  // so it carries a pencil; a blank link cell shows "add" and edits on a single click.
  document.addEventListener("click", function (e) {
    var handle = e.target.closest ? e.target.closest(".cell-edit, .cell-empty") : null;
    var td = handle ? handle.closest("td.edit") : null;
    if (td && !td.hasAttribute("data-editing")) { e.preventDefault(); startEdit(armed(td)); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var td = e.target.closest ? e.target.closest("td.edit") : null;
    if (td && e.target === td && !td.hasAttribute("data-editing")) { e.preventDefault(); startEdit(armed(td)); }
  });
  // After a swap, surface a refused edit where it happened.
  document.addEventListener("htmx:afterSwap", function (e) {
    var td = e.target && e.target.matches && e.target.matches("td[data-error]") ? e.target : null;
    if (td) { td.focus(); }
  });
})();

// Row selection for bulk edit / delete: the header checkbox ticks every row shown, and the
// toolbar's counter follows the ticks (it lives in the filter bar, outside the swapped table).
(function () {
  "use strict";
  function count() {
    var n = document.querySelectorAll('input[name="sel"]:checked').length;
    var out = document.getElementById("sel-count");
    if (out) out.textContent = String(n);
  }
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "sel-all") {
      document.querySelectorAll('input[name="sel"]').forEach(function (box) { box.checked = e.target.checked; });
    }
    if (e.target && (e.target.id === "sel-all" || e.target.name === "sel")) count();
  });
  document.addEventListener("htmx:afterSwap", count);
  document.addEventListener("DOMContentLoaded", count);
})();

// Sheets-style row selection: click a row number to select that row (its checkbox ticks and the
// row tints), click again to unselect, shift-click to select the range from the last click.
(function () {
  "use strict";
  var last = null;
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
  document.addEventListener("click", function (e) {
    var td = e.target.closest ? e.target.closest("td.rownum") : null;
    if (!td) return;
    var tr = td.parentElement;
    var box = tr.querySelector('input[name="sel"]');
    if (!box) return;
    e.preventDefault();
    var rows = rowsShown();
    if (e.shiftKey && last && rows.indexOf(last) >= 0) {
      var a = rows.indexOf(last), b = rows.indexOf(tr);
      var from = Math.min(a, b), to = Math.max(a, b);
      for (var i = from; i <= to; i++) setRow(rows[i], true);
    } else {
      setRow(tr, !box.checked);
    }
    last = tr;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    window.getSelection && window.getSelection().removeAllRanges();
  });
  document.addEventListener("change", function (e) {
    if (e.target && (e.target.name === "sel" || e.target.id === "sel-all")) syncClasses();
  });
  document.addEventListener("htmx:afterSwap", function () { last = null; syncClasses(); });
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
    all.checked = !all.checked;
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
