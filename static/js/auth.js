// Auth forms: NGO conditional fields, client-side validation, loading state.
(function () {
  var form = document.querySelector("[data-auth-form]");
  if (!form) return;

  var roleInputs = form.querySelectorAll('input[name="role"]');
  var ngoBlock = document.getElementById("ngoFields");

  function syncRole() {
    var checked = form.querySelector('input[name="role"]:checked');
    var isNgo = checked && checked.value === "ngo";
    if (ngoBlock) ngoBlock.classList.toggle("show", !!isNgo);
    if (ngoBlock) ngoBlock.querySelectorAll("input, textarea").forEach(function (el) {
      el.required = !!isNgo && el.hasAttribute("data-ngo-required");
    });
  }
  roleInputs.forEach(function (r) { r.addEventListener("change", syncRole); });
  syncRole();

  // Preselect ?type=ngo
  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get("type") === "ngo") {
      var ngo = form.querySelector('input[name="role"][value="ngo"]');
      if (ngo) { ngo.checked = true; syncRole(); }
    }
  } catch (e) { /* ignore */ }

  form.addEventListener("submit", function () {
    var valid = true;
    form.querySelectorAll("[required]").forEach(function (el) {
      var bad = !el.value || (el.type === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(el.value));
      el.setAttribute("aria-invalid", bad ? "true" : "false");
      var err = el.closest(".field") ? el.closest(".field").querySelector(".field-error") : null;
      if (err) err.textContent = bad ? "This field needs a valid value." : "";
      if (bad) valid = false;
    });
    var pw = form.querySelector('input[name="password"]');
    var confirm = form.querySelector('input[name="confirm"]');
    if (pw && confirm && pw.value !== confirm.value) {
      confirm.setAttribute("aria-invalid", "true");
      var cerr = confirm.closest(".field").querySelector(".field-error");
      if (cerr) cerr.textContent = "Passwords do not match.";
      valid = false;
    }
    if (!valid) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = "Please wait…"; }
  });

  // Clear credential fields that the browser restored for us.
  // Browsers re-fill forms on a fresh load, on back/forward navigation
  // and from bfcache, and that restoration is unaffected by no-store.
  // Only values the SERVER did not supply are cleared: on a failed
  // login the route echoes the email back as a value attribute so the
  // useful validation value is preserved.
  function serverSupplied() {
    var out = {};
    form.querySelectorAll("input[name]").forEach(function (el) {
      if (el.tagName === "INPUT" && el.hasAttribute("value")) {
        out[el.name] = el.getAttribute("value") || "";
      }
    });
    return out;
  }

  function clearRestoredFields() {
    var keep = serverSupplied();
    // Never clear a value the server deliberately rendered: it is a
    // deliberate validation value (e.g. the email echoed after a failed
    // login, or a signup field the server re-filled).
    var clearable = form.querySelectorAll('input[type="email"], input[type="password"]');
    clearable.forEach(function (el) {
      if (keep[el.name]) return;
      if (el.value) el.value = "";
      el.setAttribute("aria-invalid", "false");
      var err = el.closest(".field") ? el.closest(".field").querySelector(".field-error") : null;
      if (err && err.textContent.indexOf("Passwords do not match") === -1) err.textContent = "";
    });
    // Passwords are never worth keeping, even on a validation failure.
    form.querySelectorAll('input[type="password"]').forEach(function (el) {
      if (el.value) el.value = "";
    });
  }

  // Run on first paint and on every back/forward restore.
  clearRestoredFields();
  window.addEventListener("pageshow", clearRestoredFields);
  // Password managers/autofill can land just after DOMContentLoaded.
  window.addEventListener("load", clearRestoredFields);
})();
