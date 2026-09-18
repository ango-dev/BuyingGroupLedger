// The Orders page's frozen rows. <main> is the one scroll region; the filter bar sticks to its top,
// the bulk bar sticks just under it, and the column header sticks under both. Their heights depend
// on how the controls wrap, so measure and hand them to the stylesheet as --toolbar-h and
// --bulkbar-h; re-measure on resize and after every htmx swap (the table is replaced on each
// filter change).
(function () {
  "use strict";
  function measure() {
    var main = document.querySelector("body.wide main");
    if (!main) return;
    var bar = document.getElementById("filters");
    var bulk = document.getElementById("bulkbar");
    main.style.setProperty("--toolbar-h", (bar ? bar.offsetHeight : 0) + "px");
    main.style.setProperty("--bulkbar-h", (bulk ? bulk.offsetHeight : 0) + "px");
  }
  document.addEventListener("DOMContentLoaded", measure);
  window.addEventListener("resize", measure);
  document.addEventListener("htmx:afterSwap", measure);
  document.addEventListener("htmx:afterSettle", measure);
  measure();
})();
