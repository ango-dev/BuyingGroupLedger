// The Settings page: the side index follows the panel in view, the save bar knows whether the
// scalar form has unsaved edits, and every "remove" / "delete" form asks through the in-page
// dialog rather than the browser's own prompt.
(function () {
  var form = document.getElementById("scalar-form");
  var dirty = document.getElementById("dirty");
  if (form && dirty) {
    var initial = new URLSearchParams(new FormData(form)).toString();
    var check = function () {
      var now = new URLSearchParams(new FormData(form)).toString();
      var changed = now !== initial;
      dirty.textContent = changed ? "Unsaved changes" : "No unsaved changes";
      dirty.classList.toggle("on", changed);
    };
    form.addEventListener("input", check);
    form.addEventListener("change", check);
    // The Advanced panel's controls sit outside the form element (bound with form="scalar-form"):
    // FormData sees them, the form's own listeners do not.
    document.addEventListener("input", function (e) { if (e.target.getAttribute && e.target.getAttribute("form") === form.id) check(); });
    document.addEventListener("change", function (e) { if (e.target.getAttribute && e.target.getAttribute("form") === form.id) check(); });
    window.addEventListener("beforeunload", function (e) {
      if (dirty.classList.contains("on")) { e.preventDefault(); e.returnValue = ""; }
    });
    form.addEventListener("submit", function () { dirty.classList.remove("on"); });
  }

  // Side index: highlight the panel nearest the top of the viewport.
  var links = Array.prototype.slice.call(document.querySelectorAll(".settings-nav a[href^='#']"));
  var panels = links.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); });
  var pinned = null;  // the panel a click chose; held until the user scrolls away from it
  var mark = function () {
    var best = 0, bestTop = -Infinity;
    var atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
    panels.forEach(function (p, i) {
      if (!p) return;
      var top = p.getBoundingClientRect().top;
      if (top <= 80 && top > bestTop) { bestTop = top; best = i; }
      // A panel near the end of the page can never reach the top of the viewport (the page
      // stops scrolling first), so the one in view once the page is scrolled to its end wins.
      if (atBottom && top < window.innerHeight * 0.6) { best = i; }
    });
    if (pinned !== null) {
      // The clicked panel holds the highlight while any of it is in view -- a panel near the end
      // of the page never reaches the top (the page stops scrolling first), so "in view" is the
      // test, not "at the top".
      var box = panels[pinned] && panels[pinned].getBoundingClientRect();
      if (box && box.top < window.innerHeight - 40 && box.bottom > 60) { best = pinned; } else { pinned = null; }
    }
    links.forEach(function (a, i) { a.classList.toggle("active", i === best); });
  };
  if (links.length) {
    mark();
    window.addEventListener("scroll", mark, { passive: true });
    links.forEach(function (a, i) {
      a.addEventListener("click", function () { pinned = i; setTimeout(mark, 0); setTimeout(mark, 400); });
    });
  }

  // In-page confirmation for the delete forms (and the restart), same dialog the Orders page uses.
  var dialog = document.getElementById("settings-confirm");
  if (dialog && typeof dialog.showModal === "function") {
    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (e) {
        if (f.dataset.confirmed === "1") { return; }
        e.preventDefault();
        dialog.querySelector(".question").textContent = f.dataset.confirm;
        dialog.returnValue = "";
        dialog.onclose = function () {
          if (dialog.returnValue === "ok") { f.dataset.confirmed = "1"; f.requestSubmit ? f.requestSubmit() : f.submit(); }
        };
        dialog.showModal();
      });
    });
  }
})();

// Collapsible entries (Profiles / Warehouses / Cards): every entry starts closed on each load --
// nothing is remembered across a reload or a restart. The old `settings-open` cookie is
// dropped if a browser still carries it.
(function () {
  try { document.cookie = "settings-open=; path=/settings; max-age=0; samesite=lax"; } catch (e) {}
  document.querySelectorAll("[data-expand], [data-collapse]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var section = a.dataset.expand || a.dataset.collapse;
      var list = document.querySelector('.entry-list[data-section="' + section + '"]');
      if (!list) return;
      list.querySelectorAll("details.entry-card[data-key]").forEach(function (d) { d.open = !!a.dataset.expand; });
    });
  });
})();
