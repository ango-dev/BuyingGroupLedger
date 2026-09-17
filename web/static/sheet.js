// The Orders page's frozen rows. <main> is the one scroll region; the filter bar sticks to its top
// and the column header has to stick just BELOW the bar, whose height depends on how the filters
// wrap. Measure it and hand it to the stylesheet as --toolbar-h; re-measure on resize and after
// every htmx swap (the table is replaced on each filter change).
(function () {
  "use strict";
  function measure() {
    var main = document.querySelector("body.wide main");
    var bar = document.getElementById("filters");
    if (!main) return;
    main.style.setProperty("--toolbar-h", (bar ? bar.offsetHeight : 0) + "px");
  }
  document.addEventListener("DOMContentLoaded", measure);
  window.addEventListener("resize", measure);
  document.addEventListener("htmx:afterSwap", measure);
  document.addEventListener("htmx:afterSettle", measure);
  measure();
})();
