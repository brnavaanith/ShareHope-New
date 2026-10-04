// Dashboards: client-side filters, mock status transitions, notices, toasts.
(function () {
  function toast(msg) {
    var t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    window.clearTimeout(t._h);
    t._h = window.setTimeout(function () { t.classList.remove("show"); }, 2400);
  }

  // Filters: any [data-filter-root] with selects/inputs filters [data-cat][data-loc][data-status] rows.
  document.querySelectorAll("[data-filter-root]").forEach(function (root) {
    var scope = document.getElementById(root.getAttribute("data-filter-root"));
    if (!scope) return;
    var inputs = root.querySelectorAll("select, input");
    var empty = scope.parentElement ? scope.parentElement.querySelector("[data-empty]") : null;
    function apply() {
      var cat = (root.querySelector('[name="fcat"]') || {}).value || "";
      var loc = ((root.querySelector('[name="floc"]') || {}).value || "").toLowerCase();
      var st = (root.querySelector('[name="fstatus"]') || {}).value || "";
      var q = ((root.querySelector('[name="fq"]') || {}).value || "").toLowerCase();
      var visible = 0;
      scope.querySelectorAll("[data-cat]").forEach(function (row) {
        var ok = true;
        if (cat && row.getAttribute("data-cat") !== cat) ok = false;
        if (st && row.getAttribute("data-status") !== st) ok = false;
        if (loc && (row.getAttribute("data-loc") || "").toLowerCase().indexOf(loc) === -1) ok = false;
        if (q && row.textContent.toLowerCase().indexOf(q) === -1) ok = false;
        row.setAttribute("data-hidden", ok ? "false" : "true");
        if (row.tagName === "TR") row.style.display = ok ? "" : "none";
        else row.style.display = ok ? "" : "none";
        if (ok) visible++;
      });
      if (empty) empty.classList.toggle("show", visible === 0);
    }
    inputs.forEach(function (i) { i.addEventListener("input", apply); i.addEventListener("change", apply); });
    var reset = root.querySelector("[data-reset]");
    if (reset) reset.addEventListener("click", function () {
      inputs.forEach(function (i) { i.value = ""; });
      apply();
    });
  });

  // Dismissible notices.
  document.querySelectorAll(".notice-x").forEach(function (x) {
    x.addEventListener("click", function () {
      var li = x.closest("li");
      if (li) li.remove();
      toast("Notification dismissed.");
    });
  });
})();
