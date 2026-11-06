// Session console, tribune card: the "+" button adds a row in place and focuses its name field.
// Without this script the button sends the form back with one more row (nothing saved).
// No visible text here: the new row comes from the <template> rendered by the server.
(function () {
  "use strict";

  var rows = document.querySelector("[data-tribune-rows]");
  var template = document.getElementById("tribune-row-template");
  var button = document.querySelector("[data-add-row]");
  if (!rows || !template || !button) return;
  var max = parseInt(rows.getAttribute("data-max"), 10) || 20;

  function count() {
    return rows.querySelectorAll(".tribune-row").length;
  }

  button.addEventListener("click", function (event) {
    event.preventDefault();
    if (count() >= max) return;
    var number = String(count() + 1);
    var holder = document.createElement("div");
    holder.innerHTML = template.innerHTML.split("__N__").join(number);
    var row = holder.firstElementChild;
    rows.appendChild(row);
    var name = row.querySelector("input[name=name]");
    if (name) name.focus();
    if (count() >= max) button.hidden = true;
  });
})();
