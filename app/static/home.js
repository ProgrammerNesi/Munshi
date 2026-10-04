/* Role picker: model warmup badges (ready / warming / failed). */
(function () {
  "use strict";
  function paint(id, v) {
    var el = document.getElementById(id);
    el.textContent = v;
    el.className = v === "ready" || v === "yes" ? "ok" : (v === "failed" || v === "no" ? "bad" : "");
  }
  function poll() {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      var w = h.warmup || {};
      paint("w-stt", w.stt || "?");
      paint("w-llm", w.llm || "?");
      paint("w-ollama", h.ollama ? "yes" : "no");
    }).catch(function () { /* next poll */ });
  }
  setInterval(poll, 2000);
  poll();
})();
