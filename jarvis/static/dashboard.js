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

  function renderWorkflow(tasks, bridge) {
    var panel = document.getElementById("workflow");
    if (!panel) return;
    var latest = tasks.length ? tasks[0] : null;
    var st = latest ? latest.status : "";
    var lit = {
      command: !!latest,
      employee: !!latest,
      hermes: ["sent", "working", "done", "failed"].indexOf(st) !== -1,
      result: st === "done" || st === "failed"
    };
    var active = { queued: "employee", sent: "hermes", working: "hermes", done: "result", failed: "result" }[st] || "";
    panel.classList.toggle("is-empty", !latest);
    panel.dataset.status = st;
    panel.querySelectorAll(".flow-node").forEach(function (node) {
      var stage = node.dataset.stage;
      node.classList.toggle("is-lit", !!lit[stage]);
      node.classList.toggle("is-active", active === stage);
      node.classList.toggle("is-failed", stage === "result" && st === "failed");
    });
    var caption = document.getElementById("flow-caption");
    if (!caption) return;
    if (!latest) {
      caption.textContent = "No tasks yet — the flow lights up as your first command moves through it.";
      return;
    }
    var text = latest.ref + " · " + latest.employee_name + " · " + latest.status_label;
    if (st === "queued" && !bridge.connected) text += " — waiting for bridge";
    caption.textContent = text;
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
      a.appendChild(statusIcon(task.status));
      a.appendChild(el("span", "task-ref", task.ref));
      a.appendChild(el("span", "task-title", task.title));
      var meta = el("span", "task-meta muted small");
      meta.appendChild(document.createTextNode(task.employee_name + " · "));
      var time = el("time", "", task.created_human);
      time.setAttribute("datetime", task.created_at);
      meta.appendChild(time);
      if (task.source === "voice") meta.appendChild(document.createTextNode(" · voice"));
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
    renderWorkflow(state.recent_tasks, state.bridge);
    renderStats(state.stats);
    renderVoice(state.voice);
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
    employeeName: employeeName
  };

  document.addEventListener("DOMContentLoaded", function () {
    initCommandForm();
    refresh();
    schedule();
  });
})();
