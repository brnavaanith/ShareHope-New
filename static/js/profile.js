// ShareHope profile — submit progress states (progressive enhancement).
// Forms still POST normally (safe full-page submit + CSRF); this only
// disables double submits and labels the in-progress button.
(function () {
  "use strict";
  function wire(formSelector, btnId, busyId, busyText) {
    var forms = document.querySelectorAll(formSelector);
    forms.forEach(function (form) {
      form.addEventListener("submit", function () {
        var btn = document.getElementById(btnId);
        var busy = document.getElementById(busyId);
        // Let HTML5 validation run first; only lock on valid submit.
        if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
        if (btn) {
          btn.disabled = true;
          btn.textContent = busyText;
        }
        if (busy) busy.hidden = false;
        // Safety: re-enable if navigation failed (network error).
        window.setTimeout(function () {
          if (btn) { btn.disabled = false; }
          if (busy) busy.hidden = true;
        }, 8000);
      });
    });
  }
  wire('form[action="/profile"]', "profileSaveBtn", "profileSaving", "Saving…");
  wire('form[action="/profile/password"]', "passwordSaveBtn", "passwordSaving", "Updating…");
})();
