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
