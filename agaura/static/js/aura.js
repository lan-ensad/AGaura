/* AGaura — aura battle on the voter page (SPEC §6.8): a game, not a ballot.

   Each section [data-aura] counts down from the milliseconds the server wrote when it drew the
   page (data-choose-ms, data-end-ms), never with the phone's own clock. During the choice, the
   option is a radio button that belongs to no form; at the end of the choice it is fixed and a
   large button appears, unless nothing was picked. Each tap (a finger down, Space or Enter, a
   click from assistive technology) counts once; the taps leave in batches every BATCH ms, as
   "o=NN&n=NN" (two digits each, so that every request of a battle has one size whatever the
   option), until the end. The option never leaves the phone otherwise, and nothing is stored on
   it. No visible text here: every string comes from data- attributes. */
(function () {
  "use strict";

  var BATCH = 500;
  var MAX_BATCH = 99;
  var TICK = 100;
  var CLICK_AFTER_POINTER = 600; // a click that follows a finger down is the same tap
  var STOP_STATUSES = [401, 403, 404, 409];

  var live = document.getElementById("live");
  if (!live || !window.fetch || !window.performance) return;

  function two(n) { return (n < 10 ? "0" : "") + n; }

  function setup(section) {
    if (section.getAttribute("data-aura-ready")) return;
    section.setAttribute("data-aura-ready", "1");
    var game = section.querySelector("[data-aura-game]");
    var nojs = section.querySelector("[data-aura-nojs]");
    var status = section.querySelector("[data-aura-status]");
    var countdown = section.querySelector("[data-aura-countdown]");
    var options = section.querySelector("[data-aura-options]");
    var button = section.querySelector("[data-aura-button]");
    var label = section.querySelector("[data-aura-label]");
    var counter = section.querySelector("[data-aura-count]");
    if (!game || !button) return;
    if (nojs) nojs.hidden = true;
    game.hidden = false;

    var start = performance.now();
    var tapAt = start + (Number(section.getAttribute("data-choose-ms")) || 0);
    var endAt = start + (Number(section.getAttribute("data-end-ms")) || 0);
    var url = section.getAttribute("data-tap-url");
    var csrf = section.getAttribute("data-csrf");
    var phase = "";
    var option = "";
    var pending = 0;
    var count = 0;
    var stopped = false;
    var lastPointer = -Infinity;
    var lastKey = -Infinity;
    var sender = null;

    function say(name) { status.textContent = status.getAttribute("data-msg-" + name) || ""; }

    function flush() {
      if (!pending || stopped || !option) return;
      var n = Math.min(pending, MAX_BATCH);
      pending = 0; // beyond what one request carries, taps would not count anyway
      fetch(url, {
        method: "POST",
        body: "o=" + option + "&n=" + two(n),
        credentials: "same-origin",
        keepalive: true,
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrf,
          "X-Requested-With": "fetch"
        }
      }).then(function (response) {
        if (STOP_STATUSES.indexOf(response.status) >= 0) stopped = true;
      }).catch(function () { /* a lost batch is lost: the next one goes on */ });
    }

    function tap() {
      if (phase !== "tap") return;
      pending += 1;
      count += 1;
      counter.textContent = String(count);
      button.classList.remove("is-hit");
      void button.offsetWidth; // restart the animation of each tap
      button.classList.add("is-hit");
      if (navigator.vibrate) navigator.vibrate(8);
    }

    function lock() {
      Array.prototype.forEach.call(options.querySelectorAll("input"), function (input) {
        input.disabled = true;
      });
    }

    function toTaps() {
      phase = "tap";
      var checked = options.querySelector("input:checked");
      lock();
      if (!checked) { phase = "late"; say("late"); return; }
      option = checked.value;
      section.classList.add("is-tapping");
      Array.prototype.forEach.call(options.querySelectorAll(".choice"), function (row) {
        row.hidden = !row.contains(checked);
      });
      label.textContent = checked.getAttribute("data-label") || "";
      button.hidden = false;
      say("tap");
      sender = setInterval(flush, BATCH);
    }

    function toOver() {
      var tapped = phase === "tap";
      phase = "over";
      clearInterval(sender);
      flush();
      if (tapped) button.disabled = true;
      section.classList.remove("is-tapping");
      section.classList.add("is-over");
      countdown.textContent = "";
      lock();
      say("over");
    }

    function tick() {
      var now = performance.now();
      if (now < tapAt) {
        if (phase !== "choose") { phase = "choose"; say("choose"); }
        countdown.textContent = String(Math.ceil((tapAt - now) / 1000));
      } else if (now < endAt) {
        if (phase === "choose" || phase === "") toTaps();
        countdown.textContent = String(Math.ceil((endAt - now) / 1000));
      } else {
        toOver();
        return;
      }
      setTimeout(tick, TICK);
    }

    button.addEventListener("pointerdown", function (event) {
      event.preventDefault(); // no focus ring flash, no double-tap zoom, no text selection
      lastPointer = performance.now();
      tap();
    });
    button.addEventListener("keydown", function (event) {
      if (event.key !== " " && event.key !== "Enter") return;
      event.preventDefault();
      lastKey = performance.now();
      if (!event.repeat) tap(); // holding a key down taps once
    });
    button.addEventListener("click", function (event) {
      event.preventDefault();
      // Neither a finger nor a key: a click from assistive technology is one tap.
      var now = performance.now();
      if (event.detail === 0 && now - lastPointer > CLICK_AFTER_POINTER &&
          now - lastKey > CLICK_AFTER_POINTER) tap();
    });
    button.addEventListener("contextmenu", function (event) { event.preventDefault(); });

    tick();
  }

  function scan(root) {
    if (root.matches && root.matches("[data-aura]")) setup(root);
    if (root.querySelectorAll) Array.prototype.forEach.call(root.querySelectorAll("[data-aura]"), setup);
  }

  scan(live);
  // live.js adds the section of a battle pushed while the page is open.
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        Array.prototype.forEach.call(record.addedNodes, function (node) {
          if (node.nodeType === 1) scan(node);
        });
      });
    }).observe(live, { childList: true, subtree: true });
  }
})();
