// ShareHope messages — polling refresh (not real-time delivery).
// Polls the thread JSON every 5s and appends only unseen ids.
// Rendering uses textContent so stored HTML can never execute (XSS-safe).
(function () {
  var thread = document.getElementById("msgThread");
  if (!thread) return;
  var withId = thread.getAttribute("data-with-id");
  var jsonUrl = thread.getAttribute("data-json-url");
  if (!withId || !jsonUrl) return;
  var errBox = document.getElementById("msgError");
  var composer = document.getElementById("msgComposer");
  var sendBtn = document.getElementById("msgSendBtn");
  var sending = document.getElementById("msgSending");
  var box = document.getElementById("msgbox");

  var lastId = parseInt(thread.getAttribute("data-last-id") || "0", 10) || 0;
  var seen = {};
  thread.querySelectorAll("[data-msg-id]").forEach(function (el) {
    seen[el.getAttribute("data-msg-id")] = true;
  });

  function fmtDate(iso) {
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      return d.toLocaleString(undefined, {
        day: "2-digit", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit"
      });
    } catch (e) { return ""; }
  }

  function appendMsg(m) {
    if (!m || seen[m.id]) return;
    seen[m.id] = true;
    if (m.id > lastId) lastId = m.id;
    var div = document.createElement("div");
    div.className = "bubble " + (m.from_me ? "me" : "them");
    div.setAttribute("data-msg-id", String(m.id));
    var head = document.createElement("div");
    var strong = document.createElement("strong");
    strong.textContent = m.from_me ? "You" : (m.sender || "—");
    head.appendChild(strong);
    var when = document.createElement("span");
    when.className = "small";
    when.textContent = " · " + fmtDate(m.created_at);
    head.appendChild(when);
    var body = document.createElement("div");
    body.textContent = m.content || "";
    div.appendChild(head);
    div.appendChild(body);
    // remove the "No messages yet" placeholder on first real message
    var empty = thread.querySelector(".honest-empty");
    if (empty) empty.remove();
    thread.appendChild(div);
    thread.scrollTop = thread.scrollHeight;
  }

  var failures = 0;
  function poll() {
    var url = jsonUrl + "?after_id=" + encodeURIComponent(String(lastId));
    fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        failures = 0;
        if (errBox) errBox.hidden = true;
        if (data && data.ok && Array.isArray(data.messages)) {
          data.messages.forEach(appendMsg);
        }
      })
      .catch(function () {
        failures++;
        // Show a gentle error only after the first failure so blips stay quiet.
        if (errBox && failures >= 1) errBox.hidden = false;
      });
  }

  var timer = window.setInterval(poll, 5000);
  window.addEventListener("beforeunload", function () {
    if (timer) window.clearInterval(timer);
  });

  // Sending state: disable double submits (server also dedups by content+time).
  if (composer && sendBtn) {
    composer.addEventListener("submit", function () {
      if (box && !box.value.trim()) {
        // Let HTML5 required + server 400 handle it; don't lock the button.
        return;
      }
      sendBtn.disabled = true;
      if (sending) sending.hidden = false;
      // Re-enable after 8s in case the navigation failed (network error).
      window.setTimeout(function () {
        sendBtn.disabled = false;
        if (sending) sending.hidden = true;
      }, 8000);
    });
  }
})();
