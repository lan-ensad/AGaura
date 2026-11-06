// Page behind a delegate link (manage/delegate_join.html): the key travels in the fragment
// (#k=...), which browsers never send to the server. Copy it into the form, remove it from the
// address bar, send the form. No visible text here.
(function () {
  "use strict";

  var match = /^#k=(.+)$/.exec(window.location.hash);
  var form = document.querySelector("form[data-delegate-join]");
  if (!match || !form) return;
  history.replaceState(null, "", window.location.pathname + window.location.search);
  var key = "";
  try { key = decodeURIComponent(match[1]); } catch (error) { key = ""; }
  if (!key) return;
  form.elements.key.value = key;
  var note = document.querySelector("[data-delegate-joining]");
  if (note) note.hidden = false;
  form.submit();
})();
