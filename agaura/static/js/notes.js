// Session console, session notes, loaded by manage/_notes.html.
//
// The page may hold two copies: the card "Notes de séance" of its own rubric (#notes) and,
// during the session, the command "Note rapide" of Steering (#cmd-notes). Each copy is a
// [data-notes-url] element with its own add form, handled by itself, never by its id.
// - Sends the forms of a copy (add, and in the card edit and delete) with fetch: the server
//   answers with both copies, whose server parts replace those of the page (the current item,
//   the list of the card, the last note of the command), so the page never reloads and the
//   note being typed keeps the focus. Ctrl+Enter (or Cmd+Enter) sends the form of the text area
//   in use.
// - Keeps the unsent note of each add form in sessionStorage (data-notes-draft, one key per
//   form): a reload of the console (other actions use POST/redirect/GET) never loses typed text.
// - Follows the other consoles: when manage.js reports a console event
//   ("agaura:console-refreshed"), refetches both copies and replaces what changed, but not the
//   list of the card while a note is being edited there.
// No visible text here: messages come from the server or from the template.
(function () {
  "use strict";

  var MIN_INTERVAL = 1000;
  var PARTS = ["[data-notes-current]", "[data-notes-list]", "[data-notes-last]"];

  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-notes-url]"));
  if (!cards.length) return;
  var url = cards[0].getAttribute("data-notes-url");
  var busy = false;

  function storage(use) {
    try { return use(window.sessionStorage); } catch (e) { return null; } // private mode, quota
  }

  function editing(card) {
    var list = card.querySelector("[data-notes-list]");
    return Boolean(list && (list.querySelector("details[open]") || list.contains(document.activeElement)));
  }

  // Replace the parts of each copy that come from the server, never a note being typed.
  function apply(html, force) {
    var parsed = document.createElement("template");
    parsed.innerHTML = html;
    cards.forEach(function (card) {
      var fresh = parsed.content.querySelector('[id="' + card.id + '"]');
      if (!fresh) return;
      PARTS.forEach(function (selector) {
        var part = fresh.querySelector(selector);
        var old = card.querySelector(selector);
        if (!part || !old || part.innerHTML === old.innerHTML) return;
        if (selector === "[data-notes-list]" && editing(card) && !force) return;
        old.innerHTML = part.innerHTML;
      });
    });
  }

  function csrfToken(form) {
    var field = form.querySelector("input[name=csrf_token]");
    return field ? field.value : "";
  }

  // --- One copy: its add form, its draft, its error line ---------------------------------

  function setUp(card) {
    var draftKey = card.getAttribute("data-notes-draft");
    var addForm = card.querySelector("[data-notes-add]");
    if (!addForm) return;
    var text = addForm.querySelector("textarea[name=text]");
    var internal = addForm.querySelector("input[name=internal]");
    var error = card.querySelector("[data-notes-error]");

    function saveDraft() {
      storage(function (store) {
        if (text.value || internal.checked) {
          store.setItem(draftKey, JSON.stringify({ text: text.value, internal: internal.checked }));
        } else {
          store.removeItem(draftKey);
        }
      });
    }

    function restoreDraft() {
      var raw = storage(function (store) { return store.getItem(draftKey); });
      if (!raw || text.value) return;
      try {
        var draft = JSON.parse(raw);
        text.value = draft.text || "";
        internal.checked = Boolean(draft.internal);
      } catch (e) { /* unreadable draft: ignore it */ }
    }

    function showError(message) {
      if (!error) return;
      error.textContent = message || "";
      error.hidden = !message;
    }

    function send(form) {
      if (busy) return;
      busy = true;
      var adding = form === addForm;
      fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken(form), "X-Requested-With": "fetch" }
      })
        .then(function (response) {
          if (response.redirected || response.status === 401 || response.status === 403 || response.status === 404) {
            window.location.reload(); // logged out, or the note or the session is gone
            return null;
          }
          return response.text().then(function (body) { return { ok: response.ok, body: body }; });
        })
        .then(function (result) {
          if (!result) return;
          if (!result.ok) { showError(result.body); return; }
          showError("");
          apply(result.body, true);
          if (adding) {
            text.value = "";
            internal.checked = false;
            storage(function (store) { store.removeItem(draftKey); });
            text.focus();
          }
        })
        .catch(function () { /* network hiccup: the draft is kept, the user can send again */ })
        .then(function () { busy = false; });
    }

    card.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.hasAttribute("data-notes-add") && !form.hasAttribute("data-notes-action")) return;
      event.preventDefault();
      event.stopPropagation(); // manage.js would ask for the confirmation a second time
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) return;
      send(form);
    });

    card.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
      var area = event.target;
      if (!area || area.tagName !== "TEXTAREA" || !area.form) return;
      event.preventDefault();
      if (area.form.requestSubmit) area.form.requestSubmit();
      else if (area.form.reportValidity()) send(area.form);
    });

    text.addEventListener("input", saveDraft);
    internal.addEventListener("change", saveDraft);

    var shortcut = card.querySelector("[data-notes-shortcut]");
    if (shortcut && window.matchMedia && window.matchMedia("(pointer: fine) and (hover: hover)").matches) {
      shortcut.hidden = false;
    }
    restoreDraft();
  }

  cards.forEach(setUp);

  // --- Follow the other consoles ------------------------------------------------------------

  var lastFetch = 0;
  var pending = null;

  function refresh() {
    lastFetch = Date.now();
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "fetch" } })
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) { if (html) apply(html, false); })
      .catch(function () { /* the next event retries */ });
  }

  function schedule() {
    if (pending) return;
    var wait = Math.max(0, lastFetch + MIN_INTERVAL - Date.now());
    pending = window.setTimeout(function () { pending = null; refresh(); }, wait);
  }

  // manage.js fires this after each console event (another console may have added a note).
  if (url) document.addEventListener("agaura:console-refreshed", schedule);
})();
