/*
 * SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
 * SPDX-License-Identifier: MIT
 */
(function () {
  var workspace = document.querySelector("[data-data-workspace]");
  if (!workspace) {
    return;
  }

  var soakList = workspace.querySelector("[data-soak-list]");
  var statusNode = workspace.querySelector("[data-data-status]");
  var edgeOnlyFilter = workspace.querySelector("[data-edge-only-filter]");
  var refreshButton = workspace.querySelector("[data-refresh-soaks]");
  var selectedSoakNode = workspace.querySelector("[data-selected-soak]");
  var rowTitle = workspace.querySelector("[data-row-title]");
  var rowKindButtons = Array.prototype.slice.call(workspace.querySelectorAll("[data-row-kind]"));
  var rowTable = workspace.querySelector("[data-row-table]");
  var loadMoreButton = workspace.querySelector("[data-load-more]");

  var selectedSoak = null;
  var rowKind = "edges";
  var rowCursor = null;

  function setStatus(text, isError) {
    statusNode.textContent = text || "";
    statusNode.classList.toggle("is-error", Boolean(isError));
  }

  async function callApi(path) {
    var response = await fetch(path, { headers: { "accept": "application/json" } });
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
    node.textContent = text;
    return node;
  }

  function formatCount(value) {
    return String(Number(value || 0).toLocaleString());
  }

  function renderSoaks(items) {
    soakList.textContent = "";
    if (!items.length) {
      var empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No soaks found.";
      soakList.appendChild(empty);
      return;
    }
    items.forEach(function (soak) {
      var card = document.createElement("button");
      card.type = "button";
      card.className = "soak-card";
      card.addEventListener("click", function () {
        selectSoak(soak);
      });

      var title = document.createElement("strong");
      title.textContent = soak.label || soak.soak_id;
      var meta = document.createElement("small");
      meta.textContent = (soak.started_at || "unknown time") + " · " + soak.pair_count + " pairs";
      var counts = document.createElement("span");
      counts.className = "soak-counts";
      counts.textContent = "books " + formatCount(soak.row_counts.book) + " · edges " + formatCount(soak.row_counts.edges) + " · opps " + formatCount(soak.row_counts.opportunities);
      var chips = document.createElement("span");
      chips.className = "data-chip-row";
      chips.appendChild(chip(soak.dq_status, soak.dq_status === "pass" ? "is-pass" : soak.dq_status === "fail" ? "is-fail" : ""));
      if (soak.edges_only) {
        chips.appendChild(chip("EDGES", "is-edge"));
      }
      if (soak.legacy_book_fix_applied) {
        chips.appendChild(chip("legacy corrected", "is-warn"));
      }
      card.appendChild(title);
      card.appendChild(meta);
      card.appendChild(counts);
      card.appendChild(chips);
      soakList.appendChild(card);
    });
  }

  async function loadSoaks() {
    setStatus("Loading soaks...", false);
    try {
      var path = "/api/list_soaks?limit=50";
      if (edgeOnlyFilter.checked) {
        path += "&edges_only=true";
      }
      var data = await callApi(path);
      renderSoaks(data.items || []);
      setStatus("", false);
    } catch (err) {
      setStatus(err.message || err.code, true);
    }
  }

  function selectSoak(soak) {
    selectedSoak = soak;
    rowCursor = null;
    selectedSoakNode.textContent = soak.soak_id;
    rowTitle.textContent = soak.label || soak.soak_id;
    loadRows(false);
  }

  function setRowKind(kind) {
    rowKind = kind;
    rowCursor = null;
    rowKindButtons.forEach(function (button) {
      button.classList.toggle("active", button.dataset.rowKind === kind);
    });
    if (selectedSoak) {
      loadRows(false);
    }
  }

  function cell(text, className) {
    var td = document.createElement("td");
    if (className) {
      td.className = className;
    }
    td.textContent = text == null ? "" : String(text);
    return td;
  }

  function renderHead(columns) {
    var tr = document.createElement("tr");
    columns.forEach(function (name) {
      var th = document.createElement("th");
      th.textContent = name;
      tr.appendChild(th);
    });
    rowTable.tHead.textContent = "";
    rowTable.tHead.appendChild(tr);
  }

  function renderEdgeRow(row) {
    var tr = document.createElement("tr");
    if (Number(row.est_profit) > 0) {
      tr.className = "candidate-row";
    }
    tr.appendChild(cell(row.pair_key));
    tr.appendChild(cell(row.direction));
    tr.appendChild(cell(Number(row.est_profit || 0).toFixed(4), Number(row.est_profit) > 0 ? "positive" : ""));
    tr.appendChild(cell(Number(row.fee_adj_edge || 0).toFixed(4)));
    tr.appendChild(cell(Number(row.executable_size || 0).toFixed(2)));
    tr.appendChild(cell(row.freshness_status));
    tr.appendChild(cell(row.qualifies ? "qualifies" : row.arb_detected ? "candidate" : "observed"));
    return tr;
  }

  function renderBookRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(cell(row.display_name));
    tr.appendChild(cell(row.captured_at));
    tr.appendChild(cell(Number(row.round_trip_duration_ms || 0).toFixed(1)));
    tr.appendChild(cell(row.freshness_status));
    tr.appendChild(cell((row.dq_flags || []).join(", ")));
    return tr;
  }

  function renderRows(items, append) {
    if (!append) {
      rowTable.tBodies[0].textContent = "";
      if (rowKind === "edges") {
        renderHead(["Pair", "Direction", "Est profit", "Fee edge", "Exec size", "Freshness", "Status"]);
      } else {
        renderHead(["Market", "Captured", "RTT ms", "Freshness", "DQ flags"]);
      }
    }
    items.forEach(function (row) {
      rowTable.tBodies[0].appendChild(rowKind === "edges" ? renderEdgeRow(row) : renderBookRow(row));
    });
  }

  async function loadRows(append) {
    if (!selectedSoak) {
      return;
    }
    setStatus("Loading rows...", false);
    try {
      var path = "/api/list_soak_rows?soak_id=" + encodeURIComponent(selectedSoak.soak_id) + "&kind=" + encodeURIComponent(rowKind) + "&limit=50";
      if (append && rowCursor) {
        path += "&cursor=" + encodeURIComponent(rowCursor);
      }
      var data = await callApi(path);
      renderRows(data.items || [], append);
      rowCursor = data.next_cursor || null;
      loadMoreButton.disabled = !rowCursor;
      setStatus("", false);
    } catch (err) {
      setStatus(err.message || err.code, true);
    }
  }

  refreshButton.addEventListener("click", loadSoaks);
  edgeOnlyFilter.addEventListener("change", loadSoaks);
  loadMoreButton.addEventListener("click", function () {
    loadRows(true);
  });
  rowKindButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      setRowKind(button.dataset.rowKind);
    });
  });

  loadSoaks();
}());
