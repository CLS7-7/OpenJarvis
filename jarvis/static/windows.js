/* Holo-windows: on-demand Jarvis windows with live content (task, employee, Hermes, stats). */
(function () {
  "use strict";

  var stage, scrim;
  var wins = [];
  var zTop = 10;
  var seq = 0;
  var desktop = window.matchMedia("(min-width: 760px)");
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  var KIND_LABELS = { task: "Task", employee: "Employee", hermes: "Hermes link", stats: "Analytics" };
  var CLOSE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';

  /* ---------- small DOM helpers (all text via textContent) ---------- */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function button(className, text, key) {
    var b = el("button", className, text);
    b.type = "button";
    if (key) b.dataset.key = key;
    return b;
  }

  function section(title) {
    var wrap = el("section", "hw-section");
    wrap.appendChild(el("h3", "hw-section-title", title));
    return wrap;
  }

  function when(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : window.abuai.timeFormat.format(d);
  }

  function progressRow(value) {
    var row = el("div", "hw-progress");
    var bar = el("progress", "progress");
    bar.max = 100;
    bar.value = value;
    bar.setAttribute("aria-label", "Progress");
    row.appendChild(bar);
    row.appendChild(el("span", "hw-progress-value", value + "%"));
    return row;
  }

  function pill(status, label) {
    return el("span", "status status-" + status, label);
  }

  function dl(pairs, className) {
    var list = el("dl", className || "hw-facts");
    pairs.forEach(function (p) {
      var item = el("div");
      item.appendChild(el("dt", "", p[0]));
      var dd = el("dd");
      if (p[1] instanceof Node) dd.appendChild(p[1]); else dd.textContent = p[1];
      item.appendChild(dd);
      list.appendChild(item);
    });
    return list;
  }

  function taskButton(task, key) {
    var b = button("hw-task", null, key || "task-" + task.ref);
    b.dataset.openTask = task.ref;
    b.setAttribute("aria-label", "Open task " + task.ref + ": " + task.title + " (" + task.status_label + ")");
    var dash = window.abuai.dashboard;
    if (dash && dash.statusIcon) b.appendChild(dash.statusIcon(task.status));
    b.appendChild(el("span", "task-ref", task.ref));
    b.appendChild(el("span", "hw-task-title", task.title));
    b.appendChild(pill(task.status, task.status_label));
    return b;
  }

  function currentState() {
    var dash = window.abuai.dashboard;
    var s = dash && dash.lastState ? dash.lastState() : null;
    return s ? Promise.resolve(s) : window.abuai.requestJSON("GET", "/api/state");
  }

  function employeeDetails(state) {
    return Promise.all(state.employees.map(function (e) {
      return window.abuai.requestJSON("GET", "/api/employees/" + e.id).then(function (d) { return d.employee; });
    }));
  }

  /* ---------- data sources ---------- */
  var SOURCES = {
    task: function (key) {
      return window.abuai.requestJSON("GET", "/api/tasks/" + encodeURIComponent(key)).then(function (d) { return d.task; });
    },
    employee: function (key) {
      return window.abuai.requestJSON("GET", "/api/employees/" + encodeURIComponent(key)).then(function (d) { return d.employee; });
    },
    hermes: function () {
      return currentState().then(function (state) {
        return employeeDetails(state).then(function (emps) {
          var counts = { queued: 0, sent: 0, working: 0 };
          emps.forEach(function (e) {
            counts.queued += e.counts.queued;
            counts.sent += e.counts.sent;
            counts.working += e.counts.working;
          });
          return { bridge: state.bridge, counts: counts };
        });
      });
    },
    stats: function () {
      return currentState().then(function (state) {
        return employeeDetails(state).then(function (emps) {
          return {
            stats: state.stats,
            employees: emps.map(function (e) { return { id: e.id, name: e.name, color: e.color, counts: e.counts }; })
          };
        });
      });
    }
  };

  /* ---------- renderers ---------- */
  function timelineStep(label, stateClass, detail) {
    var li = el("li", "tl-step " + stateClass);
    li.appendChild(el("span", "tl-dot"));
    var text = el("div", "tl-text");
    text.appendChild(el("span", "tl-label", label));
    text.appendChild(el("span", "tl-detail", detail));
    li.appendChild(text);
    return li;
  }

  function renderTask(w, t, body) {
    w.title.textContent = t.ref;
    var state = window.abuai.dashboard && window.abuai.dashboard.lastState ? window.abuai.dashboard.lastState() : null;
    var head = el("div", "hw-head");
    head.appendChild(pill(t.status, t.status_label));
    var emp = button("inline-link", t.employee_name, "employee");
    emp.dataset.openEmployee = String(t.employee_id);
    emp.setAttribute("aria-label", "Open " + t.employee_name + " details");
    head.appendChild(emp);
    head.appendChild(el("span", "tag", t.source_label));
    body.appendChild(head);
    body.appendChild(progressRow(t.progress));

    var cmd = section("Command");
    cmd.appendChild(el("div", "prewrap hw-command", t.command_text));
    body.appendChild(cmd);

    var status = t.status;
    var reachedSent = status !== "queued";
    var finished = status === "done" || status === "failed";
    var tl = el("ol", "timeline");
    tl.appendChild(timelineStep("Created", "is-done", when(t.created_at) || "Time not recorded"));
    var sentDetail = reachedSent ? (when(t.sent_at) || "Reached · time not recorded")
      : (state && !state.bridge.connected ? "Waiting — Hermes not connected" : "Waiting to send");
    tl.appendChild(timelineStep("Sent to Hermes", reachedSent ? "is-done" : "is-current", sentDetail));
    var workingDetail;
    var workingClass;
    if (t.started_at) {
      workingDetail = when(t.started_at);
      workingClass = status === "working" ? "is-current" : "is-done";
    } else if (finished) {
      workingDetail = "Not observed — finished between checks";
      workingClass = "is-skipped";
    } else {
      workingDetail = "Not yet";
      workingClass = status === "sent" ? "is-pending is-next" : "is-pending";
    }
    tl.appendChild(timelineStep("Working", workingClass, workingDetail));
    tl.appendChild(timelineStep(status === "failed" ? "Failed" : "Done",
      finished ? (status === "failed" ? "is-failed" : "is-done") : "is-pending",
      finished ? (when(t.finished_at) || "Time not recorded") + (t.duration ? " · took " + t.duration : "") : "Not yet"));
    var tls = section("Timeline");
    tls.appendChild(tl);
    body.appendChild(tls);

    var res = section("Result");
    if (t.result_text) res.appendChild(el("div", "prewrap result-text", t.result_text));
    else res.appendChild(el("p", "muted", finished ? "Hermes reported no result text." : "No result yet — it appears here live when Hermes reports it."));
    body.appendChild(res);

    var foot = el("p", "hw-foot");
    var link = el("a", "link-strong", "Open report page");
    link.href = t.url;
    link.dataset.key = "report";
    foot.appendChild(link);
    body.appendChild(foot);
  }

  function renderEmployee(w, e, body) {
    w.title.textContent = e.name;
    w.root.style.setProperty("--emp", e.color);
    var head = el("div", "hw-head");
    head.appendChild(el("span", "hw-swatch"));
    head.appendChild(el("span", "pill pill-" + e.status, e.status_label));
    head.appendChild(el("span", "muted small", e.key));
    body.appendChild(head);

    var cur = section("Current task");
    if (e.current_task) {
      cur.appendChild(taskButton(e.current_task, "current"));
      cur.appendChild(progressRow(e.current_task.progress));
    } else {
      cur.appendChild(el("p", "muted", "No tasks yet for " + e.name + "."));
    }
    body.appendChild(cur);

    var c = e.counts;
    var counts = section("Counts");
    counts.appendChild(dl([["Total", String(c.total)], ["Done", String(c.done)], ["Working", String(c.working)], ["Failed", String(c.failed)], ["Queued", String(c.queued)], ["With Hermes", String(c.sent)]], "hw-stats"));
    body.appendChild(counts);

    var list = section("Last 10 tasks");
    if (e.recent_tasks.length) {
      var ul = el("ul", "hw-task-list");
      e.recent_tasks.forEach(function (t) {
        var li = el("li");
        li.appendChild(taskButton(t));
        ul.appendChild(li);
      });
      list.appendChild(ul);
    } else {
      list.appendChild(el("p", "muted", "No tasks assigned yet."));
    }
    body.appendChild(list);

    var res = section("Latest result");
    if (e.latest_result) {
      var r = e.latest_result;
      res.appendChild(el("div", "prewrap result-text", r.preview + (r.truncated ? " …" : "")));
      var row = el("p", "hw-foot");
      var open = button("btn btn-ghost btn-sm", "Open " + r.ref, "latest-open");
      open.dataset.openTask = r.ref;
      open.setAttribute("aria-label", "Open task " + r.ref + " in a window");
      row.appendChild(open);
      var link = el("a", "link-strong", "Full report");
      link.href = r.url;
      link.dataset.key = "latest-report";
      row.appendChild(link);
      res.appendChild(row);
    } else {
      res.appendChild(el("p", "muted", "No result reported yet."));
    }
    body.appendChild(res);
  }

  function renderHermes(w, d, body) {
    w.title.textContent = "Hermes";
    var b = d.bridge;
    var key = b.connected ? "connected" : (b.configured ? "unreachable" : "unconfigured");
    var head = el("div", "hw-head hw-hermes-head");
    var orb = el("span", "hermes-orb");
    orb.dataset.bridge = key;
    orb.appendChild(el("span", "hermes-orb-core"));
    orb.appendChild(el("span", "hermes-orb-slash"));
    head.appendChild(orb);
    head.appendChild(el("strong", "hw-hermes-label", b.label));
    body.appendChild(head);

    body.appendChild(dl([
      ["Configured", b.configured ? "Yes" : "No"],
      ["Connection", b.connected ? "Connected" : "Not connected"],
      ["Last successful contact", when(b.last_ok) || "Never"],
      ["Last failure", when(b.last_fail) || "None recorded"]
    ]));

    var counts = section("Tasks in the pipeline");
    counts.appendChild(dl([
      ["Queued (not sent yet)", String(d.counts.queued)],
      ["With Hermes", String(d.counts.sent)],
      ["Working now", String(d.counts.working)]
    ], "hw-stats"));
    body.appendChild(counts);

    body.appendChild(el("p", "hw-note",
      "While Hermes is offline, new commands are saved as Queued and sent automatically as soon as it reconnects. " +
      "Tasks Hermes already accepted keep their last reported status until it answers again — nothing is marked done without its report."));
  }

  function renderStats(w, d, body) {
    w.title.textContent = "AI analytics";
    var s = d.stats;
    var head = el("div", "hw-head");
    head.appendChild(el("strong", "hw-big", s.completion_rate === null ? "—" : s.completion_rate + "%"));
    head.appendChild(el("span", "muted", s.completion_rate === null ? "Completion rate appears once a task finishes." : "Completion rate (done of finished)"));
    body.appendChild(head);
    body.appendChild(dl([
      ["Total tasks", String(s.total)], ["Today (UTC)", String(s.today)], ["Completed", String(s.done)],
      ["Working now", String(s.working)], ["Failed", String(s.failed)]
    ], "hw-stats"));

    var per = section("Per employee");
    var wrap = el("div", "hw-table-wrap");
    var table = el("table", "hw-table");
    var thead = el("thead");
    var hr = el("tr");
    ["Employee", "Total", "Done", "Working", "Failed"].forEach(function (h) {
      var th = el("th", "", h);
      th.scope = "col";
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tb = el("tbody");
    d.employees.forEach(function (e) {
      var tr = el("tr");
      var td = el("td");
      var b = button("inline-link", e.name, "emp-" + e.id);
      b.dataset.openEmployee = String(e.id);
      b.setAttribute("aria-label", "Open " + e.name + " details");
      td.appendChild(b);
      tr.appendChild(td);
      [e.counts.total, e.counts.done, e.counts.working, e.counts.failed].forEach(function (n) { tr.appendChild(el("td", "", String(n))); });
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
    per.appendChild(wrap);
    body.appendChild(per);
  }

  var RENDERERS = { task: renderTask, employee: renderEmployee, hermes: renderHermes, stats: renderStats };

  /* ---------- window lifecycle ---------- */
  function shimmer() {
    var box = el("div", "hw-loading");
    box.setAttribute("role", "status");
    box.appendChild(el("span", "sr-only", "Loading"));
    for (var i = 0; i < 5; i++) box.appendChild(el("span", "shimmer-line"));
    return box;
  }

  function build(kind, key, opener) {
    var root = el("section", "holo-window");
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "false");
    var frame = el("div", "hw-frame");
    var bar = el("header", "hw-bar");
    var tag = el("span", "hw-kind", KIND_LABELS[kind]);
    var title = el("h2", "hw-title", kind === "task" ? key : KIND_LABELS[kind]);
    title.id = "hw-title-" + (++seq);
    title.tabIndex = -1;
    root.setAttribute("aria-labelledby", title.id);
    var close = button("hw-close");
    close.innerHTML = CLOSE_ICON; // static icon markup
    close.setAttribute("aria-label", "Close window");
    bar.appendChild(tag);
    bar.appendChild(title);
    bar.appendChild(close);
    var status = el("p", "hw-live");
    status.setAttribute("aria-live", "polite");
    var body = el("div", "hw-body");
    body.appendChild(shimmer());
    frame.appendChild(bar);
    frame.appendChild(status);
    frame.appendChild(body);
    root.appendChild(frame);

    var w = { id: kind + ":" + (key || ""), kind: kind, key: key, root: root, frame: frame, bar: bar, title: title,
              body: body, live: status, opener: opener, x: 0, y: 0, sig: null, loading: false, loaded: false, final: false };
    close.addEventListener("click", function () { closeWindow(w); });
    root.addEventListener("pointerdown", function () { bringToFront(w); });
    root.addEventListener("focusin", function () { bringToFront(w); });
    bar.addEventListener("pointerdown", function (ev) { startDrag(w, ev); });
    return w;
  }

  function render(w, data) {
    var sig = JSON.stringify(data);
    if (sig === w.sig) return;
    w.sig = sig;
    var active = document.activeElement;
    var focusKey = active && w.body.contains(active) && active.dataset ? active.dataset.key : null;
    var scroll = w.body.scrollTop;
    w.body.textContent = "";
    RENDERERS[w.kind](w, data, w.body);
    w.body.scrollTop = scroll;
    if (focusKey) {
      var again = w.body.querySelector('[data-key="' + focusKey + '"]');
      if (again) again.focus({ preventScroll: true });
    }
    w.final = w.kind === "task" && (data.status === "done" || data.status === "failed");
  }

  function load(w, silent) {
    if (w.loading) return;
    w.loading = true;
    SOURCES[w.kind](w.key).then(function (data) {
      w.loaded = true;
      w.live.textContent = "";
      render(w, data);
    }, function (err) {
      if (silent && w.loaded) {
        w.live.textContent = "Live update failed — retrying on the next refresh.";
        return;
      }
      w.sig = null;
      w.body.textContent = "";
      var box = el("div", "hw-error");
      box.setAttribute("role", "alert");
      box.appendChild(el("p", "", err && err.status === 404 ? "This item no longer exists." : "Could not load this window: " + (err && err.message ? err.message : "network error") + "."));
      var retry = button("btn btn-ghost btn-sm", "Try again", "retry");
      retry.addEventListener("click", function () {
        w.body.textContent = "";
        w.body.appendChild(shimmer());
        load(w);
      });
      box.appendChild(retry);
      w.body.appendChild(box);
    }).then(function () { w.loading = false; });
  }

  function bringToFront(w) {
    if (Number(w.root.style.zIndex) === zTop) return;
    w.root.style.zIndex = String(++zTop);
  }

  function topWindow() {
    var top = null;
    wins.forEach(function (w) { if (!top || Number(w.root.style.zIndex) > Number(top.root.style.zIndex)) top = w; });
    return top;
  }

  function applyPosition(w) {
    if (desktop.matches) w.root.style.transform = "translate(" + Math.round(w.x) + "px, " + Math.round(w.y) + "px)";
    else w.root.style.transform = "";
  }

  function clampPosition(w) {
    var width = w.root.offsetWidth || 520;
    var height = w.root.offsetHeight || 300;
    w.x = Math.min(Math.max(8, w.x), Math.max(8, window.innerWidth - width - 8));
    w.y = Math.min(Math.max(8, w.y), Math.max(8, window.innerHeight - Math.min(height, 120)));
  }

  function place(w) {
    var width = Math.min(560, window.innerWidth - 32);
    var offset = (wins.length - 1) % 6;
    w.x = (window.innerWidth - width) / 2 + offset * 28 - 70;
    w.y = 72 + offset * 28;
    clampPosition(w);
    applyPosition(w);
  }

  function startDrag(w, ev) {
    if (!desktop.matches || ev.button !== 0 || ev.target.closest(".hw-close")) return;
    ev.preventDefault();
    bringToFront(w);
    var start = { px: ev.clientX, py: ev.clientY, x: w.x, y: w.y };
    w.root.classList.add("is-dragging");
    w.bar.setPointerCapture(ev.pointerId);
    function move(e) {
      w.x = start.x + (e.clientX - start.px);
      w.y = start.y + (e.clientY - start.py);
      clampPosition(w);
      applyPosition(w);
    }
    function end() {
      w.root.classList.remove("is-dragging");
      w.bar.removeEventListener("pointermove", move);
      w.bar.removeEventListener("pointerup", end);
      w.bar.removeEventListener("pointercancel", end);
    }
    w.bar.addEventListener("pointermove", move);
    w.bar.addEventListener("pointerup", end);
    w.bar.addEventListener("pointercancel", end);
  }

  function updateScrim() {
    scrim.hidden = !(wins.length && !desktop.matches);
    stage.classList.toggle("has-windows", wins.length > 0);
  }

  function openWindow(kind, key, opener) {
    if (!RENDERERS[kind]) return;
    key = key === undefined || key === null ? "" : String(key);
    var id = kind + ":" + key;
    for (var i = 0; i < wins.length; i++) {
      if (wins[i].id === id) {
        bringToFront(wins[i]);
        wins[i].frame.classList.remove("is-flash");
        void wins[i].frame.offsetWidth;
        wins[i].frame.classList.add("is-flash");
        wins[i].title.focus({ preventScroll: true });
        return;
      }
    }
    if (!desktop.matches) wins.slice().forEach(function (w) { closeWindow(w, true); });
    var w = build(kind, key, opener);
    wins.push(w);
    stage.appendChild(w.root);
    place(w);
    bringToFront(w);
    if (!reduceMotion.matches) {
      w.frame.classList.add("is-opening");
      w.frame.addEventListener("animationend", function done(ev) {
        if (ev.target !== w.frame) return;
        w.frame.classList.remove("is-opening");
        w.frame.removeEventListener("animationend", done);
      });
    }
    updateScrim();
    w.title.focus({ preventScroll: true });
    load(w);
  }

  function closeWindow(w, instant) {
    var index = wins.indexOf(w);
    if (index === -1) return;
    wins.splice(index, 1);
    updateScrim();
    var remove = function () { if (w.root.parentNode) w.root.parentNode.removeChild(w.root); };
    if (instant || reduceMotion.matches) remove();
    else {
      w.frame.classList.add("is-closing");
      w.frame.addEventListener("animationend", remove);
      setTimeout(remove, 400);
    }
    if (instant) return;
    var next = topWindow();
    if (w.opener && document.contains(w.opener) && w.opener.offsetParent !== null) w.opener.focus({ preventScroll: true });
    else if (next) next.title.focus({ preventScroll: true });
  }

  /* ---------- wiring ---------- */
  function openerFor(target) {
    var t = target.closest("[data-open-task], [data-open-employee], [data-open-window]");
    if (!t) return null;
    if (t.dataset.openTask) return { kind: "task", key: t.dataset.openTask, el: t };
    if (t.dataset.openEmployee) return { kind: "employee", key: t.dataset.openEmployee, el: t };
    return { kind: t.dataset.openWindow, key: "", el: t };
  }

  document.addEventListener("click", function (ev) {
    if (!stage || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var o = openerFor(ev.target);
    if (!o) return;
    ev.preventDefault();
    // A button inside a window returns focus to the original opener of that window, not to itself.
    var host = o.el.closest(".holo-window");
    var opener = o.el;
    if (host) {
      for (var i = 0; i < wins.length; i++) if (wins[i].root === host) opener = wins[i].opener || o.el;
    }
    openWindow(o.kind, o.key, opener);
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape" || !wins.length) return;
    var top = topWindow();
    if (top) {
      ev.preventDefault();
      closeWindow(top);
    }
  });

  document.addEventListener("abuai:state", function () {
    wins.forEach(function (w) {
      if (w.kind === "task" && w.final) return; // finished tasks no longer change
      if (w.loaded) load(w, true);
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    stage = document.getElementById("holo-stage");
    scrim = document.getElementById("holo-scrim");
    if (!stage) return;
    scrim.addEventListener("click", function () {
      wins.slice().forEach(function (w) { closeWindow(w); });
    });
    var onLayout = function () {
      if (!desktop.matches && wins.length > 1) wins.slice(0, -1).forEach(function (w) { closeWindow(w, true); });
      wins.forEach(function (w) { if (desktop.matches) clampPosition(w); applyPosition(w); });
      updateScrim();
    };
    if (desktop.addEventListener) desktop.addEventListener("change", onLayout);
    window.addEventListener("resize", function () { wins.forEach(function (w) { if (desktop.matches) { clampPosition(w); applyPosition(w); } }); });
  });

  window.abuai.windows = { open: openWindow };
})();
