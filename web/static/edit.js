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
