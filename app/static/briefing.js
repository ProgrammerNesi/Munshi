/* Morning briefing: one fetch, summary on top, fact cards below. */
(function () {
  "use strict";
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  }
  function card(big, small) {
    return "<div class='card'><b style='font-size:22px'>" + esc(big) +
      "</b><div class='muted'>" + esc(small) + "</div></div>";
  }
  fetch("/api/owner/briefing").then(function (r) { return r.json(); }).then(function (b) {
    var f = b.facts;
    document.getElementById("meta").innerHTML =
      "<span>" + esc(b.date) + "</span><span>" +
      (b.llm_used ? "LLM summary" : "template summary") + "</span><span>" +
      b.seconds + "s</span>";
    document.getElementById("summary").textContent = b.summary;
    var cards = [
      card(f.orders, "orders yesterday"),
      card("₹" + f.revenue.toLocaleString("en-IN"), "revenue"),
      card(f.auto_handled + " of " + f.orders, "handled without you"),
      card(f.pending_approval + f.pending_mismatch, "need you now"),
      card(f.over_limit.length, "near credit limit"),
      card(f.quiet.length, "gone quiet"),
      card(f.runout.length, "running out (3d)"),
      card(f.exceptions.length, "exceptions"),
    ];
    document.getElementById("cards").innerHTML = cards.join("");
    var rows = [];
    f.over_limit.forEach(function (c) {
      rows.push("<div class='card'>💳 <b>" + esc(c.name) + "</b> ₹" + c.outstanding +
        " / ₹" + c.limit + " (" + c.pct + "%)</div>");
    });
    f.quiet.forEach(function (q) {
      rows.push("<div class='card'>🔇 <b>" + esc(q.name) + "</b> — " + q.days_since +
        " din, usually har " + q.usual_every_days + " din</div>");
    });
    f.runout.forEach(function (it) {
      rows.push("<div class='card'>📦 <b>" + esc(it.name) + "</b> — " + it.stock +
        " " + esc(it.unit) + " (~" + it.days_left + " din)</div>");
    });
    f.exceptions.forEach(function (p) {
      rows.push("<div class='card'>⚠️ Order #" + p.order_id + " — " +
        esc(p.message) + "</div>");
    });
    document.getElementById("detail").innerHTML = rows.join("") || "Sab badhiya. 🎉";
  }).catch(function () {
    document.getElementById("summary").textContent = "Load nahi hua — dobara try kijiye.";
  });
})();
