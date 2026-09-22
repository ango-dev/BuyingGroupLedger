// In-place saves: the forms that post through htmx swap themselves -- the Settings entries and scalars,
// the Taxes inputs -- and the outcome rides as a notification from the top (#toast, swapped out of
// band). A refused save answers 400 with the form re-rendered and the reason; htmx leaves a 4xx
// unswapped unless told otherwise, which looked like the Save button doing nothing.
(function () {
  "use strict";
  document.addEventListener("htmx:beforeSwap", function (e) {
    var d = e.detail || {};
    var elt = d.requestConfig && d.requestConfig.elt;
    if (d.xhr && d.xhr.status === 400 && elt && elt.closest && elt.closest(".entry-form, .entry-delete, .tax-form, .expense-form, #scalar-form")) {
      d.shouldSwap = true;
      d.isError = false;
    }
  });
  function arm() {
    var toast = document.querySelector("#toast .toast");
    if (!toast || toast.dataset.armed) return;
    toast.dataset.armed = "1";
    setTimeout(function () { toast.classList.add("gone"); }, 3500);
    setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 4100);
  }
  document.addEventListener("htmx:afterSettle", arm);
  document.addEventListener("htmx:oobAfterSwap", arm);
  // The Taxes inputs form's bar says when something is unsaved; a save swaps the form, which resets it.
  document.addEventListener("input", function (e) {
    var form = e.target.closest ? e.target.closest("#tax-form") : null;
    var note = form && form.querySelector("#tax-dirty");
    if (note) { note.textContent = "Unsaved changes"; note.classList.add("on"); }
  });
})();
