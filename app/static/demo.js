/* Demo clock badge (every portal) + Skip button (owner portal, demo only). */
(function () {
  "use strict";
  function badge(text) {
    var d = document.getElementById("demo-badge");
    if (!d) {
      d = document.createElement("div");
      d.id = "demo-badge";
      d.setAttribute("style",
        "position:sticky;top:0;z-index:99;background:#6a1b9a;color:#fff;" +
        "text-align:center;font-size:13px;padding:4px;");
      document.body.prepend(d);
    }
    d.textContent = text;
  }
  function refresh() {
    fetch("/api/demo/clock").then(function (r) { return r.json(); }).then(function (c) {
      if (!c.demo) return;
      badge("DEMO CLOCK +" + c.offset_min + " min");
      var bar = document.getElementById("demo-bar");
      if (bar) {
        bar.hidden = false;
        var off = document.getElementById("demo-off");
        if (off) off.textContent = "+" + c.offset_min + " min";
      }
    }).catch(function () {});
  }
  document.addEventListener("click", function (e) {
    if (e.target && e.target.id === "demo-skip") {
      fetch("/api/demo/skip?minutes=10", { method: "POST" })
        .then(refresh).catch(function () {});
    }
  });
  refresh();
})();
