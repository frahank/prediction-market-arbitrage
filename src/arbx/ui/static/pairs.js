/*
 * SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
 * SPDX-License-Identifier: MIT
 */
(function () {
  var workspace = document.querySelector("[data-pairs-workspace]");
  if (!workspace) {
    return;
  }

  var pairList = workspace.querySelector("[data-pair-list]");
  var statusNode = workspace.querySelector("[data-pairs-status]");
  var tabButtons = Array.prototype.slice.call(workspace.querySelectorAll("[data-pair-tab]"));
  var refreshButton = workspace.querySelector("[data-refresh-pairs]");
  var loadMoreButton = workspace.querySelector("[data-load-more-pairs]");
  var listKicker = workspace.querySelector("[data-pair-list-kicker]");
  var listTitle = workspace.querySelector("[data-pair-list-title]");
  var detailKicker = workspace.querySelector("[data-pair-detail-kicker]");
  var detailTitle = workspace.querySelector("[data-pair-detail-title]");
  var detailBody = workspace.querySelector("[data-pair-detail]");
  var reviewPanel = workspace.querySelector("[data-pair-review]");
  var reviewForm = workspace.querySelector("[data-review-form]");
  var reviewTitle = workspace.querySelector("[data-review-title]");
  var reviewPairKey = workspace.querySelector("[data-review-pair-key]");
  var reviewDecisionValue = workspace.querySelector("[data-review-decision-value]");
  var reviewConsequence = workspace.querySelector("[data-review-consequence]");
  var reviewActionStep = workspace.querySelector("[data-review-action-step]");
  var reviewConfirmStep = workspace.querySelector("[data-review-confirm-step]");
  var reviewActionButton = workspace.querySelector("[data-review-action]");
  var areYouSure = workspace.querySelector("[data-are-you-sure]");
  var confirmCancelButton = workspace.querySelector("[data-confirm-cancel]");
  var closeReviewButton = workspace.querySelector("[data-close-review]");

  var REVIEWER_KEY = "arbx_reviewer";
  var currentTab = "active";
  var cursor = null;
  var selectedPairKey = null;

  function rememberedReviewer() {
    try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch (e) { return ""; }
  }
  function saveReviewer(value) {
    try { localStorage.setItem(REVIEWER_KEY, value); } catch (e) { /* ignore */ }
  }

  function setStatus(text, isError) {
    statusNode.textContent = text || "";
    statusNode.classList.toggle("is-error", Boolean(isError));
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

  function chip(text, className) {
    var node = document.createElement("span");
    node.className = "data-chip " + (className || "");
    node.textContent = text || "unknown";
    return node;
  }

  function equivalenceClass(status) {
    if (status === "verified_equivalent" || status === "tail_divergence_documented") {
      return "is-pass";
    }
    if (status === "basis") {
      return "is-fail";
    }
    return "is-warn";
  }

  function latestDecision(pair) {
    return pair.latest_decision && pair.latest_decision.decision ? pair.latest_decision.decision : "no decision";
  }

  function compactPairMeta(pair) {
    return [
      pair.resolution_structure || "unknown",
      pair.grouping_alignment || "n/a",
      latestDecision(pair)
    ].join(" · ");
  }

  function decisionLabel(decision) {
    if (decision === "approve") { return "Approve"; }
    if (decision === "reject") { return "Reject"; }
    return decision.charAt(0).toUpperCase() + decision.slice(1);
  }

  function decisionConsequence(decision) {
    if (decision === "approve") {
      return "Approving moves this pair into the approved registry.";
    }
    if (decision === "reject") {
      return "Rejecting moves this pair to the Archived tab.";
    }
    return "";
  }

  function approvable(pair) {
    var status = (pair.equivalence || {}).status;
    return status === "verified_equivalent" || status === "tail_divergence_documented";
  }

  function actionButton(label, decision, pair, disabledReason) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "pair-action-button is-" + decision;
    button.textContent = label;
    if (disabledReason) {
      button.disabled = true;
      button.title = disabledReason;
    }
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      openReview(pair, decision);
    });
    return button;
  }

  function renderPairRow(pair) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "pair-row";
    row.dataset.pairKey = pair.pair_key;
    row.addEventListener("click", function () {
      loadSummary(pair.pair_key);
    });

    var main = document.createElement("span");
    main.className = "pair-row-main";
    var title = document.createElement("strong");
    title.textContent = pair.display_name || pair.pair_key;
    var key = document.createElement("small");
    key.textContent = pair.pair_key;
    main.appendChild(title);
    main.appendChild(key);

    var chips = document.createElement("span");
    chips.className = "data-chip-row";
    chips.appendChild(chip((pair.equivalence || {}).status, equivalenceClass((pair.equivalence || {}).status)));
    chips.appendChild(chip(pair.include_in_strategy_metrics ? "strategy" : "excluded", pair.include_in_strategy_metrics ? "is-edge" : "is-warn"));
    chips.appendChild(chip(pair.status || "registry"));

    var meta = document.createElement("span");
    meta.className = "pair-row-meta";
    meta.textContent = compactPairMeta(pair);

    row.appendChild(main);
    row.appendChild(chips);
    row.appendChild(meta);

    if (currentTab === "approval") {
      var actions = document.createElement("span");
      actions.className = "pair-actions";
      var approveDisabled = approvable(pair) ? null : "Equivalence must be verified before this pair can be approved";
      actions.appendChild(actionButton("Approve", "approve", pair, approveDisabled));
      actions.appendChild(actionButton("Reject", "reject", pair, null));
      row.appendChild(actions);
    }

    return row;
  }

  function renderPairRows(items, append) {
    if (!append) {
      pairList.textContent = "";
    }
    if (!items.length && !append) {
      var empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No pairs found.";
      pairList.appendChild(empty);
      return;
    }
    items.forEach(function (pair) {
      pairList.appendChild(renderPairRow(pair));
    });
  }

  function tabOp(tab) {
    if (tab === "active") { return "list_active_pairs"; }
    if (tab === "archived") { return "list_archived_pairs"; }
    return "list_pairs_needing_approval";
  }

  async function loadPairs(append) {
    setStatus("Loading pairs...", false);
    try {
      var path = "/api/" + tabOp(currentTab) + "?limit=50";
      if (append && cursor) {
        path += "&cursor=" + encodeURIComponent(cursor);
      }
      var data = await callApi(path);
      renderPairRows(data.items || [], append);
      cursor = data.next_cursor || null;
      loadMoreButton.disabled = !cursor;
      setStatus("", false);
    } catch (err) {
      setStatus(err.message || err.code, true);
    }
  }

  function setTab(tab) {
    currentTab = tab;
    cursor = null;
    selectedPairKey = null;
    hideReview();
    detailKicker.textContent = "Pair summary";
    detailTitle.textContent = "Select a pair";
    detailBody.innerHTML = '<p class="empty-state">Select a pair to view the standardized summary.</p>';
    tabButtons.forEach(function (button) {
      button.classList.toggle("active", button.dataset.pairTab === tab);
      button.setAttribute("aria-selected", button.dataset.pairTab === tab ? "true" : "false");
    });
    listKicker.textContent = tab === "active" ? "Approved" : tab === "archived" ? "Archived" : "Needs approval";
    listTitle.textContent = tab === "active" ? "Approved registry" : tab === "archived" ? "Archived pairs" : "Approval queue";
    loadPairs(false);
  }

  function kv(label, value) {
    var fragment = document.createDocumentFragment();
    var dt = document.createElement("dt");
    var dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value == null || value === "" ? "-" : String(value);
    fragment.appendChild(dt);
    fragment.appendChild(dd);
    return fragment;
  }

  function detailSection(titleText, child) {
    var section = document.createElement("section");
    section.className = "pair-detail-section";
    var title = document.createElement("h3");
    title.textContent = titleText;
    section.appendChild(title);
    section.appendChild(child);
    return section;
  }

  function renderList(items) {
    var list = document.createElement("ul");
    list.className = "plain-list";
    if (!items || !items.length) {
      var empty = document.createElement("li");
      empty.textContent = "-";
      list.appendChild(empty);
      return list;
    }
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    return list;
  }

  function renderJson(value) {
    var pre = document.createElement("pre");
    pre.className = "json-block";
    pre.textContent = JSON.stringify(value || {}, null, 2);
    return pre;
  }

  function renderEvidenceLink(link) {
    var li = document.createElement("li");
    if (/^https?:\/\//.test(link)) {
      var external = document.createElement("a");
      external.href = link;
      external.target = "_blank";
      external.rel = "noreferrer";
      external.textContent = link;
      li.appendChild(external);
      return li;
    }
    if (/\.md$/i.test(link)) {
      var doc = document.createElement("a");
      doc.href = "/docs-viewer?path=" + encodeURIComponent(link);
      doc.textContent = link;
      li.appendChild(doc);
      return li;
    }
    li.textContent = link;
    return li;
  }

  function detailActions(pair) {
    // Approve/Reject straight from the detail panel, available while the pair is
    // still in the review queue (not yet approved and not archived).
    var status = pair.status || "";
    if (status === "archived" || status === "approved_for_paper") {
      return null;
    }
    var bar = document.createElement("div");
    bar.className = "editor-actions pair-detail-actions";
    var approveDisabled = approvable(pair) ? null : "Equivalence must be verified before this pair can be approved";
    bar.appendChild(actionButton("Approve", "approve", pair, approveDisabled));
    bar.appendChild(actionButton("Reject", "reject", pair, null));
    return bar;
  }

  function renderSummary(pair) {
    selectedPairKey = pair.pair_key;
    detailKicker.textContent = pair.kalshi_market_id || "Pair summary";
    detailTitle.textContent = pair.display_name || pair.pair_key;
    detailBody.textContent = "";

    var chips = document.createElement("div");
    chips.className = "data-chip-row pair-summary-chips";
    chips.appendChild(chip((pair.equivalence || {}).status, equivalenceClass((pair.equivalence || {}).status)));
    chips.appendChild(chip(pair.include_in_strategy_metrics ? "strategy metrics" : "strategy excluded", pair.include_in_strategy_metrics ? "is-edge" : "is-warn"));
    chips.appendChild(chip(pair.simulation_scope || "scope unknown"));
    detailBody.appendChild(chips);

    var actions = detailActions(pair);
    if (actions) {
      detailBody.appendChild(actions);
    }

    var taxonomy = document.createElement("dl");
    taxonomy.className = "detail-grid";
    taxonomy.appendChild(kv("Resolution structure", pair.resolution_structure));
    taxonomy.appendChild(kv("Grouping alignment", pair.grouping_alignment));
    taxonomy.appendChild(kv("Cutoff delta hours", pair.date_cutoff_delta_hours));
    taxonomy.appendChild(kv("Time to resolution days", pair.time_to_resolution_days));
    taxonomy.appendChild(kv("Persistence cause", pair.persistence_cause));
    taxonomy.appendChild(kv("Latest decision", latestDecision(pair)));
    detailBody.appendChild(detailSection("Registry taxonomy", taxonomy));

    var equivalence = document.createElement("dl");
    equivalence.className = "detail-grid";
    equivalence.appendChild(kv("Audited at", (pair.equivalence || {}).audited_at));
    equivalence.appendChild(kv("Auditor", (pair.equivalence || {}).auditor));
    equivalence.appendChild(kv("Notes", (pair.equivalence || {}).notes));
    detailBody.appendChild(detailSection("Equivalence", equivalence));
    detailBody.appendChild(detailSection("Tail risks", renderList((pair.equivalence || {}).tail_risks || [])));
    detailBody.appendChild(detailSection("Orientation", renderJson(pair.orientation_confirmed || {})));
    detailBody.appendChild(detailSection("Liquidity", pair.liquidity ? renderJson(pair.liquidity) : renderList([])));
    detailBody.appendChild(detailSection("Edge behavior", pair.edge_behavior ? renderJson(pair.edge_behavior) : renderList([])));

    var evidence = document.createElement("ul");
    evidence.className = "evidence-list";
    (pair.evidence_links || []).forEach(function (link) {
      evidence.appendChild(renderEvidenceLink(link));
    });
    if (!evidence.children.length) {
      evidence.appendChild(renderList([]).firstChild);
    }
    detailBody.appendChild(detailSection("Evidence links", evidence));
  }

  async function loadSummary(pairKey) {
    setStatus("Loading summary...", false);
    try {
      var pair = await callApi("/api/get_pair_summary?pair_key=" + encodeURIComponent(pairKey));
      renderSummary(pair);
      setStatus("", false);
    } catch (err) {
      setStatus(err.message || err.code, true);
    }
  }

  function showActionStep() {
    reviewActionStep.hidden = false;
    reviewConfirmStep.hidden = true;
  }
  function showConfirmStep() {
    reviewActionStep.hidden = true;
    reviewConfirmStep.hidden = false;
  }

  function openReview(pair, decision) {
    reviewPanel.hidden = false;
    reviewTitle.textContent = decisionLabel(decision) + ": " + (pair.display_name || pair.pair_key);
    reviewPairKey.value = pair.pair_key;
    reviewDecisionValue.value = decision;
    reviewConsequence.textContent = decisionConsequence(decision);
    reviewForm.elements.reviewer.value = rememberedReviewer();
    reviewForm.elements.notes.value = "";
    reviewActionButton.textContent = decisionLabel(decision);
    reviewActionButton.className = "pair-action-button is-" + decision;
    areYouSure.textContent = "Are you sure you want to " + decision + " this pair?";
    showActionStep();
    reviewForm.elements.reviewer.focus();
  }

  function hideReview() {
    reviewPanel.hidden = true;
    reviewForm.reset();
    reviewConsequence.textContent = "";
    showActionStep();
  }

  async function submitReview(event) {
    event.preventDefault();
    var decision = reviewDecisionValue.value;
    var pairKey = reviewPairKey.value;
    var reviewer = (reviewForm.elements.reviewer.value || "").trim();
    if (!reviewer) {
      setStatus("Enter a reviewer name first", true);
      showActionStep();
      reviewForm.elements.reviewer.focus();
      return;
    }
    var defaultNotes = decision === "approve" ? "Approved via UI" : "Rejected via UI";
    var notes = (reviewForm.elements.notes.value || "").trim() || (defaultNotes + " by " + reviewer);
    var confirmPhrase = decision.toUpperCase() + " " + pairKey;
    setStatus("Recording decision...", false);
    try {
      var result = await callApi("/api/review_pair", {
        method: "POST",
        headers: { "accept": "application/json", "content-type": "application/json" },
        body: JSON.stringify({
          pair_key: pairKey,
          decision: decision,
          reviewer: reviewer,
          notes: notes,
          confirm: confirmPhrase
        })
      });
      saveReviewer(reviewer);
      hideReview();
      await loadPairs(false);
      if (selectedPairKey === pairKey) {
        loadSummary(pairKey).catch(function () { /* moved out of view */ });
      }
      var dest = result && result.status === "archived" ? "Archived" : "Approved";
      setStatus("Decision recorded — pair moved to " + dest, false);
    } catch (err) {
      setStatus(err.message || err.code, true);
      showActionStep();
    }
  }

  tabButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      setTab(button.dataset.pairTab);
    });
  });
  refreshButton.addEventListener("click", function () {
    loadPairs(false);
  });
  loadMoreButton.addEventListener("click", function () {
    loadPairs(true);
  });
  closeReviewButton.addEventListener("click", hideReview);
  reviewActionButton.addEventListener("click", function () {
    if (!(reviewForm.elements.reviewer.value || "").trim()) {
      setStatus("Enter a reviewer name first", true);
      reviewForm.elements.reviewer.focus();
      return;
    }
    showConfirmStep();
  });
  confirmCancelButton.addEventListener("click", showActionStep);
  reviewForm.addEventListener("submit", submitReview);

  setTab("active");
}());
