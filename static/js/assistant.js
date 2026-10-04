// ShareHope assistant — fetch chat with safe rendering (textContent only,
// so AI output can never execute as HTML). Normal form POST remains as a
// no-JS fallback. Prevents duplicate submits while a reply is pending.
(function () {
  "use strict";
  var composer = document.getElementById("botComposer");
  var box = document.getElementById("botbox");
  var sendBtn = document.getElementById("botSendBtn");
  var thread = document.getElementById("botThread");
  var typing = document.getElementById("botTyping");
  var errBox = document.getElementById("botError");
  if (!composer || !box || !sendBtn || !thread) return;

  function bubble(who, text) {
    var div = document.createElement("div");
    div.className = "bubble " + (who === "You" ? "me" : "them");
    var head = document.createElement("strong");
    head.textContent = who;
    var body = document.createElement("div");
    body.textContent = text;
    div.appendChild(head);
    div.appendChild(document.createElement("br"));
    div.appendChild(body);
    thread.appendChild(div);
    div.scrollIntoView({ block: "nearest" });
  }

  var pending = false;
  composer.addEventListener("submit", function (e) {
    var text = box.value.trim();
    if (!text || pending) {
      if (!text) return; // HTML5 required handles it
      e.preventDefault();
      return;
    }
    e.preventDefault();
    pending = true;
    sendBtn.disabled = true;
    if (typing) typing.hidden = false;
    if (errBox) errBox.hidden = true;
    bubble("You", text);
    var token = composer.querySelector('input[name="csrf_token"]');
    var data = new URLSearchParams();
    data.append("message", text);
    if (token) data.append("csrf_token", token.value);
    fetch(composer.action, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "fetch",
        "Accept": "application/json"
      },
      body: data.toString(),
      credentials: "same-origin"
    }).then(function (r) {
      if (r.status === 429) throw new Error("slow");
      if (!r.ok) throw new Error("bad");
      return r.json();
    }).then(function (d) {
      if (!d || !d.ok) throw new Error("bad");
      bubble("Assistant", d.reply || "(no reply)");
      box.value = "";
    }).catch(function (err) {
      if (errBox) {
        errBox.textContent = (err && err.message === "slow")
          ? "Too many questions in a short time — please wait a little and try again."
          : "Could not reach the assistant — your question is kept above; try again.";
        errBox.hidden = false;
      }
    }).then(function () {
      pending = false;
      sendBtn.disabled = false;
      if (typing) typing.hidden = true;
      box.focus();
    });
  });

  // Suggested questions fill the composer.
  document.querySelectorAll("[data-suggest]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      box.value = btn.getAttribute("data-suggest") || "";
      box.focus();
    });
  });
})();
