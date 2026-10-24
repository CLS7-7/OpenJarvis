/* Shared helpers: CSRF, JSON requests, button loading states, local times. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function requestJSON(method, url, body) {
    var opts = {
      method: method,
      credentials: "same-origin",
      headers: { "Accept": "application/json", "X-CSRF-Token": csrfToken() }
    };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (resp) {
      if (resp.status === 401) {
        window.location.assign("/login");
        throw new Error("Your session ended. Sign in again.");
      }
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var err = new Error(data.error || "Request failed (" + resp.status + ").");
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    });
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      if (!button.dataset.idleText) button.dataset.idleText = button.textContent;
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      if (button.dataset.loadingText) button.textContent = button.dataset.loadingText;
    } else {
      button.removeAttribute("aria-busy");
      button.disabled = false;
      if (button.dataset.idleText) button.textContent = button.dataset.idleText;
    }
  }

  var timeFormat = new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
  });

  function localizeTimes(root) {
    (root || document).querySelectorAll("time[datetime]").forEach(function (el) {
      var d = new Date(el.getAttribute("datetime"));
      if (!isNaN(d.getTime())) {
        el.textContent = timeFormat.format(d);
        el.title = d.toISOString();
      }
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || form.id === "command-form") return;
    var button = form.querySelector('button[type="submit"][data-loading-text]');
    if (button) {
      // Defer so the button's own value is still submitted with the form.
      setTimeout(function () { setBusy(button, true); }, 0);
    }
  });

  window.addEventListener("pageshow", function () {
    document.querySelectorAll('button[aria-busy="true"]').forEach(function (b) { setBusy(b, false); });
  });

  document.addEventListener("DOMContentLoaded", function () { localizeTimes(document); });

  window.abuai = {
    csrfToken: csrfToken,
    requestJSON: requestJSON,
    setBusy: setBusy,
    localizeTimes: localizeTimes,
    timeFormat: timeFormat
  };
})();
