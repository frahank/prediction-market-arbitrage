(function () {
  var workspace = document.querySelector("[data-paper-workspace]");
  if (!workspace) {
    return;
  }

  var scannerForm = workspace.querySelector("[data-scanner-form]");
  var scannerEnabled = workspace.querySelector("[data-scanner-enabled]");
  var recordToggle = workspace.querySelector("[data-record-toggle]");
  var edgesOnlyToggle = workspace.querySelector("[data-edges-only-toggle]");
  var pairSelect = workspace.querySelector("[data-pair-select]");
  var pairScopeNote = workspace.querySelector("[data-pair-scope-note]");
  var batchInput = workspace.querySelector("[data-batch-size]");
  var tickInput = workspace.querySelector("[data-tick-s]");
  var stopScannerButton = workspace.querySelector("[data-stop-scanner]");
  var scannerStatus = workspace.querySelector("[data-scanner-status]");
  var scannerState = workspace.querySelector("[data-scanner-state]");
  var cadenceDisplay = workspace.querySelector("[data-cadence-display]");
  var scannerTicks = workspace.querySelector("[data-scanner-ticks]");
  var scannerArbs = workspace.querySelector("[data-scanner-arbs]");
  var scannerQualifying = workspace.querySelector("[data-scanner-qualifying]");
  var scannerErrors = workspace.querySelector("[data-scanner-errors]");
  var startedScan = workspace.querySelector("[data-started-scan]");
  var startedScanLink = workspace.querySelector("[data-started-scan-link]");

  var analysisForm = workspace.querySelector("[data-analysis-form]");
  var analysisSoaks = workspace.querySelector("[data-analysis-soaks]");
  var analysisStatus = workspace.querySelector("[data-analysis-status]");
  var analysisState = workspace.querySelector("[data-analysis-state]");
  var analysisSummary = workspace.querySelector("[data-analysis-summary]");
  var refreshAnalysisData = workspace.querySelector("[data-refresh-analysis-data]");

  var runTestsButton = workspace.querySelector("[data-run-tests]");
  var testStatus = workspace.querySelector("[data-test-status]");
  var testState = workspace.querySelector("[data-test-state]");
  var testSummary = workspace.querySelector("[data-test-summary]");
  var testDetail = workspace.querySelector("[data-test-detail]");

  var activePairs = [];
  var scannerTimer = null;
  var analysisTimer = null;
  var testTimer = null;

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
      method: "POST",
      headers: { "accept": "application/json", "content-type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function selectedOptions(select) {
    return Array.prototype.slice.call(select.selectedOptions).map(function (option) {
      return option.value;
    });
  }

  function selectedPairKeys() {
    var values = selectedOptions(pairSelect);
    if (!values.length || values.indexOf("__all__") !== -1) {
      return null;
    }
    return values;
  }

  function selectedPairCount() {
    var selected = selectedPairKeys();
    return selected ? selected.length : activePairs.length;
  }

  function effectiveCadence(pairCount, batchSize, tickS) {
    if (!pairCount || !batchSize || !tickS) {
      return 0;
    }
    return Math.ceil(pairCount / batchSize) * tickS;
  }

  function updateCadence() {
    var count = selectedPairCount();
    var batch = Math.max(1, Number(batchInput.value) || Number(workspace.dataset.defaultBatch) || 20);
    var tick = Math.max(0.001, Number(tickInput.value) || Number(workspace.dataset.defaultTick) || 1);
    var cadence = effectiveCadence(count, batch, tick);
    cadenceDisplay.textContent = count + " pairs -> " + cadence.toFixed(3).replace(/\.?0+$/, "") + " s";
  }

  function syncEdgesOnly() {
    if (edgesOnlyToggle.checked) {
      recordToggle.checked = true;
      recordToggle.disabled = true;
      edgesOnlyToggle.disabled = false;
    } else {
      recordToggle.disabled = false;
      edgesOnlyToggle.disabled = !recordToggle.checked;
    }
  }

  function renderPairs(items) {
    var all = pairSelect.querySelector('option[value="__all__"]');
    pairSelect.textContent = "";
    pairSelect.appendChild(all || new Option("All approved", "__all__", true, true));
    activePairs = items || [];
    activePairs.forEach(function (pair) {
      var label = pair.display_name || pair.pair_key;
      var option = new Option(label, pair.pair_key, false, false);
      option.dataset.pairKey = pair.pair_key;
      pairSelect.appendChild(option);
    });
    renderPairScopeNote(activePairs);
    updateCadence();
  }

  // Scanning a pair and counting it toward strategy metrics are separate
  // decisions, and the shipped registry is fully scannable with nothing
  // strategy-eligible. Say so here rather than letting the numbers surprise
  // someone reading a results page later.
  function renderPairScopeNote(pairs) {
    if (!pairScopeNote) {
      return;
    }
    var total = pairs.length;
    var strategy = pairs.filter(function (pair) {
      return pair.include_in_strategy_metrics;
    }).length;
    if (!total) {
      pairScopeNote.hidden = false;
      pairScopeNote.textContent = "No scannable pairs in the registry.";
      return;
    }
    var note = total + " scannable \u00b7 " + strategy + " count toward strategy metrics";
    if (!strategy) {
      note += " \u2014 every registry pair was rejected as a strategy candidate, so scans record data but produce no strategy result";
    }
    pairScopeNote.hidden = false;
    pairScopeNote.textContent = note;
  }

  async function loadPairs() {
    try {
      var data = await callApi("/api/list_active_pairs?limit=100");
      renderPairs(data.items || []);
    } catch (err) {
      setStatus(scannerStatus, err.message || err.code, true);
    }
  }

  function scannerPayload() {
    var edgesOnly = edgesOnlyToggle.checked;
    var payload = {
      record: edgesOnly ? true : recordToggle.checked,
      edges_only: edgesOnly,
      batch_size: Math.max(1, Number(batchInput.value) || Number(workspace.dataset.defaultBatch) || 20),
      tick_s: Math.max(0.001, Number(tickInput.value) || Number(workspace.dataset.defaultTick) || 1)
    };
    var pairKeys = selectedPairKeys();
    if (pairKeys) {
      payload.pair_keys = pairKeys;
    }
    return payload;
  }

  function renderScannerStatus(status) {
    scannerEnabled.checked = Boolean(status.running);
    stopScannerButton.disabled = !status.running;
    scannerState.textContent = status.state || (status.running ? "Running" : "Idle");
    scannerTicks.textContent = String(status.ticks || 0);
    scannerArbs.textContent = String(status.arbs_detected || 0);
    scannerQualifying.textContent = String(status.qualifying || 0);
    scannerErrors.textContent = String(status.fetch_errors || 0);
    if (status.pair_count) {
      cadenceDisplay.textContent = status.pair_count + " pairs -> " + Number(status.effective_cadence_s || 0).toFixed(3).replace(/\.?0+$/, "") + " s";
    }
    if (status.soak_id) {
      startedScan.hidden = false;
      startedScanLink.href = "/data?soak_id=" + encodeURIComponent(status.soak_id);
      startedScanLink.textContent = status.soak_id;
    }
  }

  async function refreshScannerStatus() {
    try {
      var status = await callApi("/api/get_scanner_status");
      renderScannerStatus(status);
      setStatus(scannerStatus, "", false);
      if (status.running && !scannerTimer) {
        scannerTimer = window.setInterval(refreshScannerStatus, 2000);
      }
      if (!status.running && scannerTimer) {
        window.clearInterval(scannerTimer);
        scannerTimer = null;
      }
    } catch (err) {
      setStatus(scannerStatus, err.message || err.code, true);
    }
  }

  async function startScanner(event) {
    if (event) {
      event.preventDefault();
    }
    syncEdgesOnly();
    setStatus(scannerStatus, "Starting...", false);
    try {
      var result = await postJson("/api/start_scanner", scannerPayload());
      scannerEnabled.checked = true;
      stopScannerButton.disabled = false;
      if (result.soak_id) {
        startedScan.hidden = false;
        startedScanLink.href = "/data?soak_id=" + encodeURIComponent(result.soak_id);
        startedScanLink.textContent = result.soak_id;
      }
      await refreshScannerStatus();
      setStatus(scannerStatus, "", false);
    } catch (err) {
      scannerEnabled.checked = false;
      setStatus(scannerStatus, err.message || err.code, true);
    }
  }

  async function stopScanner() {
    setStatus(scannerStatus, "Stopping...", false);
    try {
      await postJson("/api/stop_scanner", {});
      await refreshScannerStatus();
      setStatus(scannerStatus, "", false);
    } catch (err) {
      setStatus(scannerStatus, err.message || err.code, true);
    }
  }

  function renderSoakOptions(items) {
    analysisSoaks.textContent = "";
    (items || []).forEach(function (soak) {
      var label = (soak.label || soak.soak_id) + " · edges " + ((soak.row_counts || {}).edges || 0);
      analysisSoaks.appendChild(new Option(label, soak.soak_id, false, false));
    });
  }

  async function loadAnalysisData() {
    setStatus(analysisStatus, "Loading...", false);
    try {
      var data = await callApi("/api/list_soaks?limit=100");
      renderSoakOptions(data.items || []);
      setStatus(analysisStatus, "", false);
    } catch (err) {
      setStatus(analysisStatus, err.message || err.code, true);
    }
  }

  function chip(text, className) {
    var node = document.createElement("span");
    node.className = "data-chip " + (className || "");
    node.textContent = text || "-";
    return node;
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

  function renderCaveats(caveats) {
    var list = document.createElement("ul");
    list.className = "plain-list";
    (caveats || []).forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    if (!list.children.length) {
      var empty = document.createElement("li");
      empty.textContent = "-";
      list.appendChild(empty);
    }
    return list;
  }

  function graphFallback(graph) {
    var pre = document.createElement("pre");
    pre.className = "json-block";
    pre.textContent = JSON.stringify(graph, null, 2);
    return pre;
  }

  function renderGraph(graph) {
    var series = graph && graph.payload && graph.payload.series;
    if (graph.kind !== "edge_timeline_v1" || !series) {
      return graphFallback(graph);
    }
    var wrap = document.createElement("div");
    wrap.className = "paper-graph";
    Object.keys(series).slice(0, 6).forEach(function (pairKey) {
      var values = (series[pairKey] || []).map(function (point) {
        if (Array.isArray(point)) {
          return Number(point[1]);
        }
        return Number(point && point.fee_adj_edge);
      }).filter(function (value) {
        return isFinite(value);
      });
      if (!values.length) {
        return;
      }
      var min = Math.min.apply(null, values);
      var max = Math.max.apply(null, values);
      var span = max - min || 1;
      var points = values.map(function (value, index) {
        var x = values.length === 1 ? 50 : (index / (values.length - 1)) * 100;
        var y = 30 - ((value - min) / span) * 24;
        return x.toFixed(2) + "," + y.toFixed(2);
      }).join(" ");
      var row = document.createElement("div");
      row.className = "paper-graph-series";
      var label = document.createElement("span");
      label.textContent = pairKey;
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 100 36");
      svg.setAttribute("preserveAspectRatio", "none");
      var baseline = document.createElementNS("http://www.w3.org/2000/svg", "line");
      baseline.setAttribute("x1", "0");
      baseline.setAttribute("x2", "100");
      baseline.setAttribute("y1", "30");
      baseline.setAttribute("y2", "30");
      var line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
      line.setAttribute("points", points);
      svg.appendChild(baseline);
      svg.appendChild(line);
      row.appendChild(label);
      row.appendChild(svg);
      wrap.appendChild(row);
    });
    return wrap.children.length ? wrap : graphFallback(graph);
  }

  function renderAnalysisSummary(summary) {
    analysisSummary.textContent = "";
    var chips = document.createElement("div");
    chips.className = "data-chip-row";
    var verdict = (summary.would_have_made_money_live || {}).verdict || "unknown";
    chips.appendChild(chip(verdict, verdict === "viable" ? "is-pass" : verdict === "not_viable" ? "is-fail" : "is-warn"));
    chips.appendChild(chip((summary.dq || {}).passed ? "DQ pass" : "DQ check", (summary.dq || {}).passed ? "is-pass" : "is-warn"));
    analysisSummary.appendChild(chips);

    var grid = document.createElement("dl");
    grid.className = "detail-grid";
    grid.appendChild(kv("Candidate score", Number(summary.profit_score || 0).toFixed(4)));
    grid.appendChild(kv("Min latency ms", summary.min_latency_needed_ms));
    grid.appendChild(kv("Chance profit", Number(summary.chance_of_profit || 0).toFixed(4)));
    grid.appendChild(kv("Chance loss", Number(summary.chance_of_loss || 0).toFixed(4)));
    grid.appendChild(kv("Basis", (summary.would_have_made_money_live || {}).basis));
    analysisSummary.appendChild(grid);

    var rationale = document.createElement("p");
    rationale.className = "honesty-caption";
    rationale.textContent = ((summary.would_have_made_money_live || {}).rationale || []).join(" ");
    analysisSummary.appendChild(rationale);
    analysisSummary.appendChild(renderCaveats(summary.caveats));
    if (summary.graph) {
      analysisSummary.appendChild(renderGraph(summary.graph));
    }
  }

  async function pollAnalysis(jobId) {
    try {
      var status = await callApi("/api/get_analysis_status?job_id=" + encodeURIComponent(jobId));
      analysisState.textContent = status.state || "Running";
      if (status.progress) {
        setStatus(analysisStatus, status.progress.stage + " " + status.progress.pct + "%", false);
      }
      if (status.state === "done") {
        window.clearInterval(analysisTimer);
        analysisTimer = null;
        renderAnalysisSummary(status.summary || {});
        setStatus(analysisStatus, "", false);
        return true;
      } else if (status.state === "failed") {
        window.clearInterval(analysisTimer);
        analysisTimer = null;
        setStatus(analysisStatus, status.error || "analysis failed", true);
        return true;
      }
    } catch (err) {
      setStatus(analysisStatus, err.message || err.code, true);
    }
    return false;
  }

  async function runAnalysis(event) {
    event.preventDefault();
    var ids = selectedOptions(analysisSoaks);
    if (!ids.length) {
      setStatus(analysisStatus, "Select recorded data", true);
      return;
    }
    setStatus(analysisStatus, "Starting...", false);
    try {
      var result = await postJson("/api/run_full_analysis", { soak_ids: ids });
      var terminal = await pollAnalysis(result.job_id);
      if (!terminal && !analysisTimer) {
        analysisTimer = window.setInterval(function () {
          pollAnalysis(result.job_id);
        }, 2000);
      }
    } catch (err) {
      setStatus(analysisStatus, err.message || err.code, true);
    }
  }

  function renderTestResult(status) {
    var result = status.result || {};
    testState.textContent = status.state || "Running";
    testSummary.textContent = "";
    var line = document.createElement("p");
    line.className = result.passed ? "positive" : "reader-status is-error";
    line.textContent = result.message || status.error || "Test suite running";
    testSummary.appendChild(line);
    if (result.detail_path) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "pair-action-button";
      button.textContent = "Open detail";
      button.addEventListener("click", function () {
        loadTestDetail(result.detail_path);
      });
      testSummary.appendChild(button);
    }
  }

  async function pollTests(jobId) {
    try {
      var status = await callApi("/api/get_test_suite_result?job_id=" + encodeURIComponent(jobId));
      renderTestResult(status);
      if (status.state === "done" || status.state === "failed") {
        window.clearInterval(testTimer);
        testTimer = null;
        setStatus(testStatus, "", false);
        return true;
      }
    } catch (err) {
      setStatus(testStatus, err.message || err.code, true);
    }
    return false;
  }

  async function runTests() {
    setStatus(testStatus, "Starting...", false);
    testDetail.hidden = true;
    try {
      var result = await postJson("/api/run_test_suite", {});
      var terminal = await pollTests(result.job_id);
      if (!terminal && !testTimer) {
        testTimer = window.setInterval(function () {
          pollTests(result.job_id);
        }, 2000);
      }
    } catch (err) {
      setStatus(testStatus, err.message || err.code, true);
    }
  }

  async function loadTestDetail(path) {
    try {
      var detail = await callApi("/api/get_test_run_detail?path=" + encodeURIComponent(path));
      testDetail.textContent = detail.text || "";
      testDetail.hidden = false;
    } catch (err) {
      setStatus(testStatus, err.message || err.code, true);
    }
  }

  pairSelect.addEventListener("change", updateCadence);
  batchInput.addEventListener("input", updateCadence);
  tickInput.addEventListener("input", updateCadence);
  recordToggle.addEventListener("change", syncEdgesOnly);
  edgesOnlyToggle.addEventListener("change", function () {
    syncEdgesOnly();
    updateCadence();
  });
  scannerForm.addEventListener("submit", startScanner);
  stopScannerButton.addEventListener("click", stopScanner);
  scannerEnabled.addEventListener("change", function () {
    if (scannerEnabled.checked) {
      startScanner();
    } else if (!stopScannerButton.disabled) {
      stopScanner();
    }
  });
  refreshAnalysisData.addEventListener("click", loadAnalysisData);
  analysisForm.addEventListener("submit", runAnalysis);
  runTestsButton.addEventListener("click", runTests);

  syncEdgesOnly();
  loadPairs();
  loadAnalysisData();
  refreshScannerStatus();
}());
