// ShareHope donation stepper — used ONLY on the Tracking page.
// Browsing steps never changes any donation status; each stepper starts on
// the slip's real stage (server-rendered via data-start) with real timestamps.
(function () {
  "use strict";
  var roots = document.querySelectorAll("[data-stepper]");
  if (!roots.length) return;

  function init(root) {
  var rail = root.querySelector(".stepper-rail");
  var nodes = Array.prototype.slice.call(root.querySelectorAll(".stepper-node"));
  var dots = nodes.map(function (n) { return n.querySelector(".stepper-dot"); });
  var detail = root.querySelector(".stepper-detail");
  var prev = root.querySelector("[data-prev]");
  var next = root.querySelector("[data-next]");
  var count = root.querySelector(".stepper-count");
  var total = nodes.length;
  var start = parseInt(root.getAttribute("data-start") || "0", 10);
  var current = (start >= 0 && start < total) ? start : 0;

  function describe(i) {
    var tag = nodes[i].getAttribute("data-title") || ("Step " + (i + 1));
    var body = nodes[i].getAttribute("data-body") || "";
    var time = nodes[i].getAttribute("data-time") || "";
    detail.innerHTML =
      "<h3>" + tag + "</h3><p>" + body + "</p>" +
      "<p class=\"stepper-time\">" + time + "</p>";
  }

  function render() {
    nodes.forEach(function (n, i) {
      var state = i < current ? "done" : (i === current ? "now" : "todo");
      n.setAttribute("data-state", state);
      dots[i].setAttribute("aria-current", i === current ? "step" : "false");
    });
    var pct = total > 1 ? (current / (total - 1)) * 100 : 0;
    rail.style.setProperty("--fill", pct + "%");
    describe(current);
    // Replay the panel transition on every move.
    detail.classList.remove("swap");
    void detail.offsetWidth;
    detail.classList.add("swap");
    prev.disabled = current === 0;
    next.disabled = current === total - 1;
    if (count) count.textContent = "Step " + (current + 1) + " of " + total;
  }

  prev.addEventListener("click", function () {
    if (current > 0) { current -= 1; render(); }
  });
  next.addEventListener("click", function () {
    if (current < total - 1) { current += 1; render(); }
  });
  dots.forEach(function (dot, i) {
    dot.addEventListener("click", function () { current = i; render(); });
  });

  render();
  }

  Array.prototype.forEach.call(roots, init);
})();
