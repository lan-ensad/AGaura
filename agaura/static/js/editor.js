// Poll editor, step 2: the rule sentence rewritten as the settings change, option rows added
// and removed in place, the preview of a resolution's labels kept in step, and the forms of a
// card ("Résolution", "Options libres") switched in place: the button posts the form as it
// would without script (switch=<kind>), and the step 2 the server answers replaces this one,
// so what is kept is decided in one place (editor.switch_kind). Every visible text comes from
// the page: the sentences from data-rule-texts (their {option}, {base}, {share} and {n} filled
// in here, as editor.rule_sentence does on the server), the row labels from data- attributes
// and the row template.
(function () {
  "use strict";

  var first = document.getElementById("poll-editor");
  if (first) setUp(first, true);

  function setUp(form, loaded) {
    var kind = form.getAttribute("data-kind");

    // --- Fields ---------------------------------------------------------------------------

    function checked(name, fallback) {
      var radio = form.querySelector('input[name="' + name + '"]:checked');
      return radio ? radio.value : fallback;
    }

    function text(name) {
      var field = form.querySelector('[name="' + name + '"]:not([type=radio])');
      return field ? field.value : "";
    }

    function labels() {
      var inputs = form.querySelectorAll("input[name=option_label]");
      var all = [];
      for (var i = 0; i < inputs.length; i++) {
        var value = inputs[i].value.trim();
        if (value) all.push(value);
      }
      return all;
    }

    function whole(raw) {
      var value = raw.trim();
      return /^[0-9]{1,6}$/.test(value) ? parseInt(value, 10) : null;
    }

    // --- The rule in one sentence ------------------------------------------------------------

    var sentence = form.querySelector("[data-rule-sentence]");
    var texts = {};
    try { texts = JSON.parse(sentence.getAttribute("data-rule-texts")); } catch (error) { sentence = null; }
    var missing = "…";

    function fill(template, values) {
      return (template || "").replace(/\{(option|base|share|n)\}/g, function (match, key) {
        return values[key] !== undefined && values[key] !== null && values[key] !== "" ? values[key] : missing;
      });
    }

    // "66.5" → "66,5 %", as the server shows it; anything else is not a percentage yet.
    function share(raw) {
      var value = raw.trim().replace(/\s*%$/, "");
      if (!/^[0-9]{1,3}([.,][0-9]{1,2})?$/.test(value)) return missing;
      var number = parseFloat(value.replace(",", "."));
      if (!(number > 0 && number <= 100)) return missing;
      if (/[.,]/.test(value)) value = value.replace(/0+$/, "").replace(/[.,]$/, "");
      return value.replace(".", ",") + "\u00a0%";
    }

    function rule() {
      if (kind === "majority_judgment") return texts.judgment;
      if (kind === "condorcet") return texts.condorcet;
      if (kind === "approval") {
        var seats = whole(text("seats"));
        var parts = [fill(texts[seats > 1 ? "seats-many" : "seats-one"], { n: seats ? String(seats) : "" })];
        var mode = checked("max_choices", "seats");
        var limit = mode === "seats" ? seats : mode === "number" ? whole(text("max_choices_number")) : null;
        if (mode === "none") parts.push(texts["ticks-free"]);
        else if (limit) parts.push(fill(texts[limit > 1 ? "ticks-many" : "ticks-one"], { n: String(limit) }));
        if (text("min_share_percent").trim()) {
          var key = text("min_share_strict") === "1" ? "minimum-more" : "minimum-least";
          parts.push(fill(texts[key], { share: share(text("min_share_percent")) }));
        }
        return parts.join(" ");
      }
      var options = labels();
      var threshold = checked("threshold", kind === "resolution" ? "majority" : "auto");
      if (threshold === "auto") threshold = options.length === 2 ? "majority" : "plurality";
      else if (threshold === "custom") threshold = text("threshold_strict") === "1" ? "more" : "least";
      var prefix = kind === "resolution" ? "resolution" : "single";
      var template = texts[prefix + "-" + threshold] || texts[prefix + "-plurality"];
      // The option to adopt by its row, as typed: an emptied "Pour" reads "…", not "Contre".
      var position = whole(text("adopt_option") || "1") || 0;
      var rows = form.querySelectorAll("input[name=option_label]");
      return fill(template, {
        option: position > 0 && position <= rows.length ? rows[position - 1].value.trim() : "",
        base: texts["base-" + checked("base", "valid")] || texts["base-valid"],
        share: share(text("threshold_percent"))
      });
    }

    // The labels of a resolution, as voters will see them.
    function preview() {
      var inputs = form.querySelectorAll("input[name=option_label]");
      for (var i = 0; i < inputs.length; i++) {
        var item = form.querySelector('[data-preview-option="' + (i + 1) + '"]');
        if (item) item.textContent = inputs[i].value.trim() || missing;
      }
    }

    // The hint of a resolution's base: the one of the radio button checked.
    function baseHint() {
      var base = checked("base", "valid");
      Array.prototype.forEach.call(form.querySelectorAll("[data-base-hint]"), function (hint) {
        hint.hidden = hint.getAttribute("data-base-hint") !== base;
      });
    }

    var shown = sentence ? sentence.textContent : "";
    function refresh() {
      preview();
      baseHint();
      if (!sentence) return;
      var next = rule();
      // Only a real change is announced (aria-live): typing a title says nothing.
      if (next && next !== shown) sentence.textContent = shown = next;
    }

    // Typing a custom threshold or limit selects the radio button it belongs to, before the
    // sentence is written again.
    form.addEventListener("input", function (event) {
      var target = event.target.getAttribute("data-selects");
      var radio = target && document.getElementById(target);
      if (radio) radio.checked = true;
    });
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);

    // Messages describe the refused submission: a field being corrected loses its own message, and
    // typing an option hides the message about the number of options.
    form.addEventListener("input", function (event) {
      var field = event.target;
      if (field.getAttribute("aria-invalid") === "true") {
        field.removeAttribute("aria-invalid");
        var ids = (field.getAttribute("aria-describedby") || "").split(" ");
        for (var i = 0; i < ids.length; i++) {
          var message = ids[i] && document.getElementById(ids[i]);
          if (message && message.classList.contains("field-error")) message.hidden = true;
        }
      }
      if (field.name === "option_label") {
        var general = document.getElementById("error-options");
        if (general) general.hidden = true;
      }
    });

    // After a refused save, bring the first faulty field into view (long form on a phone); the
    // server opened the fold that holds it.
    var invalid = loaded && document.getElementById("editor-errors") && form.querySelector("[aria-invalid=true]");
    if (invalid && !invalid.closest("[hidden]")) invalid.focus();

    // --- The forms of a card -----------------------------------------------------------------

    var busy = false;
    form.addEventListener("click", function (event) {
      var button = event.target.closest("[data-switch-form]");
      if (!button) return;
      event.preventDefault();
      if (busy || button.getAttribute("aria-pressed") === "true") return;
      busy = true;
      form.setAttribute("aria-busy", "true");
      var body = new URLSearchParams(new FormData(form));
      body.append(button.name, button.value);
      var token = form.querySelector("input[name=csrf_token]");
      fetch(form.getAttribute("action"), {
        method: "POST",
        body: body,
        credentials: "same-origin",
        redirect: "manual", // a draft pushed meanwhile: the page says so, as without script
        headers: { "X-CSRF-Token": token ? token.value : "", "X-Requested-With": "fetch" }
      })
        .then(function (response) {
          if (!response.ok) throw new Error("refused");
          return response.text();
        })
        .then(function (html) {
          var next = new DOMParser().parseFromString(html, "text/html").getElementById("poll-editor");
          if (!next) throw new Error("no form");
          form.replaceWith(next);
          setUp(next, false);
          var pressed = next.querySelector('[data-switch-form][value="' + button.value + '"]');
          if (pressed) pressed.focus();
        })
        .catch(function () {
          // Sent as without script: the server answers with the page, or the console and its message.
          busy = false;
          form.removeAttribute("aria-busy");
          if (form.requestSubmit) {
            form.requestSubmit(button);
            return;
          }
          var field = document.createElement("input");
          field.type = "hidden";
          field.name = button.name;
          field.value = button.value;
          form.appendChild(field);
          form.submit();
        });
    });

    // --- Option rows -------------------------------------------------------------------------

    var list = form.querySelector("[data-option-rows]");
    if (!list) return;
    var template = document.getElementById("option-row-template");
    var addButton = form.querySelector("[data-add-option]");

    var max = parseInt(list.getAttribute("data-max"), 10) || 30;
    var min = 2;
    var patterns = {
      label: list.getAttribute("data-label") || "{n}",
      description: list.getAttribute("data-description-label") || "{n}",
      remove: list.getAttribute("data-remove-label") || "{n}"
    };
    var nextId = 1000; // ids of added rows never clash with server-rendered ones

    function rows() {
      return list.querySelectorAll("[data-option-row]");
    }

    function fillNumber(pattern, number) {
      return pattern.split("{n}").join(String(number));
    }

    function renumber() {
      var all = rows();
      for (var i = 0; i < all.length; i++) {
        var row = all[i];
        var number = i + 1;
        row.querySelector("[data-row-label]").textContent = fillNumber(patterns.label, number);
        row.querySelector("[data-row-description]").textContent = fillNumber(patterns.description, number);
        row.querySelector("input[name=option_label]").setAttribute("placeholder", fillNumber(patterns.label, number));
        var remove = row.querySelector("[data-remove-option]");
        // A resolution keeps its rows (no template to add one back): no remove button there.
        if (!template) continue;
        remove.hidden = false;
        remove.disabled = all.length <= min;
        remove.setAttribute("aria-label", fillNumber(patterns.remove, number));
      }
      if (addButton) addButton.disabled = all.length >= max;
    }

    function addRow(after) {
      if (!template || rows().length >= max) return null;
      var holder = document.createElement("ol");
      holder.innerHTML = template.innerHTML.split("{n}").join(String(nextId++));
      var row = holder.querySelector("[data-option-row]");
      if (after && after.nextSibling) list.insertBefore(row, after.nextSibling);
      else list.appendChild(row);
      renumber();
      return row;
    }

    function labelInput(row) {
      return row.querySelector("input[name=option_label]");
    }

    function isEmpty(row) {
      var inputs = row.querySelectorAll("input");
      for (var i = 0; i < inputs.length; i++) if (inputs[i].value.trim()) return false;
      return true;
    }

    if (addButton) {
      addButton.addEventListener("click", function (event) {
        if (!template) return; // no row template: the button submits, the server adds rows
        event.preventDefault();
        var row = addRow(null);
        if (row) labelInput(row).focus();
      });
    }

    list.addEventListener("click", function (event) {
      var button = event.target.closest("[data-remove-option]");
      if (!button || button.disabled) return;
      var row = button.closest("[data-option-row]");
      var neighbour = row.nextElementSibling || row.previousElementSibling;
      row.parentNode.removeChild(row);
      renumber();
      refresh();
      if (neighbour) labelInput(neighbour).focus();
    });

    // Enter in an option row moves to the next row, adding one at the end of the list, rather
    // than saving a half-typed list (the phone keyboard shows "next").
    list.addEventListener("keydown", function (event) {
      var name = event.target.name;
      if (event.key !== "Enter" || event.isComposing) return;
      if (name !== "option_label" && name !== "option_description") return;
      event.preventDefault();
      var row = event.target.closest("[data-option-row]");
      var next = row.nextElementSibling || addRow(row);
      if (next) labelInput(next).focus();
    });

    // Pasting several lines into an option label fills one row per line.
    list.addEventListener("paste", function (event) {
      if (event.target.name !== "option_label" || !event.clipboardData || !template) return;
      var lines = event.clipboardData.getData("text").split(/\r?\n/).map(function (line) {
        return line.trim();
      }).filter(Boolean);
      if (lines.length < 2) return;
      event.preventDefault();
      var row = event.target.closest("[data-option-row]");
      labelInput(row).value = lines[0];
      for (var i = 1; i < lines.length; i++) {
        var next = row.nextElementSibling;
        row = next && isEmpty(next) ? next : addRow(row);
        if (!row) break;
        labelInput(row).value = lines[i];
      }
      if (row) labelInput(row).focus();
      refresh();
    });

    renumber();
  }
})();
