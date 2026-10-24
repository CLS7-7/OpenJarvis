/* Dashboard: polls /api/state every 5s and updates cards in place; text commands. */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var STATUS_LABELS = { idle: "Idle", working: "Working", done: "Done" };
  var pollTimer = null;
  var inFlight = false;
  var lastState = null;
  var lastStatusByEmployee = {};
  var DESK_ARC = 62.83;
  var DESK_BAR = 78;
  var GAUGE_ARC = 301.59;
  var SVG_OPEN = '<svg class="st-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">';
  var RING = '<circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".45"/>';
  var ICONS = {
    done: RING + '<path d="M7 12.5l3.2 3.2L17 9" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    working: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2" opacity=".25"/><path class="st-spin" d="M12 3a9 9 0 0 1 9 9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
    failed: RING + '<path d="M8.5 8.5l7 7M15.5 8.5l-7 7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
    sent: RING + '<path d="M7 12h9M13 8.5l3.5 3.5-3.5 3.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    queued: RING + '<path d="M12 7v5.5l3.5 2" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
  };

  /* Seat phase from real data: working > alert (queued/sent) > done > idle. */
  function phaseOf(emp) {
    var t = emp.current_task;
    if (emp.status === "working") return "working";
    if (t && (t.status === "queued" || t.status === "sent")) return "alert";
    if (emp.status === "done") return "done";
    return "idle";
  }

  function statusIcon(status) {
    var span = el("span", "task-icon status-" + status);
    // Static, trusted markup only (no task data is interpolated).
    span.innerHTML = SVG_OPEN + (ICONS[status] || ICONS.queued) + "</svg>";
    return span;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function bridgeConnected() {
    return !!(lastState && lastState.bridge && lastState.bridge.connected);
  }

  function renderEmployees(employees) {
    var picker = document.getElementById("employee-picker");
    employees.forEach(function (emp) {
      var card = document.querySelector('.emp-card[data-employee-id="' + emp.id + '"]');
      if (card) {
        var working = emp.status === "working";
        var phase = phaseOf(emp);
        var previous = lastStatusByEmployee[emp.id];
        card.classList.toggle("is-working", working);
        card.dataset.status = emp.status;
        card.dataset.phase = phase;
        if (previous && previous !== "done" && emp.status === "done") celebrateSeat(card);
        lastStatusByEmployee[emp.id] = emp.status;
        var arc = card.querySelector(".desk-arc");
        if (arc) arc.setAttribute("stroke-dashoffset", (DESK_ARC * (1 - emp.progress / 100)).toFixed(2));
        var bar = card.querySelector(".desk-bar");
        if (bar) bar.setAttribute("width", (DESK_BAR * emp.progress / 100).toFixed(1));
        card.querySelector(".emp-name").textContent = emp.name;

        var pill = card.querySelector('[data-role="pill"]');
        pill.className = "pill pill-" + emp.status;
        pill.textContent = STATUS_LABELS[emp.status] || emp.status_label;

        var task = card.querySelector('[data-role="task"]');
        var taskStatus = card.querySelector('[data-role="task-status"]');
        task.textContent = "";
        if (emp.current_task) {
          task.appendChild(el("span", "emp-ref", emp.current_task.ref));
          task.appendChild(document.createTextNode(" " + emp.current_task.title));
          var label = emp.current_task.status_label;
          if (emp.current_task.status === "queued" && !bridgeConnected()) label += " — waiting for bridge";
          taskStatus.textContent = label;
        } else {
          task.textContent = "No tasks yet";
          taskStatus.textContent = "";
        }

        var progress = card.querySelector('[data-role="progress"]');
        progress.value = emp.progress;
        progress.textContent = emp.progress + "%";
        progress.setAttribute("aria-label", emp.name + " progress");
      }
      if (picker) {
        var option = picker.querySelector('option[value="' + emp.id + '"]');
        if (option && option.textContent !== emp.name) option.textContent = emp.name;
      }
    });
  }

  function celebrateSeat(card) {
    card.classList.remove("just-done");
    void card.offsetWidth; // restart the one-shot animation
    card.classList.add("just-done");
    setTimeout(function () { card.classList.remove("just-done"); }, 1600);
  }

  function renderDeskHolo(employees) {
    var node = document.getElementById("desk-holo-text");
    if (!node) return;
    var working = employees.filter(function (e) { return e.status === "working"; }).length;
    node.textContent = working + " of " + employees.length + " working";
  }

  function bridgeKey(bridge) {
    if (bridge.connected) return "connected";
    return bridge.configured ? "unreachable" : "unconfigured";
  }

  function renderLinkMap(state) {
    var panel = document.getElementById("link-map");
    if (!panel) return;
    var bridge = state.bridge;
    var key = bridgeKey(bridge);
    var tasks = state.recent_tasks;
    var latest = tasks.length ? tasks[0] : null;
    var anyWorking = state.employees.some(function (e) { return e.status === "working"; });
    panel.dataset.bridge = key;
    panel.classList.toggle("is-empty", !latest);

    var orb = document.getElementById("hermes-orb");
    if (orb) {
      orb.dataset.bridge = key;
      orb.setAttribute("aria-label", "Open Hermes link details: " + bridge.label);
    }
    setText("hermes-label", bridge.label);
    panel.querySelectorAll(".link-line").forEach(function (line) {
      line.classList.toggle("is-flowing", anyWorking);
    });
    state.employees.forEach(function (emp) {
      var bot = panel.querySelector('[data-link-employee="' + emp.id + '"]');
      if (!bot) return;
      bot.classList.toggle("is-working", emp.status === "working");
      bot.setAttribute("aria-label", "Open " + emp.name + " details — " + emp.status_label);
    });

    var caption = document.getElementById("flow-caption");
    if (!caption) return;
    caption.textContent = "";
    if (!latest) {
      caption.textContent = "No tasks yet — the link lights up as your first command moves through Hermes.";
      return;
    }
    var open = el("button", "inline-link", latest.ref);
    open.type = "button";
    open.dataset.openTask = latest.ref;
    open.setAttribute("aria-label", "Open task " + latest.ref);
    caption.appendChild(open);
    var text = " · " + latest.employee_name + " · " + latest.status_label;
    if (latest.status === "queued" && !bridge.connected) text += " — waiting for bridge";
    caption.appendChild(document.createTextNode(text));
  }

  /* ---------- live feed: real transitions between successive states ---------- */
  var FEED_MAX = 20;
  var feedSeeded = false;
  var previousTasks = null;
  var previousBridge = null;
  var feedClock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  function taskMap(state) {
    var map = {};
    state.recent_tasks.forEach(function (t) { map[t.ref] = t; });
    state.employees.forEach(function (e) {
      if (e.current_task) map[e.current_task.ref] = e.current_task;
    });
    return map;
  }

  var STATUS_EVENTS = {
    sent: { kind: "sent", text: "handed to Hermes" },
    working: { kind: "working", text: "work started" },
    done: { kind: "done", text: "completed" },
    failed: { kind: "failed", text: "failed" }
  };

  function diffState(state) {
    var tasks = taskMap(state);
    var events = [];
    if (!feedSeeded) {
      feedSeeded = true;
      previousTasks = tasks;
      previousBridge = state.bridge;
      return events;
    }
    var maxPrevId = 0;
    Object.keys(previousTasks).forEach(function (ref) { maxPrevId = Math.max(maxPrevId, previousTasks[ref].id); });
    Object.keys(tasks).sort().forEach(function (ref) {
      var t = tasks[ref];
      var before = previousTasks[ref];
      if (!before) {
        if (t.id <= maxPrevId) return; // an older task scrolling into view is not a new event
        if (t.source === "text") {
          events.push({ kind: "created", ref: ref, text: "created for " + t.employee_name + " — " + t.title });
          if (STATUS_EVENTS[t.status]) events.push({ kind: STATUS_EVENTS[t.status].kind, ref: ref, text: STATUS_EVENTS[t.status].text + " · " + t.employee_name });
        } else {
          // Came in from Hermes (voice agent or another channel): the site did not hand it over.
          events.push({ kind: "created", ref: ref, text: "reported by Hermes (" + t.source_label.toLowerCase() + ") for " + t.employee_name + " — " + t.title });
          if (t.status !== "sent" && STATUS_EVENTS[t.status]) events.push({ kind: STATUS_EVENTS[t.status].kind, ref: ref, text: STATUS_EVENTS[t.status].text + " · " + t.employee_name });
        }
      } else if (before.status !== t.status && STATUS_EVENTS[t.status]) {
        events.push({ kind: STATUS_EVENTS[t.status].kind, ref: ref, text: STATUS_EVENTS[t.status].text + " · " + t.employee_name });
      }
    });
    if (previousBridge && previousBridge.connected !== state.bridge.connected) {
      events.push(state.bridge.connected
        ? { kind: "bridge-up", text: "Hermes connected" }
        : { kind: "bridge-down", text: state.bridge.configured ? "Hermes connection lost" : "Hermes not configured" });
    }
    previousTasks = tasks;
    previousBridge = state.bridge;
    return events;
  }

  function renderFeed(events, serverTime) {
    var list = document.getElementById("live-feed");
    if (!list || !events.length) return;
    var empty = list.querySelector(".feed-empty");
    if (empty) empty.remove();
    var stamp = serverTime ? new Date(serverTime) : new Date();
    var clock = isNaN(stamp.getTime()) ? "" : feedClock.format(stamp);
    events.forEach(function (ev) {
      var li = el("li", "feed-item feed-" + ev.kind);
      var time = el("time", "feed-time", clock);
      if (serverTime) time.setAttribute("datetime", serverTime);
      li.appendChild(time);
      li.appendChild(el("span", "feed-dot"));
      if (ev.ref) {
        var btn = el("button", "feed-ref inline-link", ev.ref);
        btn.type = "button";
        btn.dataset.openTask = ev.ref;
        btn.setAttribute("aria-label", "Open task " + ev.ref);
        li.appendChild(btn);
      }
      li.appendChild(el("span", "feed-text", ev.text));
      list.insertBefore(li, list.firstChild);
    });
    while (list.children.length > FEED_MAX) list.removeChild(list.lastChild);
    document.dispatchEvent(new CustomEvent("abuai:feed", { detail: { events: events } }));
  }

  function setText(id, value) {
    var node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function renderStats(stats) {
    if (!stats) return;
    setText("stat-today", String(stats.today));
    setText("stat-done", String(stats.done));
    setText("stat-working", String(stats.working));
    setText("stat-failed", String(stats.failed));
    setText("stat-total", stats.total + (stats.total === 1 ? " task" : " tasks") + " recorded in total.");
    var rate = stats.completion_rate;
    setText("gauge-value", rate === null || rate === undefined ? "—" : rate + "%");
    var arc = document.getElementById("gauge-arc");
    if (arc) arc.setAttribute("stroke-dashoffset", (GAUGE_ARC * (1 - (rate || 0) / 100)).toFixed(2));
  }

  function renderRecent(tasks) {
    var list = document.getElementById("recent-tasks");
    if (!list) return;
    list.textContent = "";
    if (!tasks.length) {
      var empty = el("li", "empty-state");
      empty.appendChild(el("p", "empty-title", "No tasks yet"));
      empty.appendChild(el("p", "muted small", "Send a command or talk to Jarvis — reports appear here as soon as a task is created."));
      list.appendChild(empty);
      return;
    }
    tasks.forEach(function (task) {
      var li = el("li", "task-row");
      var a = el("a", "task-link");
      a.href = task.url;
      a.dataset.openTask = task.ref;
      a.setAttribute("aria-label", "Open task " + task.ref + ": " + task.title);
      a.appendChild(statusIcon(task.status));
      a.appendChild(el("span", "task-ref", task.ref));
      a.appendChild(el("span", "task-title", task.title));
      var meta = el("span", "task-meta muted small");
      meta.appendChild(document.createTextNode(task.employee_name + " · "));
      var time = el("time", "", task.created_human);
      time.setAttribute("datetime", task.created_at);
      meta.appendChild(time);
      if (task.source !== "text") meta.appendChild(document.createTextNode(" · " + task.source_label.toLowerCase()));
      a.appendChild(meta);
      a.appendChild(el("span", "status status-" + task.status, task.status_label));
      li.appendChild(a);
      list.appendChild(li);
    });
    window.abuai.localizeTimes(list);
  }

  function renderBridge(bridge) {
    var banner = document.getElementById("bridge-banner");
    if (banner) banner.hidden = !!bridge.connected;
  }

  function renderVoice(voice) {
    document.dispatchEvent(new CustomEvent("abuai:voice-config", { detail: voice }));
  }

  function render(state) {
    lastState = state;
    renderBridge(state.bridge);
    renderEmployees(state.employees);
    renderDeskHolo(state.employees);
    renderRecent(state.recent_tasks);
    renderLinkMap(state);
    renderStats(state.stats);
    renderVoice(state.voice);
    renderFeed(diffState(state), state.server_time);
    document.dispatchEvent(new CustomEvent("abuai:state", { detail: state }));
  }

  function refresh() {
    if (inFlight) return Promise.resolve();
    inFlight = true;
    return window.abuai.requestJSON("GET", "/api/state")
      .then(render)
      .catch(function () { /* keep the last good state; next poll retries */ })
      .then(function () { inFlight = false; });
  }

  function schedule() {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      if (!document.hidden) refresh();
    }, POLL_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });

  /* Creates a task through the single pipeline used by text and voice. */
  function createTask(employeeId, commandText, source) {
    return window.abuai.requestJSON("POST", "/api/tasks", {
      employee_id: Number(employeeId),
      command_text: commandText,
      source: source
    }).then(function (data) {
      refresh();
      return data;
    });
  }

  function employeeName(id) {
    var option = document.querySelector('#employee-picker option[value="' + id + '"]');
    return option ? option.textContent : "employee";
  }

  /* Plain-language outcome of a created task; never claims more than the backend reported. */
  function describeCreated(data, prefix) {
    var task = data.task;
    var who = task.employee_name;
    if (task.status === "queued") {
      return {
        kind: "warn",
        text: prefix + " queued for " + who + " — " + task.ref + ". Bridge not connected — tasks will send when Hermes is connected."
      };
    }
    return { kind: "ok", text: prefix + " sent to " + who + " — " + task.ref };
  }

  function setResult(node, kind, text) {
    node.className = "form-result" + (kind ? " is-" + kind : "");
    node.textContent = text;
  }

  function initCommandForm() {
    var form = document.getElementById("command-form");
    if (!form) return;
    var textarea = document.getElementById("command-text");
    var picker = document.getElementById("employee-picker");
    var button = document.getElementById("command-send");
    var result = document.getElementById("command-result");

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = textarea.value.trim();
      if (!text) {
        setResult(result, "error", "Type a command first.");
        textarea.focus();
        return;
      }
      window.abuai.setBusy(button, true);
      setResult(result, "", "");
      createTask(picker.value, text, "text")
        .then(function (data) {
          var outcome = describeCreated(data, "Command");
          setResult(result, outcome.kind, outcome.text);
          textarea.value = "";
        })
        .catch(function (err) { setResult(result, "error", err.message); })
        .then(function () { window.abuai.setBusy(button, false); });
    });
  }

  window.abuai.dashboard = {
    refresh: refresh,
    createTask: createTask,
    describeCreated: describeCreated,
    employeeName: employeeName,
    statusIcon: statusIcon,
    lastState: function () { return lastState; }
  };

  document.addEventListener("DOMContentLoaded", function () {
    initCommandForm();
    refresh();
    schedule();
  });
})();
