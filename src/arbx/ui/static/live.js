(function () {
  var workspace = document.querySelector("[data-live-workspace]");
  if (!workspace) {
    return;
  }

  var liveHero = workspace.querySelector("[data-live-hero]");
  var liveMode = workspace.querySelector("[data-live-mode]");
  var liveSubtitle = workspace.querySelector("[data-live-subtitle]");
  var liveState = workspace.querySelector("[data-live-state]");
  var liveStatus = workspace.querySelector("[data-live-status]");
  var modeCard = workspace.querySelector("[data-mode-card]");
  var realOrdersCard = workspace.querySelector("[data-real-orders-card]");
  var killCard = workspace.querySelector("[data-kill-card]");
  var killBar = workspace.querySelector("[data-kill-bar]");
  var killReason = workspace.querySelector("[data-kill-reason]");
  var killClear = workspace.querySelector("[data-kill-clear]");
  var killForm = workspace.querySelector("[data-kill-form]");
  var killReasonInput = workspace.querySelector("[data-kill-reason-input]");
  var killStatus = workspace.querySelector("[data-kill-status]");
  var credentialStatus = workspace.querySelector("[data-credential-status]");
  var accountBadges = {
    kalshi: workspace.querySelector('[data-account-badge="kalshi"]'),
    polymarket: workspace.querySelector('[data-account-badge="polymarket"]')
  };
  var accountDetails = {
    kalshi: workspace.querySelector('[data-account-detail="kalshi"]'),
    polymarket: workspace.querySelector('[data-account-detail="polymarket"]')
  };

  function setStatus(node, text, isError) {
    node.textContent = text || "";
    node.classList.toggle("is-error", Boolean(isError));
  }

  async function callApi(path, options) {
    var response = await fetch(path, options || { headers: { "accept": "application/json" } });
    var payload = await response.json();
    if (!payload || payload.ok !== true) {
      var err = payload && payload.error ? payload.error : { code: "error", message: "operation failed" };
      throw err;
    }
    return payload.data;
  }

  function postJson(path, body) {
    return callApi(path, {
      method: ["P", "OST"].join(""),
      headers: { "accept": "application/json", "content-type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function renderCredentials(rows) {
    (rows || []).forEach(function (row) {
      var key = row.venue + "." + row.profile;
      var target = workspace.querySelector('[data-credential-row="' + key + '"]');
      if (!target) {
        return;
      }
      target.textContent = row.present
        ? "Present · " + (row.mode_600 ? "600" : "bad perms") + " · " + row.path
        : "Not present · " + row.path;
      target.classList.toggle("is-ok", Boolean(row.present && row.mode_600));
      target.classList.toggle("is-warn", Boolean(row.present && !row.mode_600));
    });
  }

  function renderLiveStatus(data) {
    var live = data.mode === "live" && data.real_orders_enabled === true;
    var kill = data.killswitch && data.killswitch.engaged === true;
    var accounts = data.accounts || {};

    liveHero.classList.toggle("is-live", live);
    liveHero.classList.toggle("is-kill", kill);
    liveMode.textContent = live ? "LIVE - REAL MONEY" : "PAPER";
    liveSubtitle.textContent = kill ? "Kill switch engaged" : live ? "Real orders enabled" : "Real orders disabled";
    liveState.textContent = kill ? "Stopped" : live ? "Live" : "Paper";
    modeCard.textContent = data.mode || "paper";
    realOrdersCard.textContent = data.real_orders_enabled ? "On" : "Off";
    killCard.textContent = kill ? "Engaged" : "Clear";

    killBar.hidden = !kill;
    if (kill) {
      killReason.textContent = data.killswitch.reason || "engaged";
      killClear.textContent = "Manual clear: rm " + data.killswitch.sentinel_path;
    }
    renderCredentials(data.credential_status || []);
    renderAccountBadges(accounts);
  }

  function renderAccountBadges(accounts) {
    ["kalshi", "polymarket"].forEach(function (venue) {
      var badge = accountBadges[venue];
      var detail = accountDetails[venue];
      if (!badge || !detail) {
        return;
      }
      var account = accounts[venue] || { connected: false, detail: "status unavailable" };
      var isConnected = account.connected === true;
      badge.textContent = isConnected ? "CONNECTED (READ-ONLY)" : "NOT CONNECTED";
      badge.classList.toggle("is-ok", isConnected);
      badge.classList.toggle("is-warn", !isConnected);
      detail.textContent = account.detail || "status unavailable";
    });
  }

  async function refreshLive() {
    try {
      var data = await callApi("/api/get_live_status");
      renderLiveStatus(data);
      setStatus(liveStatus, "", false);
    } catch (err) {
      setStatus(liveStatus, err.message || err.code, true);
    }
  }

  async function engageKill(event) {
    event.preventDefault();
    var reason = killReasonInput.value.trim();
    if (!reason) {
      setStatus(killStatus, "Reason required", true);
      return;
    }
    if (!window.confirm("Engage the global kill switch?")) {
      return;
    }
    setStatus(killStatus, "Engaging...", false);
    try {
      await postJson("/api/engage_killswitch", { reason: reason });
      killForm.reset();
      await refreshLive();
      setStatus(killStatus, "Engaged", false);
    } catch (err) {
      setStatus(killStatus, err.message || err.code, true);
    }
  }

  async function storeCredential(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var fields = {};
    Array.prototype.slice.call(form.elements).forEach(function (element) {
      if (element.name) {
        fields[element.name] = element.value;
      }
    });
    setStatus(credentialStatus, "Storing...", false);
    try {
      await postJson("/api/store_credentials", {
        venue: form.dataset.venue,
        profile: form.dataset.profile,
        fields: fields
      });
      form.reset();
      await refreshLive();
      setStatus(credentialStatus, "Stored", false);
    } catch (err) {
      setStatus(credentialStatus, err.message || err.code, true);
    }
  }

  killForm.addEventListener("submit", engageKill);
  Array.prototype.slice.call(workspace.querySelectorAll("[data-credential-form]")).forEach(function (form) {
    form.addEventListener("submit", storeCredential);
  });

  refreshLive();
  window.setInterval(refreshLive, 5000);
}());
