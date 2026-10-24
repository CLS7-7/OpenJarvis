/* Voice: ElevenLabs conversation via a short-lived token from our own backend. */
(function () {
  "use strict";

  var MIN_COMMAND_CHARS = 3;
  var button, labelNode, statusNode, panel;
  var state = "idle";
  var conversation = null;
  var transcript = [];
  var finalized = false;
  var configured = false;
  var leaving = false;

  function setStatus(kind, text) {
    statusNode.className = "voice-status" + (kind ? " is-" + kind : "");
    statusNode.textContent = text;
  }

  function setState(next, mode) {
    state = next;
    button.dataset.state = next;
    if (mode) button.dataset.mode = mode; else delete button.dataset.mode;
    button.setAttribute("aria-pressed", next === "live" ? "true" : "false");
    button.disabled = next === "unconfigured" || next === "connecting";
    var labels = {
      unconfigured: "Voice not configured yet",
      idle: "Talk to Jarvis",
      connecting: "Connecting…",
      live: mode === "speaking" ? "Jarvis speaking… · End conversation" : "Listening… · End conversation",
      ending: "Ending…"
    };
    labelNode.textContent = labels[next] || "Talk to Jarvis";
    button.setAttribute("aria-busy", next === "connecting" ? "true" : "false");
    document.dispatchEvent(new CustomEvent("abuai:voice-state", { detail: { state: next, mode: mode || "" } }));
  }

  /* Real conversation volumes (0..1) for the waveform; null when not available. */
  function readVolume(name) {
    if (!conversation || typeof conversation[name] !== "function") return null;
    try {
      var v = Number(conversation[name]());
      return isFinite(v) ? Math.max(0, Math.min(1, v)) : null;
    } catch (_e) {
      return null;
    }
  }

  window.abuai.voice = {
    levels: function () {
      return {
        live: state === "live",
        mode: button ? (button.dataset.mode || "") : "",
        input: readVolume("getInputVolume"),
        output: readVolume("getOutputVolume")
      };
    }
  };

  function substantive(text) {
    var letters = text.replace(/[^\p{L}\p{N}]/gu, "");
    return letters.length >= MIN_COMMAND_CHARS;
  }

  function requestMicrophone() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error("This browser cannot use a microphone here. Use a current browser over HTTPS."));
    }
    return navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
    }, function (err) {
      var name = err && err.name;
      if (name === "NotAllowedError" || name === "SecurityError") {
        throw new Error("Microphone access was denied. Allow the microphone for this site in your browser settings, then press Talk again.");
      }
      if (name === "NotFoundError" || name === "OverconstrainedError") {
        throw new Error("No microphone was found. Connect one and try again.");
      }
      throw new Error("The microphone could not be started. Check that no other app is using it.");
    });
  }

  function fetchToken() {
    return window.abuai.requestJSON("GET", "/api/voice/token").then(function (data) {
      if (!data.token) throw new Error("The voice service returned no token.");
      return data.token;
    }, function (err) {
      if (err.status === 503) {
        applyConfig({ configured: false });
        throw new Error("Voice not configured yet.");
      }
      throw err;
    });
  }

  function finalize() {
    if (finalized) return;
    finalized = true;
    conversation = null;
    if (leaving) return; // page is unloading
    var text = transcript.join("\n").trim();
    transcript = [];
    if (!text || !substantive(text)) {
      setState("idle");
      setStatus("warn", "No command captured from that conversation.");
      return;
    }
    // The ElevenLabs agent hands voice orders to Hermes itself, so nothing is submitted from here:
    // Hermes reports the task back through /api/bridge/report and it shows up live on the dashboard.
    setState(configured ? "idle" : "unconfigured");
    showHeard(text);
  }

  function showHeard(text) {
    statusNode.className = "voice-status voice-heard is-ok";
    statusNode.textContent = "";
    var heard = document.createElement("span");
    heard.className = "voice-heard-text";
    heard.textContent = "Heard: " + text;
    var note = document.createElement("span");
    note.className = "voice-heard-note";
    note.textContent = "Your agent passes voice orders to Hermes — the task appears here as soon as Hermes reports it.";
    statusNode.appendChild(heard);
    statusNode.appendChild(note);
  }

  function start() {
    if (typeof window.ElevenLabsClient === "undefined" || !window.ElevenLabsClient.Conversation) {
      setStatus("error", "The voice client failed to load. Check your connection and reload the page.");
      return;
    }
    setState("connecting");
    setStatus("", "Requesting microphone access…");
    transcript = [];
    finalized = false;
    requestMicrophone()
      .then(function () {
        setStatus("", "Connecting to Jarvis…");
        return fetchToken();
      })
      .then(function (token) {
        return window.ElevenLabsClient.Conversation.startSession({
          conversationToken: token,
          connectionType: "webrtc",
          workletPaths: {
            rawAudioProcessor: panel.dataset.workletRaw,
            audioConcatProcessor: panel.dataset.workletConcat
          },
          onConnect: function () {
            setState("live", "listening");
            setStatus("ok", "Live — speak your command. Press the button again to end the conversation.");
          },
          onModeChange: function (info) {
            if (state === "live") setState("live", info && info.mode === "speaking" ? "speaking" : "listening");
          },
          onMessage: function (payload) {
            if (!payload || typeof payload.message !== "string") return;
            var fromUser = payload.role === "user" || payload.source === "user";
            var text = payload.message.trim();
            if (fromUser && text) transcript.push(text);
          },
          onError: function (message) {
            setStatus("error", "Voice error: " + (typeof message === "string" ? message : "the connection failed."));
          },
          onDisconnect: function () { finalize(); }
        });
      })
      .then(function (conv) {
        conversation = conv;
        if (finalized) return;
        if (state === "connecting") {
          setState("live", "listening");
          setStatus("ok", "Live — speak your command. Press the button again to end the conversation.");
        }
      })
      .catch(function (err) {
        finalized = true;
        conversation = null;
        setState(configured ? "idle" : "unconfigured");
        setStatus("error", err && err.message ? err.message : "The conversation could not be started.");
      });
  }

  function stop() {
    if (!conversation) return;
    setState("ending");
    setStatus("", "Ending the conversation…");
    var conv = conversation;
    Promise.resolve(conv.endSession()).catch(function () { /* finalize below regardless */ })
      .then(function () { finalize(); });
  }

  function applyConfig(voice) {
    configured = !!(voice && voice.configured);
    if (!configured && (state === "idle" || state === "unconfigured")) {
      setState("unconfigured");
      setStatus("", "Voice will be available once the ElevenLabs agent is configured on the server.");
    } else if (configured && state === "unconfigured") {
      setState("idle");
      setStatus("", "Press Talk and give Jarvis your order. Your agent passes it to Hermes, and the task appears here when Hermes reports it.");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    button = document.getElementById("talk-btn");
    if (!button) return;
    panel = document.querySelector(".voice-panel");
    labelNode = button.querySelector(".talk-label");
    statusNode = document.getElementById("voice-status");
    configured = button.dataset.state !== "unconfigured";
    state = button.dataset.state;

    button.addEventListener("click", function () {
      if (state === "idle") start();
      else if (state === "live") stop();
    });
    document.addEventListener("abuai:voice-config", function (event) { applyConfig(event.detail); });
    window.addEventListener("pagehide", function () {
      leaving = true;
      if (conversation) conversation.endSession();
    });
  });
})();
