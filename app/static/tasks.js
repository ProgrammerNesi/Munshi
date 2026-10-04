/* Packer + delivery task boards. Big buttons, optimistic refresh, plain fetch. */
(function () {
  "use strict";
  var ROLE = document.body.dataset.role;
  var list = document.getElementById("list");
  var fails = 0;

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
  function linesHtml(o) {
    if (ROLE === "packer") {
      return o.lines.map(function (l) {
        return "<tr><td>" + esc(l.name) + "<br><span class='muted'>Requested: " +
          l.qty + " " + esc(l.unit) + "</span></td>" +
          "<td><input type='number' min='0' step='any' data-item='" + l.item_id +
          "' value='" + l.qty + "' aria-label='" + esc(l.name) + " packed'></td></tr>";
      }).join("");
    }
    return o.lines.map(function (l) {
      return "<tr><td>" + esc(l.name) + "</td><td>" + l.qty + " " + esc(l.unit) + "</td></tr>";
    }).join("");
  }
  function render(orders) {
    if (!orders.length) {
      list.innerHTML = "<div class='empty'>No orders to work on right now.</div>";
      return;
    }
    list.innerHTML = orders.map(function (o) {
      var btns = ROLE === "packer"
        ? "<button class='big' data-pack='" + o.id + "'>Packing complete ✓</button>"
        : (o.status === "READY_FOR_DELIVERY"
          ? "<button class='big' data-start='" + o.id + "'>Start delivery 🛵</button>"
          : "<div class='seg' data-pay='" + o.id + "'>" +
            "<button data-mode='cash'>Cash</button>" +
            "<button data-mode='upi'>UPI</button>" +
            "<button data-mode='credit'>Shop credit</button></div>" +
            "<button class='big warn' data-problem='" + o.id + "'>Report a delivery issue</button>");
      return "<div class='task' data-order='" + o.id + "'><h3>#" + o.id + " · " +
        esc(o.customer) + " · " + esc(o.area || "") + " · ₹" + o.total + "</h3>" +
        "<table>" + linesHtml(o) + "</table><div class='row'>" + btns + "</div></div>";
    }).join("");
  }
  function poll() {
    var url = ROLE === "packer" ? "/api/tasks/packing" : "/api/tasks/delivery";
    fetch(url).then(function (r) { return r.json(); }).then(function (orders) {
      fails = 0;
      document.getElementById("net").classList.add("hidden");
      // Don't clobber typed counts while the packer is editing.
      if (ROLE === "packer" && document.activeElement &&
          document.activeElement.type === "number") return;
      render(orders);
    }).catch(function () {
      if (++fails >= 3) document.getElementById("net").classList.remove("hidden");
    });
  }
  document.body.addEventListener("click", function (e) {
    function q(sel) { var el = e.target.closest(sel); return el; }
    var b;
    if ((b = q("[data-pack]"))) {
      var counts = {};
      b.closest(".task").querySelectorAll("input[data-item]").forEach(function (inp) {
        counts[inp.dataset.item] = parseFloat(inp.value) || 0;
      });
      post("/api/tasks/packing/" + b.dataset.pack, { counts: counts })
        .then(poll).catch(function (err) { alert(err.message); });
    } else if ((b = q("[data-start]"))) {
      post("/api/tasks/delivery/" + b.dataset.start + "/start").then(poll)
        .catch(function (err) { alert(err.message); });
    } else if ((b = q("[data-pay] button"))) {
      post("/api/tasks/delivery/" + b.closest("[data-pay]").dataset.pay + "/delivered",
           { payment_mode: b.dataset.mode }).then(poll)
        .catch(function (err) { alert(err.message); });
    } else if ((b = q("[data-problem]"))) {
      var note = prompt("What went wrong? (For example: shop closed or wrong address)") || "";
      if (!note.trim()) return;
      post("/api/tasks/delivery/" + b.dataset.problem + "/problem", { note: note })
        .then(poll).catch(function (err) { alert(err.message); });
    }
  });
  setInterval(poll, 2000);
  poll();
})();
