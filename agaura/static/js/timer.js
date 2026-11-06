/* AGaura — chronometer of the session console (FR-74), loaded by manage/_timer.html.

   The page may hold two copies: the card of the Projection screen rubric (#timer) and, during
   the session, the command of Steering (#cmd-timer); each is a [data-timer-url] element, handled
   by itself, never by its id. Counts on from the server's values (data-timer-*), with the
   arithmetic of services/timer.py, in every [data-timer] element (both copies and the status
   bar), on one clock corrected by the server's time. In the setup form of the card, shows the
   durations only for a countdown, and the minutes and seconds only with "Other duration", and
   keeps the line above "Start" in step with the folded settings. When manage.js reports a
   console event ("agaura:console-refreshed"), refetches both copies in one answer and replaces
   the state-dependent part of each copy only if its state changed, so a console open on another
   device follows a chronometer run from a phone, and a label being typed survives otherwise. No
   visible text here: every string comes from the template. */
(function () {
  "use strict";

  var MIN_INTERVAL = 1000;
  var TICK = 250;

  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-timer-url]"));
  if (!cards.length) return;
  var url = cards[0].getAttribute("data-timer-url");
  // Server clock minus local clock, in ms: a phone's clock may be wrong.
  var skew = Number(cards[0].getAttribute("data-server-time")) - Date.now() || 0;

  function pad(number) { return (number < 10 ? "0" : "") + number; }

  function clock(seconds) {
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    return (hours ? hours + ":" + pad(minutes) : String(minutes)) + ":" + pad(seconds % 60);
  }

  function tick() {
    var now = Date.now() + skew;
    // The clocks of both copies and the one of the console status bar.
    Array.prototype.forEach.call(document.querySelectorAll("[data-timer]"), function (el) {
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
      var face = el.querySelector("[data-timer-clock]");
      var note = el.querySelector("[data-timer-over]");
      if (face) face.textContent = (over ? "+" : "") + clock(seconds);
      el.classList.toggle("is-overtime", over);
      if (note) note.hidden = !over;
    });
  }

  // --- Setup form of the card: the durations matter only for a countdown, the minutes and
  // seconds only with "Other duration" (without script, everything stays visible).
  function applyMode(card) {
    var setup = card.querySelector("[data-timer-setup]");
    if (!setup) return;
    var mode = setup.querySelector("input[name=mode]:checked");
    var durations = setup.querySelector("[data-countdown-fields]");
    var custom = setup.querySelector("input[name=duration][value=custom]");
    var fields = setup.querySelector("[data-custom-fields]");
    var hint = setup.querySelector("[data-no-script-hint]");
    if (durations) durations.hidden = !mode || mode.value !== "countdown";
    if (fields) fields.hidden = !custom || !custom.checked;
    if (hint) hint.hidden = true;
  }

  // --- The line above "Start" repeats the folded settings: the texts of the chosen mode and
  // duration labels, the minutes and seconds typed for "Other duration", the label typed.
  function updateSummary(card) {
    var setup = card.querySelector("[data-timer-setup]");
    var summary = setup && setup.querySelector("[data-timer-summary]");
    if (!summary) return;
    var mode = setup.querySelector("input[name=mode]:checked");
    var countdown = Boolean(mode && mode.value === "countdown");
    var modeText = summary.querySelector("[data-summary-mode]");
    var modeLabel = mode && mode.parentNode.querySelector("strong");
    if (modeText && modeLabel) modeText.textContent = modeLabel.textContent;
    var duration = summary.querySelector("[data-summary-duration]");
    var chosen = setup.querySelector("input[name=duration]:checked");
    if (duration) {
      duration.hidden = !countdown || !chosen;
      if (chosen && chosen.value === "custom") {
        var minutes = Number(setup.querySelector("input[name=minutes]").value) || 0;
        var seconds = Number(setup.querySelector("input[name=seconds]").value) || 0;
        if (minutes || seconds) duration.textContent = clock(minutes * 60 + seconds);
        else duration.hidden = true;
      } else if (chosen) {
        duration.textContent = chosen.parentNode.querySelector("span").textContent;
      }
    }
    var label = summary.querySelector("[data-summary-label]");
    var typed = setup.querySelector("input[name=label]");
    if (label && typed) {
      label.textContent = typed.value.trim();
      label.hidden = !label.textContent;
    }
  }

  // --- Follow changes made from another console.

  // The status part without what the clock changes every second: a running chronometer does
  // not count as a change, so the buttons are not replaced under a finger.
  function signature(el) {
    var copy = el.cloneNode(true);
    Array.prototype.forEach.call(copy.querySelectorAll("[data-timer-clock]"), function (face) { face.textContent = ""; });
    Array.prototype.forEach.call(copy.querySelectorAll("[data-timer-over]"), function (note) { note.removeAttribute("hidden"); });
    Array.prototype.forEach.call(copy.querySelectorAll("[data-timer]"), function (face) { face.classList.remove("is-overtime"); });
    return copy.innerHTML;
  }

  var copies = cards.map(function (card) {
    var status = card.querySelector("[data-timer-status]");
    return { card: card, status: status, current: status ? signature(status) : null };
  });

  copies.forEach(function (copy) {
    var card = copy.card;
    card.addEventListener("input", function (event) {
      if (["label", "minutes", "seconds"].indexOf(event.target.name) !== -1) updateSummary(card);
    });
    card.addEventListener("change", function (event) {
      var name = event.target.name;
      if (name !== "mode" && name !== "duration") return;
      updateSummary(card);
      applyMode(card);
      if (name === "duration" && event.target.value === "custom") {
        var minutes = card.querySelector("[data-timer-setup] input[name=minutes]");
        if (minutes) minutes.focus();
      }
    });
    applyMode(card);
  });

  var lastFetch = 0;
  var pending = null;

  function refresh() {
    lastFetch = Date.now();
    var sent = Date.now();
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "fetch" } })
      .then(function (response) {
        if (!response.ok) return null; // logged out or session gone: manage.js reloads the page
        var received = Date.now();
        return response.text().then(function (html) { return { html: html, at: (sent + received) / 2 }; });
      })
      .then(function (result) {
        if (!result) return;
        var parsed = document.createElement("template");
        parsed.innerHTML = result.html;
        var section = parsed.content.querySelector("[data-server-time]");
        var serverTime = section && Number(section.getAttribute("data-server-time"));
        if (serverTime) skew = serverTime - result.at;
        copies.forEach(function (copy) {
          if (!copy.status) return;
          var fresh = parsed.content.querySelector('[id="' + copy.card.id + '"] [data-timer-status]');
          if (!fresh || signature(fresh) === copy.current) return;
          copy.current = signature(fresh);
          copy.status.innerHTML = fresh.innerHTML;
          applyMode(copy.card);
          updateSummary(copy.card);
        });
        tick();
      })
      .catch(function () { /* network hiccup: the next event retries */ });
  }

  function schedule() {
    if (pending) return;
    var wait = Math.max(0, lastFetch + MIN_INTERVAL - Date.now());
    pending = window.setTimeout(function () { pending = null; refresh(); }, wait);
  }

  // manage.js fires this after each console event (another console may have changed the time).
  if (url) document.addEventListener("agaura:console-refreshed", schedule);

  tick();
  window.setInterval(tick, TICK);
})();
