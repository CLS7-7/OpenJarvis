/* Dashboard: polls /api/state every 5s and updates cards in place; text commands. */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var STATUS_LABELS = { idle: "Idle", working: "Working", done: "Done" };
  var pollTimer = null;
  var inFlight = false;
  var lastState = null;

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
        card.classList.toggle("is-working", working);
        card.dataset.status = emp.status;
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
    renderRecent(state.recent_tasks);
    renderVoice(state.voice);
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
    schedule();
  });
})();
