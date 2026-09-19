// In-page pickers. ONE popover
// element, placed under the input it serves, with one of three contents:
//
//   cal      a calendar: the days of a month; the month name and the year in its head are
//            buttons -- the month name opens a grid of the twelve months, the year a grid of
//            years -- so any date is three clicks away. ‹ › step a month (or twelve years).
//            One click on a day picks it; Today / Clear at the foot.
//   month    the same without the days: a grid of months under a year (the filters' Placed in /
//            Paid in); a pick is YYYY-MM.
//   choices  the column's previous answers, filtered by what is typed; arrows move the highlight,
//            Enter takes the highlighted one, and Enter with nothing highlighted keeps what was
//            typed (a new answer, which becomes a previous one once saved)
//
// Forms: <input data-date> opens the calendar on focus, <input data-month> the month grid; a pick
// fires `input` and `change` so a filter form that submits on change follows. Cells: static/edit.js
// opens the calendar or the choices on the editor input by the cell's data-kind; a pick saves.
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
    if (done) { done(value); return; }
    input.dispatchEvent(new Event("change", { bubbles: true }));  // a filter form submits on it
    suppress = true;
    input.focus();
  }

  // ---- dates: days / months / years ----------------------------------------------------------------
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"];
  var SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function iso(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function parseIso(s) {
    var m = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec((s || "").trim());
    return m ? new Date(+m[1], +m[2] - 1, m[3] ? +m[3] : 1) : null;
  }
  function dateState() {
    if (state) return state;
    var base = parseIso(owner.value) || new Date();
    state = { y: base.getFullYear(), m: base.getMonth(), view: kind === "month" ? "months" : "days" };
    return state;
  }
  function head(prevAttr, nextAttr, middle) {
    return '<div class="cal-head"><button type="button" ' + prevAttr + ' aria-label="previous">‹</button>' +
      "<span>" + middle + '</span><button type="button" ' + nextAttr + ' aria-label="next">›</button></div>';
  }
  function renderDays(s) {
    var chosen = parseIso(owner.value);
    var lead = (new Date(s.y, s.m, 1).getDay() + 6) % 7;  // Monday first
    var days = new Date(s.y, s.m + 1, 0).getDate();
    var today = iso(new Date());
    var html = head('data-nav="-1"', 'data-nav="1"',
      '<button type="button" data-view="months" title="pick a month">' + MONTHS[s.m] + "</button> " +
      '<button type="button" data-view="years" title="pick a year">' + s.y + "</button>") + '<div class="cal-grid">';
    DOW.forEach(function (d) { html += '<span class="cal-dow">' + d + "</span>"; });
    for (var i = 0; i < lead; i++) html += "<span></span>";
    for (var d = 1; d <= days; d++) {
      var v = s.y + "-" + pad(s.m + 1) + "-" + pad(d);
      var cls = "cal-day" + (v === today ? " today" : "") + (chosen && v === iso(chosen) ? " chosen" : "");
      html += '<button type="button" class="' + cls + '" data-pick="' + v + '">' + d + "</button>";
    }
    html += '</div><div class="cal-foot"><button type="button" data-pick="' + today + '">Today</button>' +
      '<button type="button" data-pick="">Clear</button></div>';
    return html;
  }
  function renderMonths(s) {
    var chosen = parseIso(owner.value);
    var now = new Date();
    var html = head('data-year="-1"', 'data-year="1"',
      '<button type="button" data-view="years" title="pick a year">' + s.y + "</button>") + '<div class="cal-months">';
    SHORT.forEach(function (name, i) {
      var cls = "cal-month" + (s.y === now.getFullYear() && i === now.getMonth() ? " today" : "") +
        (chosen && chosen.getFullYear() === s.y && chosen.getMonth() === i ? " chosen" : "");
      html += '<button type="button" class="' + cls + '" data-month="' + i + '">' + name + "</button>";
    });
    html += "</div>";
    if (kind === "month") {
      var thisMonth = now.getFullYear() + "-" + pad(now.getMonth() + 1);
      html += '<div class="cal-foot"><button type="button" data-pick="' + thisMonth + '">This month</button>' +
        '<button type="button" data-pick="">Clear</button></div>';
    }
    return html;
  }
  function renderYears(s) {
    var chosen = parseIso(owner.value);
    var first = s.y - 6;
    var html = head('data-years="-12"', 'data-years="12"', first + " – " + (first + 11)) + '<div class="cal-months">';
    for (var y = first; y < first + 12; y++) {
      var cls = "cal-month" + (y === new Date().getFullYear() ? " today" : "") + (chosen && chosen.getFullYear() === y ? " chosen" : "");
      html += '<button type="button" class="' + cls + '" data-year-pick="' + y + '">' + y + "</button>";
    }
    return html + "</div>";
  }
  function renderDate() {
    var s = dateState();
    pop.innerHTML = s.view === "days" ? renderDays(s) : s.view === "months" ? renderMonths(s) : renderYears(s);
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
  function render() { if (kind === "choices") renderChoices(); else renderDate(); }

  function onClick(e) {
    var t = function (sel) { return e.target.closest ? e.target.closest(sel) : null; };
    var el;
    if ((el = t("[data-nav]"))) {  // a month either way
      var s = dateState();
      s.m += parseInt(el.getAttribute("data-nav"), 10);
      if (s.m < 0) { s.m = 11; s.y -= 1; }
      if (s.m > 11) { s.m = 0; s.y += 1; }
    } else if ((el = t("[data-year]"))) { dateState().y += parseInt(el.getAttribute("data-year"), 10); }
    else if ((el = t("[data-years]"))) { dateState().y += parseInt(el.getAttribute("data-years"), 10); }
    else if ((el = t("[data-view]"))) { dateState().view = el.getAttribute("data-view"); }
    else if ((el = t("[data-year-pick]"))) { dateState().y = parseInt(el.getAttribute("data-year-pick"), 10); dateState().view = "months"; }
    else if ((el = t("[data-month]"))) {
      var st = dateState();
      st.m = parseInt(el.getAttribute("data-month"), 10);
      if (kind === "month") { pick(st.y + "-" + pad(st.m + 1)); return; }
      st.view = "days";
    }
    else if ((el = t("[data-pick]"))) { pick(el.getAttribute("data-pick")); return; }
    else return;
    render();
    if (owner) place(owner);
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

  // Forms: <input data-date> is a date field, <input data-month> a month field; the picker opens
  // on focus (and on a click when it was closed with Esc).
  function formKind(el) {
    if (!el || !el.matches) return null;
    return el.matches("input[data-date]") ? "cal" : el.matches("input[data-month]") ? "month" : null;
  }
  document.addEventListener("focusin", function (e) {
    var k = formKind(e.target);
    if (k && !suppress) { state = null; open(e.target, k, null); }
    suppress = false;
  });
  document.addEventListener("click", function (e) {
    var k = formKind(e.target);
    if (k && owner !== e.target) { state = null; open(e.target, k, null); }
  });

  window.Picker = {
    date: function (input, pickFn) { state = null; open(input, "cal", pickFn); },
    month: function (input, pickFn) { state = null; open(input, "month", pickFn); },
    choices: function (input, values, pickFn) {
      state = { values: values || [], hi: -1, shown: [] };
      open(input, "choices", pickFn);
    },
    close: close
  };
})();
