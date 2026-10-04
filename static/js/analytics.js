// ShareHope analytics — dependency-free SVG charts from LIVE data.
// The server embeds aggregates in #analytics-data (same numbers as the
// tables); no sample dataset remains. Charts re-render on filter Apply
// via full page load, so cards and charts always agree.
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var raw = document.getElementById("analytics-data");
  if (!raw) return;
  var DATA;
  try { DATA = JSON.parse(raw.textContent); }
  catch (e) { DATA = null; }
  if (!DATA) return;
  var trends = DATA.trends || [];
  var byCategory = DATA.by_category || [];
  var byStatus = DATA.by_status || [];

  function el(name, attrs, parent) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function label(parent, x, y, text, anchor, size, fill, bold) {
    var t = el("text", { x: x, y: y, "text-anchor": anchor || "middle", "font-size": size || 10, fill: fill || "#756367", "font-family": "IBM Plex Mono, monospace" }, parent);
    t.textContent = text;
    if (bold) t.setAttribute("font-weight", "700");
    return t;
  }
  function emptyNote(svg, msg) {
    label(svg, 240, 120, msg, "middle", 12);
  }
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- Line chart: offers + completed per bucket ---
  (function line() {
    var svg = document.getElementById("chartLine");
    if (!svg) return;
    if (!trends.length) { emptyNote(svg, "No data in range"); return; }
    var vals = trends.map(function (d) { return d.offers; });
    var max = Math.max.apply(null, vals.concat([1])) * 1.2;
    // Cap buckets for legibility; aggregate the tail into the last point.
    var show = trends.slice(-24);
    var W = 480, H = 240, P = { l: 38, r: 12, t: 14, b: 30 };
    function X(i) { return P.l + (show.length < 2 ? (W - P.l - P.r) / 2 : (i * (W - P.l - P.r)) / (show.length - 1)); }
    function Y(v) { return H - P.b - (v / max) * (H - P.t - P.b); }
    [0.25, 0.5, 0.75, 1].forEach(function (f) {
      var y = Y(max * f);
      el("line", { x1: P.l, x2: W - P.r, y1: y, y2: y, stroke: "#E9DCD2", "stroke-width": 1 }, svg);
    });
    function series(key, color) {
      var pts = show.map(function (d, i) { return X(i).toFixed(1) + "," + Y(d[key]).toFixed(1); }).join(" ");
      var pl = el("polyline", { points: pts, fill: "none", stroke: color, "stroke-width": key === "offers" ? 2.5 : 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      if (!reduce && key === "offers") {
        var len = 1400;
        pl.style.strokeDasharray = String(len);
        pl.style.strokeDashoffset = String(len);
        pl.getBoundingClientRect();
        pl.style.transition = "stroke-dashoffset 900ms cubic-bezier(0.22,0.61,0.21,1)";
        pl.style.strokeDashoffset = "0";
      }
      show.forEach(function (d, i) {
        el("circle", { cx: X(i), cy: Y(d[key]), r: 3.5, fill: color, stroke: "#fff", "stroke-width": 1.5 }, svg);
      });
    }
    series("offers", "#4A2E35");
    series("completed", "#E37A5C");
    // Sparse x labels: first, middle, last.
    var idx = [0, Math.floor((show.length - 1) / 2), show.length - 1].filter(function (v, i, a) { return a.indexOf(v) === i; });
    idx.forEach(function (i) { label(svg, X(i), H - 10, show[i].label, "middle", 9); });
    var peak = Math.max.apply(null, vals);
    show.forEach(function (d, i) {
      if (d.offers === peak && peak > 0) label(svg, X(i), Y(d.offers) - 10, String(d.offers), "middle", 10, "#24181A", true);
    });
  })();

  // --- Bar chart: eight catalog categories ---
  (function bars() {
    var svg = document.getElementById("chartBar");
    if (!svg) return;
    if (!byCategory.length) { emptyNote(svg, "No data in range"); return; }
    var W = 480, H = 240, P = { l: 10, r: 10, t: 16, b: 44 };
    var max = Math.max.apply(null, byCategory.map(function (d) { return d.offers; }).concat([1])) * 1.12;
    var slot = (W - P.l - P.r) / byCategory.length;
    var bw = Math.min(38, slot * 0.58);
    var peak = Math.max.apply(null, byCategory.map(function (d) { return d.offers; }));
    byCategory.forEach(function (d, i) {
      var h = ((H - P.t - P.b) * d.offers) / max;
      var x = P.l + slot * i + (slot - bw) / 2;
      var y = H - P.b - h;
      el("rect", { x: x.toFixed(1), y: y.toFixed(1), width: bw.toFixed(1), height: Math.max(h, 2).toFixed(1), rx: 3, fill: (d.offers === peak && peak > 0) ? "#E37A5C" : "#4A2E35", opacity: 0.92 }, svg);
      label(svg, x + bw / 2, y - 6, String(d.offers), "middle", 10, "#24181A", true);
      var short = d.name.split(" &")[0].split(" ")[0];
      var t = label(svg, x + bw / 2, H - 26, short, "middle", 9);
      if (short.length > 7) t.setAttribute("font-size", 8);
    });
  })();

  // --- Donut chart: four workflow statuses ---
  (function donut() {
    var svg = document.getElementById("chartPie");
    if (!svg) return;
    var colors = { handed_over: "#4A2E35", accepted: "#A9502E", pending: "#E37A5C", declined: "#C9B3A6" };
    var names = { handed_over: "Handed over", accepted: "Accepted", pending: "Pending", declined: "Declined" };
    var total = byStatus.reduce(function (a, d) { return a + d.count; }, 0);
    var legend = document.getElementById("pieLegend");
    if (!total) {
      emptyNote(svg, "No donations yet");
      if (legend) legend.textContent = "All statuses are zero in this range.";
      return;
    }
    var R = 84, C = 2 * Math.PI * R, off = 0;
    byStatus.forEach(function (d) {
      var frac = d.count / total;
      if (!frac) return;
      el("circle", {
        cx: 120, cy: 120, r: R, fill: "none", stroke: colors[d.status] || "#756367", "stroke-width": 34,
        "stroke-dasharray": (frac * C).toFixed(1) + " " + C.toFixed(1),
        "stroke-dashoffset": (-off * C).toFixed(1), transform: "rotate(-90 120 120)"
      }, svg);
      off += frac;
    });
    var pct = Math.round((byStatus.filter(function (d) { return d.status === "handed_over"; })[0] || { count: 0 }).count / total * 10) / 10;
    var t1 = el("text", { x: 120, y: 116, "text-anchor": "middle", "font-size": 26, "font-family": "Fraunces, serif", fill: "#38222A", "font-weight": 600 }, svg);
    t1.textContent = pct + "%";
    label(svg, 120, 136, "handed over", "middle", 10);
    if (legend) byStatus.forEach(function (d) {
      var s = document.createElement("span");
      var i = document.createElement("i");
      i.style.background = colors[d.status] || "#756367";
      s.appendChild(i);
      s.appendChild(document.createTextNode((names[d.status] || d.status) + " · " + d.count));
      legend.appendChild(s);
    });
  })();
})();
