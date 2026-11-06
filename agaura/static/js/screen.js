/* AGaura — projection screen (FR-73, FR-74): refreshes itself with no interaction.

   Server-Sent Events on the console channel, at most two refreshes per second however many
   events arrive (a burst of ballots): an action of the tribune still shows within a second
   (FR-73) even right after a refresh. Polling every 5 s while the stream is down, and a slow
   refresh in any case. A 404 (link regenerated, session deleted) reloads the page, which then
   shows the 404 page. Poll countdowns and the chronometer follow the server's clock. No visible
   text here: every string comes from the templates. */
(function () {
  "use strict";

  var MIN_INTERVAL = 500;
  var POLL_INTERVAL = 5000;
  var SAFETY_INTERVAL = 30000;
  var SSE_RETRY = 15000;
  var SSE_FAILURES_BEFORE_POLLING = 3;
  var EXPIRED_REFRESH = 1500;
  var IDLE_AFTER = 3000;
  var MIN_FIT_LEVEL = -4; // default start, when a box sets no data-fit-min
  var MAX_FIT_LEVEL = 6;
  var TICK = 250;

  var root = document.getElementById("screen");
  if (!root) return;
  var stateUrl = root.getAttribute("data-state-url");
  var eventsUrl = root.getAttribute("data-events-url");
  var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  // Server clock minus local clock, in ms: the projector's clock may be wrong.
  var skew = Number(root.getAttribute("data-server-time")) - Date.now() || 0;

  // --- Fit: each box takes the largest type at which its content fits, from data-fit-min
  // (negative: larger than normal) down to the smallest level, so the page never scrolls.
  function overflowing(box) {
    return box.scrollHeight > box.clientHeight + 1 || box.scrollWidth > box.clientWidth + 1;
  }

  function fitBox(box) {
    var level = parseInt(box.getAttribute("data-fit-min"), 10);
    if (isNaN(level)) level = MIN_FIT_LEVEL;
    for (; level <= MAX_FIT_LEVEL; level += 1) {
      box.setAttribute("data-fit-level", String(level));
      if (!overflowing(box)) return;
    }
  }

  // A box as tall as its content (data-fit-first: the way to join beside other blocks) is
  // fitted first, so the boxes that share its column are fitted in the room it leaves.
  function fit() {
    each(root.querySelectorAll("[data-fit][data-fit-first]"), fitBox);
    each(root.querySelectorAll("[data-fit]:not([data-fit-first])"), fitBox);
  }

  // --- Countdowns (FR-71).
  function pad(number) { return (number < 10 ? "0" : "") + number; }

  function format(seconds) {
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var rest = seconds % 60;
    return (hours ? hours + ":" + pad(minutes) : pad(minutes)) + ":" + pad(rest);
  }

  // --- Chronometer (FR-74): the same arithmetic as services/timer.py. A countdown shows the
  // seconds left rounded up, then "+m:ss" past zero; counting up shows whole seconds counted.
  function clock(seconds) {
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    return (hours ? hours + ":" + pad(minutes) : String(minutes)) + ":" + pad(seconds % 60);
  }

  function tickTimer(el, now) {
    var started = el.getAttribute("data-timer-started");
    if (!started) return; // stopped: the server's text is exact
    var duration = el.getAttribute("data-timer-duration");
    var elapsed = Number(el.getAttribute("data-timer-elapsed")) * 1000 + Math.max(0, now - Number(started));
    var over = false;
    var seconds;
    if (!duration) {
      seconds = Math.floor(elapsed / 1000);
    } else {
      var left = Number(duration) * 1000 - elapsed;
      over = left <= 0;
      seconds = over ? Math.floor(-left / 1000) : Math.ceil(left / 1000);
    }
    var text = (over ? "+" : "") + clock(seconds);
    var face = el.querySelector("[data-timer-clock]");
    var note = el.querySelector("[data-timer-over]");
    if (!face || face.textContent === text) return;
    var refit = face.textContent.length !== text.length || el.classList.contains("is-overtime") !== over;
    face.textContent = text;
    el.classList.toggle("is-overtime", over);
    if (note) note.hidden = !over;
    if (refit) fitBox(el); // "9:59" to "+0:00", "59:59" to "1:00:00": the width changes
  }

  function tick() {
    var now = Date.now() + skew;
    each(root.querySelectorAll("[data-timer]"), function (el) { tickTimer(el, now); });
    each(root.querySelectorAll("[data-closes-at]"), function (el) {
      var left = Math.max(0, Math.ceil((Number(el.getAttribute("data-closes-at")) - now) / 1000));
      var value = el.querySelector("[data-countdown-value]");
      var fixed = el.querySelector("[data-countdown-fixed]");
      var live = el.querySelector("[data-countdown-live]");
      if (!value || !fixed || !live) return;
      value.textContent = format(left);
      fixed.hidden = true;
      live.hidden = false;
      el.classList.toggle("urgent", left <= 60);
      if (left === 0 && !el.hasAttribute("data-expired")) {
        el.setAttribute("data-expired", "");
        window.setTimeout(schedule, EXPIRED_REFRESH); // the poll stops accepting votes
      }
    });
  }

  // --- Refresh: the whole content is replaced, only when it changed.
  var current = null;
  var loading = false;
  var again = false;
  var lastFetch = 0;
  var pending = null;

  function refresh() {
    if (loading) { again = true; return; }
    loading = true;
    lastFetch = Date.now();
    var sent = Date.now();
    // No cookie: the screen needs none, and must not consume the console's notices.
    fetch(stateUrl, { credentials: "omit", cache: "no-store", headers: { "X-Requested-With": "fetch" } })
      .then(function (response) {
        if (response.status === 404) { stop(); window.location.reload(); return null; }
        if (!response.ok) return null;
        var serverTime = Number(response.headers.get("X-Server-Time"));
        if (serverTime) skew = serverTime - (sent + Date.now()) / 2;
        return response.text();
      })
      .then(function (html) {
        if (html === null || html === current) return;
        current = html;
        root.innerHTML = html;
        tick();
        fit();
      })
      .catch(function () { /* offline: the next event or poll retries */ })
      .then(function () {
        loading = false;
        if (again) { again = false; schedule(); }
      });
  }

  function schedule() {
    if (pending) return;
    var wait = Math.max(0, lastFetch + MIN_INTERVAL - Date.now());
    pending = window.setTimeout(function () { pending = null; refresh(); }, wait);
  }

  // --- Real time: Server-Sent Events, polling fallback, automatic reconnection.
  var source = null;
  var failures = 0;
  var pollTimer = null;

  function startPolling() { if (!pollTimer) pollTimer = window.setInterval(schedule, POLL_INTERVAL); }
  function stopPolling() { if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; } }

  function stop() {
    stopPolling();
    if (source) { source.close(); source = null; }
  }

  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    source = new EventSource(eventsUrl);
    source.addEventListener("turnout", schedule);
    source.onopen = function () {
      failures = 0;
      stopPolling();
      schedule(); // catch up with anything missed while disconnected
    };
    source.onerror = function () {
      failures += 1;
      if (source && source.readyState === 2) { // closed for good (404): poll, retry later
        source = null;
        startPolling();
        schedule();
        window.setTimeout(connect, SSE_RETRY);
      } else if (failures >= SSE_FAILURES_BEFORE_POLLING) {
        startPolling(); // the browser keeps reconnecting meanwhile
      }
    };
  }

  // --- Full screen button, hidden cursor, screen kept awake.
  var button = document.querySelector("[data-fullscreen]");
  var page = document.documentElement;
  if (button && document.fullscreenEnabled && page.requestFullscreen) {
    button.hidden = false;
    button.addEventListener("click", function () {
      page.requestFullscreen().catch(function () { /* refused: stay as is */ });
    });
    document.addEventListener("fullscreenchange", function () {
      button.hidden = Boolean(document.fullscreenElement);
    });
  }

  var idleTimer = null;
  function awake() {
    document.body.classList.remove("idle");
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(function () { document.body.classList.add("idle"); }, IDLE_AFTER);
  }
  document.addEventListener("mousemove", awake);
  document.addEventListener("keydown", awake);
  awake();

  var wakeLock = null;
  function keepAwake() {
    if (!navigator.wakeLock || document.hidden || wakeLock) return;
    navigator.wakeLock.request("screen").then(function (lock) {
      wakeLock = lock;
      lock.addEventListener("release", function () { wakeLock = null; });
    }).catch(function () { /* not allowed (plain HTTP, battery saver): ignore */ });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    keepAwake();
    schedule();
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(fit, 200);
  });

  tick();
  fit();
  keepAwake();
  window.setInterval(tick, TICK);
  window.setInterval(schedule, SAFETY_INTERVAL);
  connect();
})();
