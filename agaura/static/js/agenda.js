// Session console, agenda card (manage/_agenda.html): one text area, one item per line, edited in
// place, then the choice of the item under discussion (a live form, which manage.js sends).
// - Sends the text with fetch: the server answers with the card alone, the agenda as saved
//   (numbering and blank lines removed: the text area takes it, unless typed in meanwhile) or
//   the text refused with its messages on the field (status 400), so the page never reloads.
// - Marks unsaved changes, with a button that discards them, and keeps them on the device
//   (sessionStorage, data-agenda-draft): a reload of the console never loses them.
// - Follows the other consoles: on "agaura:console-refreshed" (manage.js, after each console
//   event) refetches the card. The text area takes the new agenda unless it holds changes or the
//   focus: changes are kept, with a notice that the agenda changed meanwhile, and a text area
//   left untouched takes the new agenda once the focus leaves it. The list of items takes the
//   new agenda and item, unless it holds a choice not sent yet; it is updated in place, so the
//   focus stays.
// No visible text here: every string comes from the template or from the server.
(function () {
  "use strict";

  var MIN_INTERVAL = 1000;
  var MIN_ROWS = 3;
  var MAX_ROWS = 12;

  var card = document.querySelector("[data-agenda-url]");
  var form = card && card.querySelector("[data-agenda-form]");
  var text = form && form.querySelector("textarea[name=items]");
  if (!text) return;
  var url = card.getAttribute("data-agenda-url");
  var draftKey = card.getAttribute("data-agenda-draft");
  var unsaved = card.querySelector("[data-agenda-unsaved]");
  var discard = card.querySelector("[data-agenda-discard]");
  var elsewhere = card.querySelector("[data-agenda-elsewhere]");
  var savedNotice = card.querySelector("[data-agenda-saved]");
  var errors = card.querySelector("[data-agenda-errors]");
  // base: the stored agenda the text in the text area comes from; latest: the stored agenda now.
  var base = text.getAttribute("data-saved") || "";
  var latest = base;
  var busy = false;

  function storage(use) {
    try { return use(window.sessionStorage); } catch (e) { return null; } // private mode, quota
  }

  // The items as the server keeps them, near enough to tell a change: lines trimmed, blank
  // lines dropped (the numbering typed before an item still counts as a change).
  function items(value) {
    return value.split(/\r\n?|\n/).map(function (line) { return line.trim(); })
      .filter(Boolean).join("\n");
  }

  function edited() {
    return items(text.value) !== items(base);
  }

  function fitRows() {
    var rows = Math.min(MAX_ROWS, Math.max(MIN_ROWS, text.value.split("\n").length));
    if (text.rows !== rows) text.rows = rows;
  }

  // The marks of unsaved changes, and the notice of a change made elsewhere meanwhile.
  function show() {
    var changed = edited();
    unsaved.hidden = !changed;
    discard.hidden = !changed;
    var message = changed && items(base) !== items(latest) ? elsewhere.getAttribute("data-message") : "";
    if (elsewhere.textContent !== message) elsewhere.textContent = message;
    fitRows();
  }

  function keepDraft() {
    storage(function (store) {
      if (edited()) store.setItem(draftKey, JSON.stringify({ text: text.value, base: base }));
      else store.removeItem(draftKey);
    });
  }

  function restoreDraft() {
    if (text.value !== base) return; // the page came back with a refused text: it is the draft
    var raw = storage(function (store) { return store.getItem(draftKey); });
    if (!raw) return;
    try {
      var draft = JSON.parse(raw);
      if (typeof draft.text !== "string" || items(draft.text) === items(latest)) return;
      text.value = draft.text;
      base = typeof draft.base === "string" ? draft.base : latest;
    } catch (e) { /* unreadable draft: ignore it */ }
  }

  // The text area takes the stored agenda, when it holds no change of its own.
  function follow() {
    if (edited() || items(latest) === items(base)) return;
    text.value = latest;
    base = latest;
  }

  function clearErrors() {
    var shown = errors.querySelector("[id]");
    if (shown) {
      var ids = (text.getAttribute("aria-describedby") || "").split(" ").filter(function (id) {
        return id && id !== shown.id;
      });
      text.setAttribute("aria-describedby", ids.join(" "));
    }
    errors.innerHTML = "";
    text.removeAttribute("aria-invalid");
  }

  // The option the list was drawn with: what the server holds as the item under discussion.
  function drawn(select) {
    var option = select.querySelector("option[selected]") || select.querySelector("option");
    return option ? option.value : "";
  }

  function followCurrent(fresh) {
    var old = card.querySelector("[data-agenda-current]");
    var part = fresh.querySelector("[data-agenda-current]");
    if (!old || !part) return;
    var select = old.querySelector("select");
    var next = part.querySelector("select");
    if (!select || !next) {
      if (old.innerHTML.trim() !== part.innerHTML.trim()) old.innerHTML = part.innerHTML;
      return;
    }
    if (select.value !== drawn(select) && select.value !== drawn(next)) return; // a choice not sent
    if (select.innerHTML.trim() !== next.innerHTML.trim()) select.innerHTML = next.innerHTML;
  }

  // A choice sent (manage.js sends the live form after this) is no longer a choice to keep: it
  // becomes the option the list holds, until the card is fetched again.
  card.addEventListener("submit", function (event) {
    var select = event.target.closest("[data-agenda-current]") && event.target.querySelector("select");
    if (!select) return;
    Array.prototype.forEach.call(select.options, function (option) {
      option.defaultSelected = option.selected;
    });
  });

  // ``sent``: the text this card sent, when ``html`` answers its own save.
  function apply(html, sent) {
    var parsed = document.createElement("template");
    parsed.innerHTML = html;
    var fresh = parsed.content.querySelector('[id="' + card.id + '"]');
    var freshText = fresh && fresh.querySelector("[data-agenda-form] textarea[name=items]");
    if (!freshText) {
      window.location.reload(); // the session closed meanwhile: the card is read only now
      return;
    }
    latest = freshText.getAttribute("data-saved") || "";
    if (typeof sent === "string") {
      var refused = freshText.getAttribute("aria-invalid") === "true";
      clearErrors();
      errors.innerHTML = fresh.querySelector("[data-agenda-errors]").innerHTML;
      text.setAttribute("aria-describedby", freshText.getAttribute("aria-describedby") || "");
      if (refused) text.setAttribute("aria-invalid", "true");
      savedNotice.textContent = fresh.querySelector("[data-agenda-saved]").textContent;
      var button = form.querySelector("button[type=submit]");
      var freshButton = fresh.querySelector("[data-agenda-form] button[type=submit]");
      if (button && freshButton) button.className = freshButton.className; // primary while empty
      if (refused) {
        text.focus();
      } else {
        base = latest;
        if (text.value === sent) text.value = latest; // as saved: numbering, blank lines gone
      }
    } else if (document.activeElement !== text) {
      follow();
    }
    followCurrent(fresh);
    show();
    keepDraft();
  }

  form.addEventListener("submit", function (event) {
    if (event.defaultPrevented) return;
    event.preventDefault();
    if (busy) return;
    busy = true;
    var sent = text.value;
    var token = form.querySelector("input[name=csrf_token]");
    fetch(form.getAttribute("action").split("#")[0], {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      credentials: "same-origin",
      headers: { "X-CSRF-Token": token ? token.value : "", "X-Requested-With": "fetch" }
    })
      .then(function (response) {
        if (response.redirected || response.status === 401 || response.status === 403 || response.status === 404) {
          window.location.reload(); // logged out, or the right or the session is gone
          return null;
        }
        if (!response.ok && response.status !== 400) return null; // busy server: send again
        return response.text();
      })
      .then(function (html) {
        busy = false;
        if (html) apply(html, sent);
      })
      .catch(function () { busy = false; /* network hiccup: the draft is kept, send again */ });
  });

  text.addEventListener("input", function () {
    if (savedNotice.textContent) savedNotice.textContent = "";
    show();
    keepDraft();
  });

  // A text area left untouched while the agenda changed elsewhere takes it once left.
  text.addEventListener("blur", function () {
    follow();
    show();
  });

  discard.addEventListener("click", function () {
    text.value = latest;
    base = latest;
    clearErrors();
    savedNotice.textContent = "";
    show();
    keepDraft();
    text.focus();
  });

  restoreDraft();
  show();
  keepDraft();

  // --- Follow the other consoles ------------------------------------------------------------

  var lastFetch = 0;
  var pending = null;

  function refresh() {
    lastFetch = Date.now();
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "fetch" } })
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) { if (html && !busy) apply(html); })
      .catch(function () { /* the next event retries */ });
  }

  function schedule() {
    if (pending) return;
    var wait = Math.max(0, lastFetch + MIN_INTERVAL - Date.now());
    pending = window.setTimeout(function () { pending = null; refresh(); }, wait);
  }

  // manage.js fires this after each console event (another console may have changed the agenda).
  document.addEventListener("agaura:console-refreshed", schedule);
})();
