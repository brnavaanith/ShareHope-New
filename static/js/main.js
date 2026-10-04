// ShareHope — vanilla JS only. Auto-expanding sidebar, drawer, footer year.
(function () {
  var sidebar = document.getElementById("sidebar");
  var toggle = document.getElementById("navToggle");
  var scrim = document.getElementById("scrim");

  function isMobile() { return window.matchMedia("(max-width: 960px)").matches; }
  function isTouch() { return window.matchMedia("(hover: none)").matches; }
  var touchMode = isTouch();
  function isCollapsed() { return document.body.classList.contains("sidebar-collapsed"); }
  function expandAuto() {
    document.body.classList.remove("sidebar-collapsed");
    hideTip();
  }
  function collapseAuto() {
    document.body.classList.add("sidebar-collapsed");
    hideTip();
    hideTouchScrim();
  }
  function closeDrawer() {
    if (!sidebar) return;
    sidebar.classList.remove("open");
    if (scrim) scrim.hidden = true;
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  }
  // Touch-expanded sidebar (desktop widths, no hover): scrim closes it.
  function showTouchScrim() {
    if (!scrim || isMobile()) return;
    scrim.classList.add("touch-show");
    scrim.hidden = false;
  }
  function hideTouchScrim() {
    if (!scrim) return;
    scrim.classList.remove("touch-show");
    if (!sidebar || !sidebar.classList.contains("open")) scrim.hidden = true;
  }
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      var open = sidebar.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (scrim) scrim.hidden = !open;
    });
    if (scrim) scrim.addEventListener("click", function () {
      if (sidebar.classList.contains("open")) { closeDrawer(); return; }
      if (!isMobile()) collapseAuto(); // touch-expanded desktop sidebar
    });
    sidebar.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function (e) {
        if (isMobile()) { closeDrawer(); return; }
        // Touchscreen at desktop width: first tap expands, second navigates.
        // (Hover/focus auto-expand is disabled in touch mode so emulated
        // mouse events on tap cannot trigger navigation early.)
        if (touchMode && isCollapsed()) {
          e.preventDefault();
          expandAuto();
          showTouchScrim();
        }
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      closeDrawer();
      if (!isMobile() && !sidebar.contains(document.activeElement)) collapseAuto();
    });
  }
  // Desktop auto-expand: hover the rail, or keyboard-focus a link.
  // No toggle button and no stored preference by design. Touch mode uses
  // tap-to-expand instead, so hover/focus handlers stay off there.
  // Hover uses intent delays (expand ~120ms, collapse ~280ms) so quick
  // pointer sweeps across the rail never strobe the layout; timers cancel
  // each other so fast sidebar↔content moves settle without flicker.
  // Focus stays immediate for keyboard users.
  if (sidebar && !isMobile() && !touchMode) {
    var hovering = false;
    var expandTimer = null, collapseTimer = null;
    function clearSideTimers() {
      if (expandTimer) { window.clearTimeout(expandTimer); expandTimer = null; }
      if (collapseTimer) { window.clearTimeout(collapseTimer); collapseTimer = null; }
    }
    sidebar.addEventListener("mouseenter", function () {
      hovering = true;
      if (!isCollapsed()) { clearSideTimers(); return; }
      clearSideTimers();
      expandTimer = window.setTimeout(function () {
        expandTimer = null;
        expandAuto();
      }, 120);
    });
    sidebar.addEventListener("mouseleave", function () {
      hovering = false;
      clearSideTimers();
      collapseTimer = window.setTimeout(function () {
        collapseTimer = null;
        if (!sidebar.contains(document.activeElement)) collapseAuto();
      }, 280);
    });
    sidebar.addEventListener("focusin", function () {
      clearSideTimers();
      expandAuto();
    });
    sidebar.addEventListener("focusout", function () {
      window.setTimeout(function () {
        if (!hovering && !sidebar.contains(document.activeElement)) collapseAuto();
      }, 0);
    });
  }

  // Collapsed-sidebar tooltips: body-level bubble so the scrolling nav
  // never clips it. Mouse + keyboard, fine-pointer desktop only.
  var tip = null;
  function tipOK() {
    return document.body.classList.contains("sidebar-collapsed") &&
      !isMobile() &&
      window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  }
  function hideTip() {
    if (tip && tip.parentNode) tip.parentNode.removeChild(tip);
    tip = null;
  }
  function showTip(link) {
    var label = link.getAttribute("data-tip");
    if (!label || !tipOK()) return;
    hideTip();
    tip = document.createElement("div");
    tip.className = "side-tip";
    tip.textContent = label;
    document.body.appendChild(tip);
    var r = link.getBoundingClientRect();
    var h = tip.offsetHeight || 32;
    var top = Math.min(Math.max(8, r.top + r.height / 2 - h / 2), window.innerHeight - h - 8);
    tip.style.left = (r.right + 10) + "px";
    tip.style.top = Math.max(8, top) + "px";
    window.requestAnimationFrame(function () { if (tip) tip.classList.add("show"); });
  }
  if (sidebar) {
    // Move native titles aside once: custom bubble replaces them (no doubles).
    sidebar.querySelectorAll(".side-link").forEach(function (a) {
      var t = a.getAttribute("title");
      if (t) { a.setAttribute("data-tip", t); a.removeAttribute("title"); }
      a.addEventListener("mouseenter", function () { showTip(a); });
      a.addEventListener("mouseleave", hideTip);
      a.addEventListener("focus", function () { showTip(a); });
      a.addEventListener("blur", hideTip);
    });
    window.addEventListener("scroll", hideTip, true);
    window.addEventListener("resize", hideTip);
  }

  var year = document.getElementById("year");
  if (year) year.textContent = String(new Date().getFullYear());

  // ScrollReveal-style: fade + slight blur/rotation on entry. Stagger siblings lightly.
  // Disabled entirely when reduced motion is preferred.
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var items = document.querySelectorAll(".reveal");
  if (reduce || !("IntersectionObserver" in window)) {
    items.forEach(function (el) { el.classList.add("in"); });
    return;
  }
  var seen = 0;
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) {
        // Gentle stagger: cap the delay so the page never feels slow.
        var delay = Math.min((seen % 4) * 70, 210);
        e.target.style.transitionDelay = delay + "ms";
        e.target.classList.add("in");
        seen++;
        io.unobserve(e.target);
      }
    });
  }, { threshold: 0.1, rootMargin: "0px 0px -6% 0px" });
  items.forEach(function (el) { io.observe(el); });
})();
