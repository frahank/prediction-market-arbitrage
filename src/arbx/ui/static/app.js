(function () {
  function applyStatus(payload) {
    var banner = document.getElementById("mode-banner");
    if (!banner || !payload || !payload.ok || !payload.data) {
      return;
    }
    var data = payload.data;
    var live = data.mode === "live" && data.real_orders_enabled === true;
    var kill = data.killswitch_engaged === true;
    banner.classList.toggle("is-live", live);
    banner.classList.toggle("is-paper", !live);
    banner.classList.toggle("is-kill", kill);
    banner.textContent = live ? "LIVE - REAL MONEY" : "PAPER";
    if (kill) {
      banner.textContent = banner.textContent + " / KILL ENGAGED";
      if (data.killswitch_reason) {
        banner.textContent = banner.textContent + " - " + data.killswitch_reason;
      }
    }
  }

  async function pollStatus() {
    try {
      var response = await fetch("/api/get_app_status", { headers: { "accept": "application/json" } });
      applyStatus(await response.json());
    } catch (_err) {
      return;
    }
  }

  var banner = document.getElementById("mode-banner");
  var intervalMs = Number(banner && banner.dataset.statusPollMs) || 5000;
  pollStatus();
  window.setInterval(pollStatus, intervalMs);
}());
