// In-page tooltips. Any element with a `title` -- or an SVG element with a <title> child, as
// the chart slices have -- shows its text in one small panel after a short hover, positioned under
// (or over) the element and kept inside the viewport. The native attribute is moved to data-tip on
// first hover so the browser's own bubble never appears. Focus shows it too, for the keyboard.
(function () {
  "use strict";
  var DELAY = 280;
  var tip = null, timer = null, current = null;

  function panel() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.className = "tip";
    tip.setAttribute("role", "tooltip");
    document.body.appendChild(tip);
    return tip;
  }

  function textOf(el) {
    if (el.hasAttribute("title")) {
      var t = el.getAttribute("title");
      el.removeAttribute("title");
      if (t && t.trim()) el.setAttribute("data-tip", t.trim());
    }
    if (!el.hasAttribute("data-tip") && el.namespaceURI === "http://www.w3.org/2000/svg") {
      var child = el.firstElementChild;
      while (child) {
        if (child.tagName && child.tagName.toLowerCase() === "title") {
          el.setAttribute("data-tip", (child.textContent || "").trim());
          child.parentNode.removeChild(child);
          break;
        }
        child = child.nextElementSibling;
      }
    }
    return el.getAttribute("data-tip") || "";
  }

  function target(node) {
    var el = node && node.nodeType === 1 ? node : node && node.parentElement;
    while (el && el !== document.body) {
      if (el.hasAttribute("title") || el.hasAttribute("data-tip") ||
          (el.namespaceURI === "http://www.w3.org/2000/svg" && el.querySelector(":scope > title"))) {
        return el;
      }
      el = el.parentElement;
    }
    return null;
  }

  function place(el) {
    var box = el.getBoundingClientRect();
    var p = panel();
    var w = p.offsetWidth, h = p.offsetHeight;
    var pad = 8;
    var left = box.left + box.width / 2 - w / 2;
    left = Math.max(pad, Math.min(left, window.innerWidth - w - pad));
    var top = box.bottom + 8;
    if (top + h > window.innerHeight - pad) top = box.top - h - 8;
    if (top < pad) top = pad;
    p.style.left = left + "px";
    p.style.top = top + "px";
  }

  function show(el) {
    var text = textOf(el);
    if (!text) return;
    var p = panel();
    p.textContent = text;
    p.classList.add("on");
    place(el);
    current = el;
  }

  function hide() {
    clearTimeout(timer);
    timer = null;
    current = null;
    if (tip) tip.classList.remove("on");
  }

  function arm(el, immediate) {
    clearTimeout(timer);
    if (!el) { hide(); return; }
    if (el === current) return;
    timer = setTimeout(function () { show(el); }, immediate ? 0 : DELAY);
  }

  document.addEventListener("mouseover", function (e) {
    var el = target(e.target);
    if (!el) { if (current) hide(); return; }
    if (el !== current) arm(el, false);
  });
  document.addEventListener("mouseout", function (e) {
    var el = target(e.target);
    if (el && el.contains(e.relatedTarget)) return;
    hide();
  });
  document.addEventListener("focusin", function (e) { var el = target(e.target); if (el) arm(el, true); });
  document.addEventListener("focusout", hide);
  document.addEventListener("mousedown", hide);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  window.addEventListener("scroll", function () { if (current) place(current); }, { passive: true });
  window.addEventListener("resize", function () { if (current) place(current); });
})();
