// In-page pickers. ONE popover element, placed under the input
// it serves, with one of two contents:
//
//   cal      a month calendar: ‹ › move months, a day is picked with one click, Today / Clear
//   choices  the column's previous answers, filtered by what is typed; arrows move the highlight,
//            Enter takes the highlighted one, and Enter with nothing highlighted keeps what was
//            typed (a new answer, which becomes a previous one once saved)
//
// Forms: any <input data-date> opens the calendar on focus. Cells: static/edit.js opens either
// on the editor input by the cell's data-kind, and a pick saves the cell.
(function () {
  "use strict";
  var pop = null, owner = null, onPick = null, kind = null, state = null, suppress = false;

  function panel() {
    if (pop) return pop;
    pop = document.createElement("div");
    pop.className = "pop";
    pop.setAttribute("role", "dialog");
    // A press inside the popover must not blur the input it serves: a blur would commit the cell
    // editor before the click lands. The press is swallowed; the click acts.
    pop.addEventListener("mousedown", function (e) { e.preventDefault(); });
    pop.addEventListener("click", onClick);
    document.body.appendChild(pop);
    return pop;
  }
  function place(input) {
    var box = input.getBoundingClientRect();
    var p = panel();
    var pad = 8;
    var left = Math.max(pad, Math.min(box.left, window.innerWidth - p.offsetWidth - pad));
    var top = box.bottom + 4;
    if (top + p.offsetHeight > window.innerHeight - pad) top = Math.max(pad, box.top - p.offsetHeight - 4);
    p.style.left = left + "px";
    p.style.top = top + "px";
  }
  function close() {
    if (!pop) return;
    pop.classList.remove("on");
    pop.innerHTML = "";
    owner = null; onPick = null; kind = null; state = null;
  }
  function open(input, k, pick) {
    panel();
    owner = input; kind = k; onPick = pick || null;
    pop.className = "pop " + k;
    render();
    pop.classList.add("on");
    place(input);
  }
  function pick(value) {
    var input = owner;
    if (!input) return;
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    var done = onPick;
    close();
    if (done) done(value);
    else { suppress = true; input.focus(); }
  }

  // ---- the calendar ------------------------------------------------------------------------------
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"];
  var DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function iso(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function parseIso(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((s || "").trim());
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }
  function renderCalendar() {
    var chosen = parseIso(owner.value);
    if (!state) { var base = chosen || new Date(); state = { y: base.getFullYear(), m: base.getMonth() }; }
    var first = new Date(state.y, state.m, 1);
    var lead = (first.getDay() + 6) % 7;  // Monday first
    var days = new Date(state.y, state.m + 1, 0).getDate();
    var today = iso(new Date());
    var html = '<div class="cal-head"><button type="button" data-nav="-1" aria-label="previous month">‹</button>' +
      "<span>" + MONTHS[state.m] + " " + state.y + "</span>" +
      '<button type="button" data-nav="1" aria-label="next month">›</button></div><div class="cal-grid">';
    DOW.forEach(function (d) { html += '<span class="cal-dow">' + d + "</span>"; });
    for (var i = 0; i < lead; i++) html += "<span></span>";
    for (var d = 1; d <= days; d++) {
      var v = state.y + "-" + pad(state.m + 1) + "-" + pad(d);
      var cls = "cal-day" + (v === today ? " today" : "") + (chosen && v === iso(chosen) ? " chosen" : "");
      html += '<button type="button" class="' + cls + '" data-pick="' + v + '">' + d + "</button>";
    }
    html += '</div><div class="cal-foot"><button type="button" data-pick="' + today + '">Today</button>' +
      '<button type="button" data-pick="">Clear</button></div>';
    pop.innerHTML = html;
  }

  // ---- the choices -------------------------------------------------------------------------------
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function renderChoices() {
    var typed = (owner.value || "").trim().toLowerCase();
    var values = state.values.filter(function (v) { return !typed || v.toLowerCase().indexOf(typed) >= 0; });
    state.shown = values;
    if (state.hi >= values.length) state.hi = values.length ? values.length - 1 : -1;
    var html = "";
    values.slice(0, 40).forEach(function (v, i) {
      html += '<button type="button" class="choice' + (i === state.hi ? " hi" : "") +
        '" data-pick="' + escapeHtml(v) + '">' + escapeHtml(v) + "</button>";
    });
    if (!values.length) {
      html = '<span class="muted small">' + (typed ? "a new answer: Enter keeps what you typed" : "no previous answers yet") + "</span>";
    }
    pop.innerHTML = html;
  }
  function render() { if (kind === "cal") renderCalendar(); else renderChoices(); }

  function onClick(e) {
    var nav = e.target.closest ? e.target.closest("[data-nav]") : null;
    if (nav && state) {
      state.m += parseInt(nav.getAttribute("data-nav"), 10);
      if (state.m < 0) { state.m = 11; state.y -= 1; }
      if (state.m > 11) { state.m = 0; state.y += 1; }
      render();
      if (owner) place(owner);
      return;
    }
    var b = e.target.closest ? e.target.closest("[data-pick]") : null;
    if (b) pick(b.getAttribute("data-pick"));
  }

  // Keys on the owning input, in the CAPTURE phase so the popover acts before the input's own
  // listeners (the cell editor's Enter must not save when Enter takes a highlighted choice).
  document.addEventListener("keydown", function (e) {
    if (!owner || e.target !== owner) return;
    if (e.key === "Escape") { close(); return; }  // the editor's own Esc still cancels the edit
    if (e.key === "Tab") { close(); return; }
    if (kind !== "choices" || !state) return;
    if (e.key === "ArrowDown") { e.preventDefault(); state.hi = Math.min(state.hi + 1, state.shown.length - 1); render(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); state.hi = Math.max(state.hi - 1, -1); render(); }
    else if (e.key === "Enter" && state.hi >= 0 && state.shown[state.hi] !== undefined) {
      e.preventDefault();
      e.stopImmediatePropagation();
      pick(state.shown[state.hi]);
    }
  }, true);
  document.addEventListener("input", function (e) {
    if (owner && e.target === owner && kind === "choices" && state) { state.hi = -1; render(); }
  });
  document.addEventListener("focusout", function (e) { if (owner && e.target === owner) close(); });
  document.addEventListener("mousedown", function (e) {
    if (pop && pop.classList.contains("on") && !pop.contains(e.target) && e.target !== owner) close();
  });
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close);

  // Forms: an <input data-date> is a date field; the calendar opens on focus (and on a click
  // when it was closed with Esc).
  document.addEventListener("focusin", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("input[data-date]") && !suppress) { state = null; open(el, "cal", null); }
    suppress = false;
  });
  document.addEventListener("click", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("input[data-date]") && owner !== el) { state = null; open(el, "cal", null); }
  });

  window.Picker = {
    date: function (input, pickFn) { state = null; open(input, "cal", pickFn); },
    choices: function (input, values, pickFn) {
      state = { values: values || [], hi: -1, shown: [] };
      open(input, "choices", pickFn);
    },
    close: close
  };
})();
