/* Owner portal: tabs, 2s polling, big action buttons. No libraries. */
(function () {
  "use strict";
  var openDetail = null, rulesLoaded = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || r.status); });
      return r.json();
    });
  }

  /* -- health strip -- */
  function health() {
    Promise.all([fetch("/api/health").then(function (r) { return r.json(); }),
                 fetch("/api/owner/health_extra").then(function (r) { return r.json(); })])
      .then(function (pair) {
        var h = pair[0], x = pair[1], el = document.getElementById("health");
        var stages = Object.keys(x.last_order.stages || {}).map(function (k) {
          return k + " " + x.last_order.stages[k] + "s";
        }).join(" · ") || "no runs yet";
        el.innerHTML =
          "<span>STT " + esc(h.models.stt_backend) + "</span>" +
          "<span>LLM " + esc(h.models.llm) + "</span>" +
          "<span>agent " + (h.agent ? "on" : "off") + "</span>" +
          "<span class='" + (h.ollama ? "" : "bad") + "'>Ollama " +
          (h.ollama ? "yes" : "no") + "</span>" +
          "<span>last order #" + (x.last_order.order_id || "—") + ": " +
          esc(stages) + "</span>";
      }).catch(function () { /* next poll */ });
  }

  /* -- needs-you strip -- */
  function needs(list) {
    var box = document.getElementById("needs");
    if (!list.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
    box.classList.remove("hidden");
    box.innerHTML = "<h3>Needs you (" + list.length + ")</h3>" + list.map(card).join("");
  }
  function card(o) {
    var btns = o.status === "AWAITING_APPROVAL"
      ? "<button class='big' data-do='approve' data-id='" + o.id + "'>Approve</button>" +
        "<button class='big danger' data-do='decline' data-id='" + o.id + "'>Decline</button>"
      : "<button class='big' data-do='partial' data-id='" + o.id + "'>Accept partial</button>" +
        "<button class='big warn' data-do='recount' data-id='" + o.id + "'>Ask recount</button>" +
        "<button class='big danger' data-do='cancel' data-id='" + o.id + "'>Cancel</button>";
    return "<div class='card'><b>Order #" + o.id + " · " + esc(o.customer) +
      " · ₹" + o.total + "</b>" +
      "<div class='reasons'>" + o.reasons.map(esc).join("<br>") + "</div>" +
      (o.agent_note ? "<div class='agent-note'>Agent: " + esc(o.agent_note) + "</div>" : "") +
      "<div class='row'>" + btns + "</div></div>";
  }

  /* -- board + detail -- */
  function board(data) {
    needs(data.needs_you);
    var html = "";
    Object.keys(data.groups).sort().forEach(function (st) {
      html += "<div class='group'><h3>" + esc(st) +
        "<span class='badge b-" + st + "'>" + data.groups[st].length + "</span></h3>";
      html += data.groups[st].map(function (o) {
        return "<div class='order' data-open='" + o.id + "'><b>#" + o.id + "</b> " +
          esc(o.customer) + " · ₹" + o.total +
          "<div class='status'>" + esc(o.updated_at.slice(0, 16).replace("T", " ")) + "</div></div>";
      }).join("") + "</div>";
    });
    document.getElementById("tab-board").innerHTML = html || "No orders yet.";
    if (openDetail) detail(openDetail, true);
  }
  function detail(id, silent) {
    openDetail = id;
    fetch("/api/owner/order/" + id).then(function (r) { return r.json(); }).then(function (o) {
      var tl = o.timeline.map(function (e) {
        var secs = e.seconds == null ? "" : " <span class='secs'>" + e.seconds + "s</span>";
        return "<li class='k-" + esc(e.kind) + "'><span class='pill p-" + esc(e.kind) + "'>" +
          esc(e.kind) + "</span> <span class='who'>" + esc(e.actor) + "</span> " +
          esc(e.message) + secs + "</li>";
      }).join("");
      var lines = o.lines.map(function (l) {
        return "<tr><td>" + esc(l.name) + "</td><td>" + l.qty + " " + esc(l.unit) +
          "</td><td>₹" + l.unit_price + "</td><td>" + l.packed_qty + "</td></tr>";
      }).join("");
      document.getElementById("tab-detail").innerHTML =
        "<h3>Order #" + o.id + " · " + esc(o.customer) + "</h3>" +
        "<p><i>" + esc(o.transcript || "(no transcript)") + "</i></p>" +
        "<table class='sheet'><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Packed</th></tr>" +
        lines + "</table>" +
        (o.bill ? "<pre class='bill'>Total ₹" + o.bill.total + " (delivery ₹" +
          o.bill.delivery_fee + ") · " + esc(o.bill.eta_text) + "</pre>" : "") +
        "<h4>Reasoning</h4><ul class='tl'>" + tl + "</ul>";
      show("detail");
      if (!silent) document.getElementById("tab-detail").scrollIntoView();
    });
  }

  /* -- khata / stock / inbox / rules -- */
  function khata(rows) {
    document.getElementById("tab-khata").innerHTML = rows.map(function (c) {
      return "<div class='card'><b>" + esc(c.name) + "</b> — ₹" + c.outstanding +
        " of ₹" + c.limit + " · last order " + (c.last_order || "—") +
        "<div class='bar" + (c.pct >= 100 ? " over" : "") + "'><i style='width:" +
        Math.min(c.pct, 100) + "%'></i></div></div>";
    }).join("") || "No customers.";
  }
  function stock(rows) {
    var body = rows.map(function (it) {
      var left = it.days_left == null ? "—" : it.days_left + "d";
      return "<tr class='" + (it.low ? "low" : "") + "'><td>" + esc(it.name) +
        "</td><td>" + it.stock + " " + esc(it.unit) + "</td><td>" + left +
        "</td><td>" + it.used_per_day + "/d</td></tr>";
    }).join("");
    document.getElementById("tab-stock").innerHTML =
      "<table class='sheet'><tr><th>Item</th><th>Stock</th><th>Left</th><th>Use</th></tr>" +
      body + "</table>";
  }
  function inbox(feed) {
    document.getElementById("bell").textContent = feed.unread;
    document.getElementById("tab-inbox").innerHTML = feed.items.map(function (n) {
      return "<div class='card'>" + esc(n.text) +
        "<div class='row'><span class='muted'>#" + n.id + " · " + esc(n.role) +
        (n.order_id ? " · order #" + n.order_id : "") + "</span>" +
        (n.done ? "" : "<button class='big ghost' data-done='" + n.id + "'>Done</button>") +
        "</div></div>";
    }).join("") || "Inbox empty.";
  }
  function rules() {
    if (rulesLoaded) return;
    rulesLoaded = true;
    fetch("/api/owner/rules").then(function (r) { return r.json(); }).then(function (j) {
      document.getElementById("rules-text").value = j.text;
    });
  }

  /* -- tabs + actions (delegated, works after every re-render) -- */
  function show(name) {
    document.querySelectorAll("#tabs button").forEach(function (b) {
      b.classList.toggle("on", b.dataset.tab === name || (name === "detail" && b.dataset.tab === "board"));
    });
    ["board", "detail", "khata", "stock", "rules", "inbox"].forEach(function (t) {
      document.getElementById("tab-" + t).classList.toggle("hidden", t !== name);
    });
  }
  document.getElementById("tabs").onclick = function (e) {
    var t = e.target.closest("button");
    if (t) show(t.dataset.tab);
  };
  document.body.onclick = function (e) {
    var open = e.target.closest("[data-open]");
    if (open) { detail(open.dataset.open); return; }
    var done = e.target.closest("[data-done]");
    if (done) {
      post("/api/owner/notifications/" + done.dataset.done + "/done").then(poll);
      return;
    }
    var btn = e.target.closest("[data-do]");
    if (!btn) return;
    var id = btn.dataset.id, do_ = btn.dataset.do;
    var p;
    if (do_ === "approve") p = post("/api/owner/order/" + id + "/approve");
    else if (do_ === "decline") {
      var reason = prompt("Reason for declining? (optional)") || "";
      p = post("/api/owner/order/" + id + "/decline", { reason: reason });
    }
    else p = post("/api/owner/order/" + id + "/mismatch",
                  { mode: do_ === "partial" ? "accept_partial" : do_ });
    p.then(function () { openDetail = null; show("board"); poll(); })
     .catch(function (err) { alert("Failed: " + err.message); });
  };
  document.getElementById("rules-save").onclick = function () {
    var msg = document.getElementById("rules-msg");
    msg.textContent = "saving…";
    post("/api/owner/rules", { text: document.getElementById("rules-text").value })
      .then(function () { msg.textContent = "saved — live now."; })
      .catch(function (err) { msg.textContent = "not saved: " + err.message; });
  };

  /* -- poll every 2s -- */
  function poll() {
    fetch("/api/owner/board").then(function (r) { return r.json(); }).then(board).catch(function () {});
    fetch("/api/owner/khata").then(function (r) { return r.json(); }).then(khata).catch(function () {});
    fetch("/api/owner/stock").then(function (r) { return r.json(); }).then(stock).catch(function () {});
    fetch("/api/owner/notifications").then(function (r) { return r.json(); }).then(inbox).catch(function () {});
    fetch("/api/state").then(function (r) { return r.json(); }).then(function (s) {
      document.getElementById("bell").textContent = s.notifications;
    }).catch(function () {});
    health();
    rules();
  }
  setInterval(poll, 2000);
  poll();
})();
