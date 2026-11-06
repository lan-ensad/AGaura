/* AGaura — voter pages: automatic join from the QR code, live updates, inline ballot steps.
   Every page works without this script (NFR-05). Visible strings come from data- attributes
   rendered by the templates; no inline code anywhere (strict CSP). */
(function () {
  "use strict";

  var RELOAD_STATUSES = [401, 403, 404]; // credential gone, expired form, poll gone
  var POLL_INTERVAL = 5000;
  var SSE_RETRY = 15000;
  var SSE_FAILURES_BEFORE_POLLING = 3;
  var RELOAD_LATER = 20000;
  var NET_GRACE = 5000; // without the stream nor a refresh this long: the connection is lost
  var NET_BACK = 3000; // "Connexion rétablie" stays this long
  var REFRESH_TIMEOUT = 10000; // a refresh with no answer by then gives up: the next one retries
  var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  function withoutHash() { return location.pathname + location.search; }

  // --- Join (FR-22): the QR code carries the password or code in the fragment (#k=...), which
  // browsers never send to the server. Read it, remove it from the address bar, submit.
  function autoJoin() {
    var match = /^#k=(.+)$/.exec(location.hash);
    if (!match) return;
    var form = document.querySelector("form[data-autojoin]");
    if (!form && document.querySelector("[data-reload-later]")) return; // not started: keep it
    history.replaceState(null, "", withoutHash());
    var secret = "";
    try { secret = decodeURIComponent(match[1]); } catch (error) { secret = ""; }
    if (!form || !secret) return;
    form.elements.secret.value = secret;
    var note = document.querySelector("[data-joining]");
    if (note) note.hidden = false;
    form.submit();
  }

  autoJoin();
  if (document.querySelector("[data-reload-later]")) {
    setTimeout(function () { location.reload(); }, RELOAD_LATER);
  }

  var live = document.getElementById("live");
  if (!live) return;

  function parse(html) {
    var template = document.createElement("template");
    template.innerHTML = html;
    return template.content;
  }

  // Strings with a placeholder ({n}, {title}); a function keeps "$" in the value as it is.
  function fill(template, name, value) {
    return template.replace("{" + name + "}", function () { return value; });
  }

  // A text of a counter: "-one" or "-other" with the plural rule of the catalogs
  // (agaura/i18n.py: singular up to 1), the count in place of {n}.
  function plural(el, name, n) {
    return fill(el.getAttribute(name + (n <= 1 ? "-one" : "-other")), "n", String(n));
  }

  // A counter is a status the screen reader reads out: only changes are written.
  function show(counter, text, help, state) {
    if (counter.textContent !== text + help) {
      counter.textContent = text;
      if (help) {
        var line = document.createElement("span");
        line.className = "counter-help";
        line.textContent = help;
        counter.appendChild(line);
      }
    }
    ["is-full", "is-over", "is-complete"].forEach(function (name) {
      counter.classList.toggle(name, name === state);
    });
    counter.hidden = false;
  }

  // At the maximum of an approval ballot, a box left unchecked refuses a tick: aria-disabled,
  // not disabled, so that it stays focusable and its description (the counter) says why.
  function block(box, blocked) {
    if (blocked) box.setAttribute("aria-disabled", "true"); else box.removeAttribute("aria-disabled");
    var row = box.closest("label");
    if (row) row.classList.toggle("is-blocked", blocked);
  }

  // --- Counters, right after the options, in what remains (SPEC §6.5): approvals ("Encore 1
  // choix possible", "Maximum atteint : 2 choix sur 2"), graded options of Majority Judgment
  // ("1 option sur 3 évaluée"), ranked options of Condorcet ("2 options sur 5 classées").
  function count(form) {
    var counter = form.querySelector("[data-counter]");
    if (counter) {
      var boxes = form.querySelectorAll("input[name=approved]");
      var checked = form.querySelectorAll("input[name=approved]:checked").length;
      var max = parseInt(counter.getAttribute("data-max"), 10) || 0;
      if (!max) show(counter, plural(counter, "data-count", checked), "", "");
      else if (checked < max) show(counter, plural(counter, "data-left", max - checked), "", "");
      else if (checked === max) {
        show(counter, counter.getAttribute("data-full"), counter.getAttribute("data-help"), "is-full");
      } else show(counter, plural(counter, "data-over", checked - max), "", "is-over");
      each(boxes, function (box) { block(box, max > 0 && checked >= max && !box.checked); });
    }
    var graded = form.querySelector("[data-graded]");
    if (graded) {
      var n = form.querySelectorAll("input[name^='grade-']:checked").length;
      var total = parseInt(graded.getAttribute("data-total"), 10);
      show(graded, plural(graded, "data", n), "", n === total ? "is-complete" : "");
    }
    var ranked = form.querySelector("[data-ranked]");
    if (ranked) {
      var r = 0;
      each(form.querySelectorAll("select[name^='rank-']"), function (list) { if (isRanked(list)) r += 1; });
      var options = parseInt(ranked.getAttribute("data-total"), 10);
      show(ranked, plural(ranked, "data", r), "", r === options ? "is-complete" : "");
    }
  }

  // A tap on a blocked box changes nothing; the counter is read out again.
  function remind(form) {
    var counter = form.querySelector("[data-counter]");
    if (!counter) return;
    var nodes = Array.prototype.slice.call(counter.childNodes);
    counter.textContent = "";
    // A short pause between emptying and filling, so that screen readers notice the change.
    setTimeout(function () {
      if (counter.textContent) return; // written again meanwhile
      nodes.forEach(function (node) { counter.appendChild(node); });
    }, 100);
  }

  function enhance(root) { each(root.querySelectorAll("form"), count); }

  // "Non classée" in a Condorcet list: "00", as long as a rank (ballot_forms.UNRANKED), so
  // that a request's size never tells how many options were ranked.
  var UNRANKED = "00";
  function isRanked(list) { return list.value !== "" && list.value !== UNRANKED; }

  // --- The blank vote excludes every other choice (SPEC §6.1): checking its box clears the
  // options, grades and ranks, choosing one of them clears the box. Single choice needs
  // nothing: its blank is a radio button of the options' group. Without script the server
  // refuses both.
  function exclusive(input) {
    var form = input.form;
    if (input.tagName === "SELECT") {
      if (!isRanked(input)) return;
    } else if (!input.checked) return;
    if (input.type === "checkbox" && input.hasAttribute("data-blank")) {
      each(form.querySelectorAll("input[name=approved], input[name^='grade-']"), function (other) {
        other.checked = false;
      });
      each(form.querySelectorAll("select[name^='rank-']"), function (list) { list.value = UNRANKED; });
    } else if (input.name === "approved" || /^(grade|rank)-/.test(input.name)) {
      var blank = form.querySelector("input[type=checkbox][data-blank]");
      if (blank) blank.checked = false;
    }
  }

  live.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.form || (input.tagName !== "INPUT" && input.tagName !== "SELECT")) return;
    exclusive(input);
    count(input.form);
  });

  live.addEventListener("click", function (event) {
    var input = event.target;
    if (input.tagName !== "INPUT" || !input.form || input.getAttribute("aria-disabled") !== "true") return;
    event.preventDefault(); // the box stays unchecked; the server would refuse it (FR-44)
    remind(input.form);
  });

  // --- Reading position. Content added or removed above what the voter reads (a poll pushed
  // on top, a summary that grows) must not move it: the page scrolls by the same amount. The
  // browser's own scroll anchoring is off (live.css), so the two never add up, and Safari,
  // which has none, behaves the same.
  function summaryOf(root) {
    for (var i = 0; i < root.children.length; i += 1) {
      if (root.children[i].hasAttribute("data-summary")) return root.children[i];
    }
    return null;
  }

  // The top of what the voter can read: under the summary once it sticks to the top.
  function readingLine() {
    var summary = summaryOf(live);
    if (!summary) return 0;
    var box = summary.getBoundingClientRect();
    return box.top <= 1 ? box.bottom : 0;
  }

  function identity(el) { return el.id || el.getAttribute("data-key"); }

  function find(id) {
    for (var i = 0; i < live.children.length; i += 1) {
      if (identity(live.children[i]) === id) return live.children[i];
    }
    return null;
  }

  function keepReading(change) {
    var line = readingLine();
    var anchor = null;
    // Nothing to keep while the top of the list is in view: what appears there is seen.
    if (live.getBoundingClientRect().top < line) {
      for (var i = 0; i < live.children.length && !anchor; i += 1) {
        var el = live.children[i];
        var box = el.getBoundingClientRect();
        if (!el.hasAttribute("data-summary") && box.bottom > line) {
          anchor = { id: identity(el), offset: box.top - line };
        }
      }
    }
    change();
    var target = anchor && find(anchor.id); // gone (closed poll): let the page flow
    // Twice: the first scroll may make the summary stick, which moves the reading line.
    for (var pass = 0; target && pass < 2; pass += 1) {
      var shift = target.getBoundingClientRect().top - readingLine() - anchor.offset;
      if (Math.abs(shift) >= 1) window.scrollBy(0, shift);
    }
  }

  // --- Announcements: a poll pushed after the page was loaded is read out ("New vote open:
  // …"), without moving the focus.
  var announcer = document.querySelector("[data-announce]");
  function announce(titles) {
    if (!announcer || !titles.length) return;
    var template = announcer.getAttribute("data-msg-new-poll");
    announcer.textContent = "";
    // A short pause between emptying and filling, so that screen readers notice the change.
    setTimeout(function () {
      titles.forEach(function (title) {
        var line = document.createElement("p");
        line.textContent = fill(template, "title", title);
        announcer.appendChild(line);
      });
    }, 100);
  }

  // --- The summary of the open polls says which poll waits for its confirmation on this
  // device ("À confirmer · …"): a ballot step the server keeps no trace of.
  function titleOf(section) {
    var heading = document.getElementById(section.id + "-title");
    return heading ? heading.textContent.trim() : "";
  }

  function pollOf(link) {
    var href = link.getAttribute("href") || "";
    return href.charAt(0) === "#" ? document.getElementById(href.slice(1)) : null;
  }

  function markSummary() {
    var summary = summaryOf(live);
    if (!summary) return;
    var template = summary.getAttribute("data-msg-review");
    each(summary.querySelectorAll("a"), function (link) {
      var poll = pollOf(link);
      if (!poll) return;
      var review = poll.classList.contains("poll-review");
      var text = review ? fill(template, "title", titleOf(poll)) : titleOf(poll);
      if (link.textContent !== text) link.textContent = text;
      link.classList.toggle("is-review", review);
    });
  }

  // --- Right after a vote, "Vote suivant : …" leads to a poll still to vote (a link of the
  // summary): the first one below, else the first one above (arrow up). The server always
  // sends this link empty, so its target never changes the size of a ballot step.
  function nextVotes() {
    var summary = summaryOf(live);
    each(live.querySelectorAll("[data-next-vote]"), function (holder) {
      var section = holder.closest("[data-key]");
      var below = null;
      var first = null;
      each(summary ? summary.querySelectorAll("a") : [], function (link) {
        var poll = pollOf(link);
        if (!poll || poll === section || !live.contains(poll)) return;
        if (!first) first = poll;
        if (!below && (section.compareDocumentPosition(poll) & 4)) below = poll; // following
      });
      var target = below || first;
      holder.hidden = !target;
      if (!target) return;
      var link = holder.querySelector("a");
      link.setAttribute("href", "#" + target.id);
      link.querySelector("span").textContent = fill(link.getAttribute("data-msg"), "title", titleOf(target));
      holder.classList.toggle("is-above", !below);
    });
  }

  function marks() { markSummary(); nextVotes(); }

  // --- Refresh: sections are keyed (data-key). A section whose key is unchanged is kept as it
  // is, so a ballot being filled in is never wiped; new ones are added, others removed. A
  // section that holds the focus (the confirmation of a vote, folded to one line on the page)
  // also stays until the focus leaves it: a refresh never moves nor drops the focus. Sections
  // that go are removed first, so that those kept are never moved (moving one drops its focus).
  function merge(html) {
    var current = {};
    var present = {};
    each(live.children, function (el) {
      current[el.getAttribute("data-key")] = el;
      if (el.id) present[el.id] = el;
    });
    var focused = document.activeElement;
    var wanted = [];
    var added = [];
    var fresh = [];
    var seen = {};
    each(Array.prototype.slice.call(parse(html).children), function (node) {
      var key = node.getAttribute("data-key");
      var old = node.id && present[node.id];
      if (!current[key] && old && old.contains(focused)) key = old.getAttribute("data-key");
      var el = current[key] || node;
      seen[key] = true;
      wanted.push(el);
      if (el !== node) return;
      added.push(el);
      if (/^poll-\d+-open$/.test(key) && !present[el.id]) {
        var title = el.querySelector("#" + el.id + "-title");
        if (title) fresh.push(title.textContent.trim());
      }
    });
    keepReading(function () {
      Object.keys(current).forEach(function (key) {
        if (!seen[key] && current[key].parentNode === live) live.removeChild(current[key]);
      });
      wanted.forEach(function (el, index) {
        if (live.children[index] !== el) live.insertBefore(el, live.children[index] || null);
      });
    });
    added.forEach(enhance);
    marks();
    if (focused && focused !== document.activeElement && live.contains(focused)) {
      focused.focus({ preventScroll: true });
    }
    announce(fresh);
  }

  // After a vote, only the summary changes ("To vote: 2 · Recorded: 1"): the full confirmation
  // the voter is reading stays until the next refresh folds it to one line.
  function mergeSummary(html) {
    var fresh = summaryOf(parse(html));
    var old = summaryOf(live);
    if (!fresh || !old || fresh.getAttribute("data-key") === old.getAttribute("data-key")) return;
    keepReading(function () { live.replaceChild(fresh, old); });
    marks();
  }

  // ``scope`` "summary" refreshes the summary alone; anything else (an event) everything.
  var loading = false;
  var queued = null;
  function refresh(scope) {
    scope = scope === "summary" ? "summary" : "all";
    if (loading) { if (queued !== "all") queued = scope; return; }
    loading = true;
    // A network that drops packets leaves a request hanging: give it up, so polls go on.
    var abort = window.AbortController ? new AbortController() : null;
    var timeout = abort && setTimeout(function () { abort.abort(); }, REFRESH_TIMEOUT);
    fetch(live.getAttribute("data-live-url"), {
      credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "fetch" },
      signal: abort ? abort.signal : undefined
    }).then(function (response) {
      if (RELOAD_STATUSES.indexOf(response.status) >= 0) { location.reload(); return null; }
      if (response.status >= 500) throw new Error(String(response.status));
      reached();
      return response.ok ? response.text() : null;
    }).then(function (html) {
      if (html === null) return;
      if (scope === "summary") mergeSummary(html); else merge(html);
    }).catch(function () { trouble(); /* the next event or poll retries */ }).then(function () {
      clearTimeout(timeout);
      loading = false;
      var next = queued;
      queued = null;
      if (next) refresh(next);
    });
  }

  // --- Lost connection (FR-42, R10): after NET_GRACE without the live stream nor an answer of
  // the server, a banner fixed at the bottom of the screen says so (a status, read out
  // politely); once the network is back it says so for NET_BACK, then empties. It covers the
  // bottom of the page and never pushes it: the page gains room below the last line once
  // (net-room, kept, since a page that shrinks moves what is read at its bottom), and focus
  // stops short of the banner while it shows (net-trouble).
  var banner = document.querySelector("[data-net-banner]");
  var source = null;
  var lost = false;
  var lostTimer = null;
  var backTimer = null;

  function setBanner(state) {
    if (!banner) return;
    clearTimeout(backTimer);
    backTimer = null;
    var text = banner.querySelector("[data-net-text]");
    text.textContent = "";
    if (state === "lost") {
      var title = document.createElement("strong");
      title.textContent = banner.getAttribute("data-msg-lost");
      var help = document.createElement("span");
      help.className = "net-help";
      help.textContent = banner.getAttribute("data-msg-help");
      text.appendChild(title);
      text.appendChild(document.createTextNode(" "));
      text.appendChild(help);
    } else if (state === "back") {
      text.textContent = banner.getAttribute("data-msg-back");
    }
    banner.classList.toggle("is-lost", state === "lost");
    banner.classList.toggle("is-back", state === "back");
    document.documentElement.classList.toggle("net-trouble", state === "lost");
    if (state === "lost") document.documentElement.classList.add("net-room");
  }

  // The server answered (the stream opened or spoke, a refresh or a ballot step came back).
  function reached() {
    clearTimeout(lostTimer);
    lostTimer = null;
    if (!lost) return;
    lost = false;
    setBanner("back");
    backTimer = setTimeout(function () { setBanner(null); }, NET_BACK);
  }

  // Something failed (the stream dropped, a request got no answer): the banner shows unless the
  // server answers within NET_GRACE. A refresh asks it at once, so a stream refused while the
  // network works never shows the banner.
  function trouble() {
    if (lost || lostTimer) return;
    lostTimer = setTimeout(function () {
      lostTimer = null;
      lost = true;
      setBanner("lost");
      startPolling(); // the first answer takes the banner away
    }, NET_GRACE);
    refresh();
  }

  window.addEventListener("offline", trouble);
  window.addEventListener("online", function () { refresh(); });

  // --- Real time: Server-Sent Events, polling fallback (FR-42).
  var timer = null;
  var failures = 0;
  function startPolling() { if (!timer) timer = setInterval(refresh, POLL_INTERVAL); }
  function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    source = new EventSource(live.getAttribute("data-events-url"));
    source.addEventListener("update", function () { reached(); refresh(); });
    source.onopen = function () {
      failures = 0;
      reached();
      stopPolling();
      refresh(); // catch up with anything missed while disconnected
    };
    source.onerror = function () {
      failures += 1;
      trouble();
      if (source.readyState === 2) { // closed for good (e.g. 204): poll, retry later
        startPolling();
        refresh();
        setTimeout(connect, SSE_RETRY);
      } else if (failures >= SSE_FAILURES_BEFORE_POLLING) {
        startPolling(); // the browser keeps reconnecting meanwhile
      }
    };
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });

  // --- Ballot steps inline: summary, change, confirmation (FR-43). The server answers with the
  // poll's section, which replaces the current one. While a step is sent, its section is busy
  // and a second tap does nothing; the buttons stay focusable (aria-disabled).
  function setBusy(form, busy) {
    var section = form.closest("[data-key]");
    if (busy) section.setAttribute("aria-busy", "true"); else section.removeAttribute("aria-busy");
    each(form.querySelectorAll("button"), function (button) {
      if (busy) button.setAttribute("aria-disabled", "true"); else button.removeAttribute("aria-disabled");
    });
  }

  function isBusy(form) {
    var section = form.closest("[data-key]");
    return !!section && section.getAttribute("aria-busy") === "true";
  }

  function networkError(section) {
    if (section.querySelector("[data-network-error]")) return;
    var alert = document.createElement("p");
    alert.className = "alert alert-error";
    alert.setAttribute("role", "alert");
    alert.setAttribute("data-network-error", "");
    alert.textContent = live.getAttribute("data-msg-network");
    var head = section.querySelector(".poll-head");
    section.insertBefore(alert, head ? head.nextSibling : section.firstChild);
  }

  // Every ballot-step request of a poll has the same size (data-pad, computed by the server
  // from the poll alone), so its size never tells the choice: a "pad" field the server ignores
  // fills the gap. The answers are padded by the server likewise.
  var PAD_SEPARATOR = "&pad=".length;
  function pad(form, body) {
    var size = parseInt(form.getAttribute("data-pad"), 10);
    var missing = size - body.toString().length - PAD_SEPARATOR;
    if (missing >= 0) body.append("pad", "x".repeat(missing));
  }

  live.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button") : null;
    if (button && button.form) button.form.lastSubmitter = button;
  });

  live.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute("data-live-form") || !window.fetch || !window.URLSearchParams) return;
    event.preventDefault();
    if (isBusy(form)) return;
    var button = event.submitter || form.lastSubmitter || null;
    form.lastSubmitter = null;
    var body = new URLSearchParams(new FormData(form));
    if (button && button.name) body.append(button.name, button.value);
    pad(form, body);
    var url = (button && button.getAttribute("formaction")) || form.getAttribute("action");
    var section = form.closest("[data-key]");
    setBusy(form, true);
    fetch(url, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch", "X-CSRF-Token": form.elements.csrf_token.value }
    }).then(function (response) {
      if (RELOAD_STATUSES.indexOf(response.status) >= 0) { location.reload(); return null; }
      if (response.status >= 500) throw new Error(String(response.status));
      reached();
      return response.text();
    }).then(function (html) {
      if (html === null) return;
      var node = parse(html).firstElementChild;
      if (!node || !section.parentNode) throw new Error("unexpected answer");
      section.parentNode.replaceChild(node, section);
      enhance(node);
      marks();
      var heading = node.querySelector("[data-focus]");
      if (heading) heading.focus();
      if (/-voted$/.test(node.getAttribute("data-key") || "")) refresh("summary");
    }).catch(function () {
      // Safe to retry: a ballot that did go through answers "vote already recorded".
      setBusy(form, false);
      networkError(section);
      trouble();
    });
  });

  enhance(live);
  marks();
  connect();
})();
