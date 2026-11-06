// Session console (manage/console.html): one rubric shown at a time, confirmations, copy buttons,
// print, links handed over in place from a card ("Confier"), and the live parts refreshed on
// server events (or every 5 s), whose forms are sent in place. No visible text here: every
// string comes from the templates.
(function () {
  "use strict";

  var consoleRoot = document.querySelector("[data-console]");
  var currentRubric = null;

  // The notice of the page (main > .alert) speaks of the action that loaded it: the next one,
  // sent in place (a live form, a card's own script), would leave it stale on top of the page.
  function clearNotices() {
    Array.prototype.forEach.call(document.querySelectorAll("#main > .alert"), function (alert) {
      alert.remove();
    });
  }
  document.addEventListener("submit", clearNotices, true);

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm") ||
      (event.submitter && event.submitter.getAttribute("data-confirm"));
    if (message && !window.confirm(message)) { event.preventDefault(); return; }
    if (event.defaultPrevented || !consoleRoot) return;
    // Come back to the rubric the form was sent from: the server redirects to the console
    // without a fragment, and the browser keeps the one of the address it posted to.
    var action = form.getAttribute("action");
    var rubric = form.closest("[data-rubric]");
    var target = rubric ? rubric.id : currentRubric;
    if (target && action && action.indexOf("#") === -1 && form.method.toLowerCase() === "post" &&
        !form.hasAttribute("data-leaves-console")) {
      form.setAttribute("action", action + "#" + target);
    }
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-print]")) window.print();
  });

  // Copy buttons: data-copy = id of the field, data-copied = confirmation text. Shown only where
  // the clipboard works, also in parts swapped in later (armCopy).
  function armCopy(root) {
    if (!navigator.clipboard) return;
    Array.prototype.forEach.call(root.querySelectorAll("[data-copy]"), function (button) { button.hidden = false; });
  }
  armCopy(document);
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button || !navigator.clipboard) return;
    var field = document.getElementById(button.getAttribute("data-copy"));
    if (!field) return;
    navigator.clipboard.writeText(field.value).then(function () {
      var label = button.textContent;
      button.textContent = button.getAttribute("data-copied");
      window.setTimeout(function () { button.textContent = label; }, 2000);
    });
  });

  // "⋯" menus and "Confier" (details[data-menu]): Escape closes them; a click elsewhere closes
  // the "⋯" menus only, since "Confier" opens a form over its card that a stray tap must keep.
  function menus() { return Array.prototype.slice.call(document.querySelectorAll("[data-menu]")); }
  document.addEventListener("click", function (event) {
    menus().forEach(function (menu) {
      if (menu.open && !menu.classList.contains("hand-over") && !menu.contains(event.target)) {
        menu.open = false;
      }
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    menus().forEach(function (menu) {
      if (!menu.open) return;
      menu.open = false;
      menu.querySelector("summary").focus();
    });
  });

  // "Confier" (details.hand-over): a toggle whose panel floats over its card (manage.css), one
  // open at a time (their shared name). Opened, the page scrolls just enough to show the whole
  // panel above the bottom tabs of a phone (its scroll-margin-bottom), never so far that the
  // toggle leaves the window or slips under the status bar of a computer (its scroll-margin-top);
  // scrollIntoView "nearest" would ignore that margin. Its link to Settings closes it on the way
  // out. Closed on a link just created, by its toggle, Escape or "Fermer" (data-hand-over-close,
  // shown with script only), the panel gives the form back, for the next person.
  function armClose(root) {
    Array.prototype.forEach.call(root.querySelectorAll("[data-hand-over-close]"), function (button) { button.hidden = false; });
  }
  armClose(document);
  function reveal(menu, panel) {
    var margin = function (el, side) { return parseFloat(window.getComputedStyle(el)[side]) || 0; };
    var below = panel.getBoundingClientRect().bottom + margin(panel, "scrollMarginBottom") - window.innerHeight;
    var above = menu.getBoundingClientRect().top - margin(menu, "scrollMarginTop");
    if (below > 0 && above > 0) window.scrollBy(0, Math.min(below, above));
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-hand-over-close], .hand-over-panel a[href]");
    var menu = button && button.closest(".hand-over");
    if (!menu) return;
    menu.open = false;  // also when its link leads to Settings: the panel stays behind, closed
    if (button.hasAttribute("data-hand-over-close")) menu.querySelector("summary").focus();
  });
  document.addEventListener("toggle", function (event) {
    var menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains("hand-over")) return;
    if (menu.open) {
      var panel = menu.querySelector(".hand-over-panel");
      if (panel) reveal(menu, panel);
      return;
    }
    var created = menu.querySelector(".hand-over-created");
    var form = menu.querySelector("[data-hand-over-form]");
    if (created && form) {
      created.remove();
      form.hidden = false;
    }
  }, true);

  // "Confier" of a card (form[data-hand-over-form]): the link is created without leaving the
  // card. The answer holds the delegation of every card ([data-hand-over], the one sent from
  // open on the new link) and the Settings card (#delegates): each replaces its counterpart,
  // then the focus goes to the new link. An error shows in the form. Without script, the form
  // posts and the page comes back to the card.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute("data-hand-over-form") || event.defaultPrevented) return;
    event.preventDefault();
    var slot = form.closest("[data-hand-over]");
    var card = slot ? slot.getAttribute("data-hand-over") : "";
    var error = form.querySelector("[data-hand-over-error]");
    var token = form.querySelector("input[name=csrf_token]");
    fetch(form.getAttribute("action").split("#")[0], {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      credentials: "same-origin",
      headers: { "X-CSRF-Token": token ? token.value : "", "X-Requested-With": "fetch" }
    })
      .then(function (response) {
        if (response.redirected || response.status === 401 || response.status === 403 || response.status === 404) {
          window.location.reload();
          return null;
        }
        return response.text().then(function (body) { return { ok: response.ok, body: body }; });
      })
      .then(function (result) {
        if (!result) return;
        if (!result.ok) {
          if (error) { error.textContent = result.body; error.hidden = false; }
          return;
        }
        var parsed = document.createElement("template");
        parsed.innerHTML = result.body;
        Array.prototype.forEach.call(parsed.content.querySelectorAll("[data-hand-over]"), function (fresh) {
          var key = fresh.getAttribute("data-hand-over");
          var old = document.querySelector('[data-hand-over="' + key + '"]');
          if (old) old.replaceWith(fresh.cloneNode(true));
        });
        var settings = parsed.content.querySelector("#delegates");
        var current = document.getElementById("delegates");
        if (settings && current) current.replaceWith(settings.cloneNode(true));
        armCopy(document);
        armClose(document);
        var link = document.getElementById(card + "-hand-url");
        if (link) link.focus();
      })
      .catch(function () { /* network hiccup: the form stays, the user can send again */ });
  });

  // --- Rubrics: the address names a rubric (#tab-…) or anything inside one (#notes, #draft-3);
  // that rubric is shown, the others hidden. Without script they all stay stacked.
  if (consoleRoot) {
    var rubrics = Array.prototype.slice.call(document.querySelectorAll("[data-rubric]"));
    var links = Array.prototype.slice.call(document.querySelectorAll("[data-rubric-link]"));
    var more = document.querySelector("[data-tab-more]");
    var fallback = document.querySelector("[data-rubric][data-default]") || rubrics[0];

    var show = function (rubric) {
      currentRubric = rubric.id;
      rubrics.forEach(function (each) { each.hidden = each !== rubric; });
      links.forEach(function (link) {
        if (link.getAttribute("href") === "#" + rubric.id) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      });
      if (more) {
        more.classList.toggle("is-current", Boolean(more.querySelector("[aria-current]")));
        more.open = false;
      }
      document.documentElement.classList.add("console-ready");
    };

    var route = function () {
      var id = "";
      try { id = decodeURIComponent(window.location.hash.slice(1)); } catch (error) { id = ""; }
      var target = id ? document.getElementById(id) : null;
      var rubric = target ? target.closest("[data-rubric]") : null;
      show(rubric || fallback);
      if (target && rubric && target !== rubric) {
        for (var node = target; node && node !== rubric; node = node.parentElement) {
          if (node.tagName === "DETAILS") node.open = true;
        }
        target.scrollIntoView({ block: "start" });
        // A form shown by its link ("+" of the delegate links): its first field takes the focus.
        if (target.hasAttribute("data-focus-first")) {
          var first = target.querySelector("input:not([type=hidden]), textarea, select");
          if (first) first.focus({ preventScroll: true });
        }
      } else {
        window.scrollTo(0, 0);
      }
    };

    window.addEventListener("hashchange", route);
    links.forEach(function (link) {
      link.addEventListener("click", function () {
        // The rubric already in the address: no hashchange, so go back to its top here.
        if (link.getAttribute("href") === window.location.hash) route();
      });
    });
    if (more) {
      document.addEventListener("click", function (event) {
        if (more.open && !more.contains(event.target)) more.open = false;
      });
    }
    route();
  }

  // --- Live parts ([data-live-part], from one answer of the panel): the status bar, the polls
  // open now, and during the session the item under discussion and the next poll of Steering,
  // with the count of drafts in its head. A part may show in several places (the polls open
  // now: in Steering and at the top of Polls). Each copy is replaced only when its HTML
  // changed; the control that had the focus inside it gets it back.
  var bar = document.getElementById("status-bar");
  if (!bar) return;
  var panelUrl = bar.getAttribute("data-panel-url");
  var eventsUrl = bar.getAttribute("data-events-url");
  var pageStatus = consoleRoot ? consoleRoot.getAttribute("data-status") : null;
  var pageAccess = consoleRoot ? consoleRoot.getAttribute("data-access") : null;
  var hints = document.querySelectorAll("[data-refresh-hint]");
  var pollTimer = null;
  var pending = false;

  // How to find the focused control again in the new copy of its part: its id; else a button
  // by its name and value in a form with the same address ("Suivant" has no id, "Clore le vote"
  // names its poll in the address), or a link by its address.
  function address(form) {
    return form ? (form.getAttribute("action") || "").split("#")[0] : null;
  }

  function focusKey(part) {
    var active = document.activeElement;
    if (!active || active === part || !part.contains(active)) return null;
    return {
      id: active.id,
      tag: active.tagName,
      name: active.getAttribute("name"),
      value: active.getAttribute("value"),
      form: address(active.form),
      href: active.getAttribute("href")
    };
  }

  function refocus(part, key) {
    var match = key.id ? document.getElementById(key.id) : null;
    if (match && !part.contains(match)) match = null;
    var buttons = Array.prototype.filter.call(part.querySelectorAll("button"), function (button) {
      return address(button.form) === key.form;
    });
    if (!match && key.tag === "BUTTON") {
      match = buttons.filter(function (button) {
        return button.getAttribute("name") === key.name && button.getAttribute("value") === key.value;
      })[0] || null;
      // Disabled now ("Suivant" at the last item): another button of the same form keeps the
      // focus in the card ("Précédent"), never a button of another form.
      if (match && match.disabled) {
        match = buttons.filter(function (button) { return !button.disabled; })[0] || null;
      }
    } else if (!match && key.tag === "A") {
      match = Array.prototype.filter.call(part.querySelectorAll("a[href]"), function (link) {
        return link.getAttribute("href") === key.href;
      })[0] || null;
    }
    if (match) match.focus({ preventScroll: true });
  }

  function apply(html) {
    var parsed = document.createElement("template");
    parsed.innerHTML = html;
    var fresh = parsed.content.querySelector("[data-status]");
    if (fresh && ((pageStatus && fresh.getAttribute("data-status") !== pageStatus) ||
        (pageAccess && fresh.getAttribute("data-access") !== pageAccess))) {
      // Opened or closed from another console, or the rights of a delegate link changed: the
      // rubrics change, so draw the page again.
      window.location.reload();
      return;
    }
    Array.prototype.forEach.call(parsed.content.querySelectorAll("[data-live-part]"), function (part) {
      var name = part.getAttribute("data-live-part");
      Array.prototype.forEach.call(document.querySelectorAll('[data-live-part="' + name + '"]'), function (target) {
        // The tiles unfolded here stay unfolded in the new copy (a poll's tile in Polls).
        var fresh = part.cloneNode(true);
        Array.prototype.forEach.call(target.querySelectorAll("details[open][id]"), function (open) {
          var same = fresh.querySelector('details[id="' + open.id + '"]');
          if (same) same.setAttribute("open", "");
        });
        // Unchanged (the page indents its copies): nothing moves.
        if (target.innerHTML.trim() === fresh.innerHTML.trim()) return;
        var key = focusKey(target);
        target.innerHTML = fresh.innerHTML;
        if (key) refocus(target, key);
      });
    });
    // A card shown only while its live part lists something: data-shown-with is the selector.
    Array.prototype.forEach.call(document.querySelectorAll("[data-shown-with]"), function (card) {
      card.hidden = !card.querySelector(card.getAttribute("data-shown-with"));
    });
    // timer.js and notes.js refetch their card when another console may have changed it.
    document.dispatchEvent(new CustomEvent("agaura:console-refreshed"));
  }

  function refresh() {
    return fetch(panelUrl, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (response) {
        if (response.redirected || response.status === 401 || response.status === 403 || response.status === 404) {
          window.location.reload();
          return null;
        }
        return response.ok ? response.text() : null;
      })
      .then(function (html) { if (html) apply(html); })
      .catch(function () { /* network hiccup: the next event or poll retries */ });
  }

  // Forms of a live part (form[data-live-form]: "Précédent" and "Suivant" of the item under
  // discussion): sent with fetch, then the live parts are fetched at once, so the page neither
  // reloads nor scrolls and the button pressed keeps the focus. The server answers 204; a
  // refusal keeps its message for the page, drawn again to show it. A tap while the previous
  // one is on its way does nothing (the server also ignores a move from an item already left).
  // Without script, the form posts and the page comes back as before.
  var sending = false;
  document.addEventListener("submit", function (event) {
    var form = event.target;
    // Without the button pressed (an old browser), the form posts as without script.
    if (!form.hasAttribute("data-live-form") || event.defaultPrevented || !event.submitter) return;
    event.preventDefault();
    if (sending) return;
    sending = true;
    var body = new URLSearchParams(new FormData(form));
    if (event.submitter.name) body.append(event.submitter.name, event.submitter.value);
    var token = form.querySelector("input[name=csrf_token]");
    fetch(form.getAttribute("action").split("#")[0], {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-CSRF-Token": token ? token.value : "", "X-Requested-With": "fetch" }
    })
      .then(function (response) {
        if (response.redirected || !response.ok) {
          window.location.reload(); // refused, logged out, or the right taken away
          return null;
        }
        return refresh();
      })
      .catch(function () { /* network hiccup: the form stays, the user can send again */ })
      .then(function () { sending = false; });
  });

  // Bursts of ballots produce bursts of events: refresh at most every 500 ms.
  function schedule() {
    if (pending) return;
    pending = true;
    window.setTimeout(function () { pending = false; refresh(); }, 500);
  }

  function startPolling() {
    if (!pollTimer) pollTimer = window.setInterval(refresh, 5000);
  }

  if (!("EventSource" in window)) { startPolling(); return; }
  var source = new EventSource(eventsUrl);
  // The server sends each event's data as JSON (FastAPI's ServerSentEvent): "polls" arrives
  // quoted. A poll closed (by a console or by its timer) shows the hints to reload the page.
  function payload(event) {
    try { return JSON.parse(event.data); } catch (error) { return event.data; }
  }
  source.addEventListener("turnout", function (event) {
    if (payload(event) === "polls") Array.prototype.forEach.call(hints, function (hint) { hint.hidden = false; });
    schedule();
  });
  source.onopen = function () {
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
  };
  source.onerror = startPolling;
})();
