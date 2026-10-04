/* Customer chat: poll state every 2s, send text/audio, hold-to-record mic. */
(function () {
  "use strict";
  var CID = document.body.dataset.customerId;
  var chat = document.getElementById("chat");
  var ordersBox = document.getElementById("orders");
  var activity = document.getElementById("activity");
  var activityLabel = document.getElementById("activity-label");
  var busy = document.getElementById("busy");
  var toast = document.getElementById("toast");
  var textBox = document.getElementById("text");
  var micHint = document.getElementById("mic-hint");
  var lastRendered = "";
  var pollFails = 0;

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.remove("hidden");
    setTimeout(function () { toast.classList.add("hidden"); }, 4000);
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  }

  function render(state) {
    var key = JSON.stringify(state);
    if (key === lastRendered) return;
    lastRendered = key;

    chat.innerHTML = state.messages.map(function (m) {
      return '<div class="msg ' + m.direction + '">' + esc(m.text) + "</div>";
    }).join("");
    chat.scrollTop = chat.scrollHeight;

    ordersBox.innerHTML = state.orders.map(function (o) {
      var bill = "";
      if (o.bill) {
        var rows = o.bill.lines.map(function (l) {
          return "<tr><td>" + esc(l.name) + " " + l.qty + " " + esc(l.unit) +
            "</td><td>₹" + l.line_total + "</td></tr>";
        }).join("");
        bill = '<div class="bill"><h4>Bill #' + o.id + "</h4><table>" + rows +
          '<tr><td>Delivery</td><td>₹' + o.bill.delivery_fee + "</td></tr>" +
          '<tr class="total"><td>Total</td><td>₹' + o.bill.total + "</td></tr>" +
          "</table><div class='status'>" + esc(o.bill.eta_text) + "</div></div>";
      }
      return bill + '<div class="status">Order #' + o.id + ": " + esc(o.status) +
        (state.notifications ? " · 🔔" + state.notifications : "") + "</div>";
    }).join("");

    if (state.activity) {
      activityLabel.textContent = state.activity;
      activity.classList.remove("hidden");
    } else {
      activity.classList.add("hidden");
    }
    busy.classList.toggle("hidden", !(state.queue_depth > 3));
  }

  function poll() {
    fetch("/api/state?customer_id=" + CID)
      .then(function (r) { return r.json(); })
      .then(function (state) {
        pollFails = 0;
        document.getElementById("offline").classList.add("hidden");
        render(state);
      })
      .catch(function () {
        // Show the banner only after 3 straight failures (no flapping).
        if (++pollFails >= 3) {
          document.getElementById("offline").classList.remove("hidden");
        }
      });
  }

  function postForm(form) {
    return fetch("/api/customer/" + CID + "/message", { method: "POST", body: form });
  }

  function sendText() {
    var v = textBox.value.trim();
    if (!v) return;
    textBox.value = "";
    var form = new FormData();
    form.append("text", v);
    postForm(form).then(function (r) {
      if (!r.ok) showToast("Bhejne me dikkat aayi, dobara try kijiye.");
      poll();
    }).catch(function () { showToast("Upload failed — net ya server dekhiye."); });
  }

  document.getElementById("send").onclick = sendText;
  textBox.onkeydown = function (e) { if (e.key === "Enter") sendText(); };
  document.getElementById("who").onchange = function (e) {
    window.location = "/customer/" + e.target.value;
  };
  document.getElementById("file").onchange = function (e) {
    if (!e.target.files.length) return;
    var form = new FormData();
    form.append("audio", e.target.files[0]);
    if (textBox.value.trim()) form.append("text", textBox.value.trim());
    postForm(form).then(poll).catch(function () {
      showToast("Upload failed — file badi ya net slow ho sakta hai.");
    });
    e.target.value = "";
  };

  /* Hold-to-record mic: press and hold, release to send. */
  var micBtn = document.getElementById("mic");
  var recorder = null, chunks = [];
  function micStart(e) {
    e.preventDefault();
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      micHint.classList.remove("hidden");
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = function (ev) { chunks.push(ev.data); };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        var form = new FormData();
        form.append("audio", blob, "mic.webm");
        postForm(form).then(poll).catch(function () {
          showToast("Upload failed — dobara try kijiye.");
        });
      };
      recorder.start();
      micBtn.classList.add("rec");
    }).catch(function () {
      micHint.classList.remove("hidden");  // text input stays usable
    });
  }
  function micStop(e) {
    if (e) e.preventDefault();
    if (recorder && recorder.state !== "inactive") recorder.stop();
    micBtn.classList.remove("rec");
  }
  micBtn.addEventListener("pointerdown", micStart);
  ["pointerup", "pointerleave", "pointercancel"].forEach(function (ev) {
    micBtn.addEventListener(ev, micStop);
  });

  setInterval(poll, 2000);
  poll();
})();
