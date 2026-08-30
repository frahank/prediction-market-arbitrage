# Scope: TEST — latency-study report schema on a local stub server (P5-T4).
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.latency_study import percentiles, run_study  # noqa: E402


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib handler name
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        pass


def test_report_schema():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/status"
        report = run_study({"stub": url}, n=3, delay_s=0.0)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert report["n_per_endpoint"] == 3
    assert report["vantage"] == "local"
    (endpoint,) = report["endpoints"]
    assert endpoint["name"] == "stub"
    assert endpoint["failures"] == 0
    assert endpoint["statuses"] == {"200": 3}
    stages = endpoint["stages"]
    assert set(stages) == {"tcp_connect_ms", "tls_handshake_ms", "http_get_ms", "total_ms"}
    for stage in ("tcp_connect_ms", "http_get_ms", "total_ms"):
        pct = stages[stage]
        assert set(pct) == {"p50", "p95", "p99"}
        assert 0 <= pct["p50"] <= pct["p95"] <= pct["p99"]
    # plain-http stub has no TLS stage
    assert stages["tls_handshake_ms"] == {"p50": None, "p95": None, "p99": None}


def test_percentiles_ordering():
    pct = percentiles([5.0, 1.0, 3.0, 2.0, 4.0])
    assert pct["p50"] == 3.0
    assert pct["p50"] <= pct["p95"] <= pct["p99"] == 5.0
    assert percentiles([]) == {"p50": None, "p95": None, "p99": None}
