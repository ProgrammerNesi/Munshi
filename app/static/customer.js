/* Customer chat: poll state every 2s, send text/audio, hold-to-record mic. */
(function () {
  "use strict";
  var CID = document.body.dataset.customerId;
  var chat = document.getElementById("chat");
  var typing = document.getElementById("typing");
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
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderMessage(m) {
    var text = String(m.text || "");
    var match = text.match(/https?:\/\/[^\s]+\/t\/[A-Za-z0-9_-]+/);
    var body = match ? text.replace(match[0], "").trim() : text;
    var stamp = new Date(m.ts);
    var time = isNaN(stamp.getTime()) ? "" :
      stamp.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return '<div class="msg ' + m.direction + '"><div class="message-text">' +
      esc(body) + '</div>' +
      (match ? '<a class="track-button" href="' + esc(match[0]) +
        '" target="_blank" rel="noopener noreferrer">Open order tracking</a>' : "") +
      '<div class="message-meta">' + esc(time) +
      (m.direction === "out" ? ' <span class="checks" aria-label="sent">✓✓</span>' : "") +
      "</div></div>";
  }

  function render(state) {
    var key = JSON.stringify(state);
    if (key === lastRendered) return;
    lastRendered = key;

    var newestOrderId = state.orders.length ? state.orders[0].id : null;
    var visibleMessages = state.messages.filter(function (message) {
      var isBill = /^(your order summary:|aapka bill ready hai:)/i.test(message.text);
      return !isBill || message.order_id === newestOrderId;
    });
    chat.innerHTML = visibleMessages.map(renderMessage).join("");
    chat.scrollTop = chat.scrollHeight;

    typing.classList.toggle("hidden", !state.activity);
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
      if (!r.ok) showToast("Message not sent. Please try again.");
      poll();
    }).catch(function () { showToast("Message not sent. Check the local server."); });
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
      showToast("Upload failed. Check the file and local server.");
    });
    e.target.value = "";
  };

  /* Hold-to-record mic: press and hold, release to send. */
  var micBtn = document.getElementById("mic");
  var recorder = null, chunks = [], micPressed = false, micRequest = 0;
  function micStart(e) {
    e.preventDefault();
    if (micPressed) return;
    micPressed = true;
    var request = ++micRequest;
    if (micBtn.setPointerCapture && e.pointerId !== undefined) {
      micBtn.setPointerCapture(e.pointerId);
    }
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      micHint.classList.remove("hidden");
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      if (!micPressed || request !== micRequest) {
        stream.getTracks().forEach(function (t) { t.stop(); });
        return;
      }
      chunks = [];
      var activeRecorder = new MediaRecorder(stream);
      recorder = activeRecorder;
      activeRecorder.ondataavailable = function (ev) { chunks.push(ev.data); };
      activeRecorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var blob = new Blob(chunks, { type: activeRecorder.mimeType || "audio/webm" });
        recorder = null;
        micBtn.classList.remove("rec");
        if (!blob.size) {
          showToast("No audio captured. Hold the microphone while speaking.");
          return;
        }
        var form = new FormData();
        form.append("audio", blob, "mic.webm");
        postForm(form).then(poll).catch(function () {
          showToast("Voice note not sent. Please try again.");
        });
      };
      activeRecorder.start();
      micBtn.classList.add("rec");
    }).catch(function () {
      if (micPressed && request === micRequest) {
        micHint.classList.remove("hidden");  // text input stays usable
      }
    });
  }
  function micStop(e) {
    if (e) e.preventDefault();
    if (!micPressed) return;
    micPressed = false;
    micRequest++;
    if (recorder && recorder.state !== "inactive") recorder.stop();
    micBtn.classList.remove("rec");
  }
  micBtn.addEventListener("pointerdown", micStart);
  ["pointerup", "pointercancel"].forEach(function (ev) {
    micBtn.addEventListener(ev, micStop);
  });

  setInterval(poll, 2000);
  poll();
})();
