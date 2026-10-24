/* Jarvis core visuals: state classes from real data, completion pulse, particles and the voice waveform. */
(function () {
  "use strict";

  var core, particlesCanvas, waveCanvas, voiceLive, voiceLiveText;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var anyWorking = false;
  var voiceState = "idle";
  var voiceMode = "";
  var knownStatus = null; // ref -> status from the previous poll
  var rafId = null;
  var particles = [];
  var waveLevel = 0;

  var VOICE_LABELS = {
    unconfigured: "Voice not configured",
    idle: "Standing by",
    connecting: "Connecting",
    live: "Live",
    ending: "Ending",
    submitting: "Sending command"
  };

  /* ---------- core state (real data only) ---------- */
  function applyCoreState() {
    if (!core) return;
    var voiceActive = voiceState === "live" || voiceState === "connecting";
    var next = voiceActive ? "voice" : (anyWorking ? "working" : "idle");
    core.classList.remove("core-state-idle", "core-state-working", "core-state-voice");
    core.classList.add("core-state-" + next);
  }

  function celebrate() {
    var ring = core && core.querySelector(".core-pulse-ring");
    if (!ring) return;
    ring.classList.remove("is-pulsing");
    void ring.getBoundingClientRect(); // restart the one-shot animation
    ring.classList.add("is-pulsing");
  }

  function onState(event) {
    var state = event.detail;
    anyWorking = state.employees.some(function (e) { return e.status === "working"; });
    var completed = false;
    var current = {};
    state.recent_tasks.forEach(function (t) {
      current[t.ref] = t.status;
      if (knownStatus && knownStatus[t.ref] && knownStatus[t.ref] !== "done" && t.status === "done") completed = true;
    });
    knownStatus = current;
    applyCoreState();
    if (completed) celebrate();
  }

  function onVoiceState(event) {
    voiceState = event.detail.state;
    voiceMode = event.detail.mode;
    if (voiceLive) {
      voiceLive.dataset.state = voiceState;
      var label = VOICE_LABELS[voiceState] || "Standing by";
      if (voiceState === "live") label += voiceMode === "speaking" ? " — Jarvis speaking" : " — listening";
      voiceLiveText.textContent = label;
    }
    applyCoreState();
    if (reduceMotion.matches) drawWave(0);
  }

  /* ---------- canvas helpers ---------- */
  function fitCanvas(canvas) {
    var rect = canvas.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.max(1, Math.round(rect.width * dpr));
    var h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    return { w: w, h: h, dpr: dpr };
  }

  /* ---------- particles (rising light motes) ---------- */
  function seedParticles() {
    particles = [];
    for (var i = 0; i < 42; i++) particles.push(newParticle(Math.random()));
  }

  function newParticle(yFraction) {
    return {
      x: Math.random(),
      y: yFraction,
      r: 0.6 + Math.random() * 1.6,
      speed: 0.0006 + Math.random() * 0.0014,
      drift: (Math.random() - 0.5) * 0.0004,
      alpha: 0.25 + Math.random() * 0.55
    };
  }

  function drawParticles() {
    if (!particlesCanvas) return;
    var size = fitCanvas(particlesCanvas);
    var ctx = particlesCanvas.getContext("2d");
    ctx.clearRect(0, 0, size.w, size.h);
    var boost = core.classList.contains("core-state-idle") ? 1 : 2.4;
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      p.y -= p.speed * boost;
      p.x += p.drift;
      if (p.y < -0.02 || p.x < -0.02 || p.x > 1.02) particles[i] = p = newParticle(1.02);
      var fade = Math.min(1, p.y * 3) * Math.min(1, (1 - p.y) * 4 + 0.2);
      ctx.globalAlpha = p.alpha * fade;
      ctx.fillStyle = i % 5 === 0 ? "#7C5CFF" : "#67E8F9";
      ctx.beginPath();
      ctx.arc(p.x * size.w, p.y * size.h, p.r * size.dpr, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  /* ---------- waveform ---------- */
  function targetLevel(t) {
    var api = window.abuai && window.abuai.voice;
    var live = voiceState === "live";
    if (!live) {
      // Not live audio: only a very subtle breathing line.
      return reduceMotion.matches ? 0.04 : 0.04 + 0.02 * Math.sin(t * 1.3);
    }
    var levels = api ? api.levels() : { input: null, output: null, mode: voiceMode };
    var speaking = (levels.mode || voiceMode) === "speaking";
    var real = speaking ? levels.output : levels.input;
    if (real !== null && real !== undefined) return Math.min(1, Math.sqrt(real) * 1.15);
    // SDK volume unavailable: fall back to mode-based levels.
    return (speaking ? 0.55 : 0.32) * (0.75 + 0.25 * Math.sin(t * 3));
  }

  function drawWave(t) {
    if (!waveCanvas) return;
    var size = fitCanvas(waveCanvas);
    var ctx = waveCanvas.getContext("2d");
    var target = targetLevel(t);
    waveLevel = reduceMotion.matches ? target : waveLevel * 0.72 + target * 0.28;
    ctx.clearRect(0, 0, size.w, size.h);
    var bars = Math.max(24, Math.min(64, Math.floor(size.w / (9 * size.dpr))));
    var gap = size.w / bars;
    var barW = Math.max(2, gap * 0.45);
    var mid = size.h / 2;
    var live = voiceState === "live";
    var color = live && voiceMode === "speaking" ? "#A78BFA" : "#67E8F9";
    ctx.fillStyle = color;
    ctx.shadowColor = color;
    ctx.shadowBlur = 8 * size.dpr;
    for (var i = 0; i < bars; i++) {
      var pos = i / (bars - 1);
      var envelope = Math.pow(Math.sin(Math.PI * pos), 1.4);
      var wobble = live && !reduceMotion.matches ? 0.5 + 0.5 * Math.abs(Math.sin(t * 7 + i * 0.73) * Math.cos(t * 3.1 + i * 0.29)) : 1;
      var amp = Math.max(1.5 * size.dpr, waveLevel * envelope * wobble * (size.h * 0.48));
      ctx.globalAlpha = 0.35 + 0.65 * envelope;
      ctx.fillRect(i * gap + (gap - barW) / 2, mid - amp, barW, amp * 2);
    }
    ctx.globalAlpha = 1;
    ctx.shadowBlur = 0;
  }

  /* ---------- loop: runs only while visible and motion is allowed ---------- */
  function frame(ms) {
    var t = ms / 1000;
    drawParticles();
    drawWave(t);
    rafId = window.requestAnimationFrame(frame);
  }

  function start() {
    if (rafId !== null || document.hidden || reduceMotion.matches) return;
    rafId = window.requestAnimationFrame(frame);
  }

  function stop() {
    if (rafId !== null) window.cancelAnimationFrame(rafId);
    rafId = null;
  }

  function syncMotion() {
    document.documentElement.classList.toggle("is-paused", document.hidden);
    if (document.hidden || reduceMotion.matches) {
      stop();
      if (reduceMotion.matches && particlesCanvas) {
        var size = fitCanvas(particlesCanvas);
        particlesCanvas.getContext("2d").clearRect(0, 0, size.w, size.h);
      }
      drawWave(0);
    } else {
      start();
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    core = document.getElementById("jarvis-core");
    if (!core) return;
    particlesCanvas = document.getElementById("core-particles");
    waveCanvas = document.getElementById("waveform");
    voiceLive = document.getElementById("voice-live");
    voiceLiveText = document.getElementById("voice-live-text");
    var talk = document.getElementById("talk-btn");
    if (talk) voiceState = talk.dataset.state || "idle";
    anyWorking = core.classList.contains("core-state-working");

    seedParticles();
    document.addEventListener("abuai:state", onState);
    document.addEventListener("abuai:voice-state", onVoiceState);
    document.addEventListener("visibilitychange", syncMotion);
    if (reduceMotion.addEventListener) reduceMotion.addEventListener("change", syncMotion);
    window.addEventListener("resize", function () { if (rafId === null) drawWave(0); });
    syncMotion();
  });
})();
