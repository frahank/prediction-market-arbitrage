#!/usr/bin/env python3
# Scope: BOT_RUNTIME — Local public-endpoint latency baseline.
#
# For each venue endpoint, runs N timed probes on FRESH connections and reports
# p50/p95/p99 per stage: TCP connect, TLS handshake, and the full HTTP GET
# round trip. Output: reports/latency/<date>/local_baseline.{json,md} — the
# This measures network response time only; it is not an order-routing benchmark.
#
# Public GETs to public endpoints only; no auth, no order capability. Minutes,
# not a soak (N=500 per endpoint at a 20ms spacing is ~1-2 min each).
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

# Cheap public endpoints, one per venue surface we depend on. The WS host
# answers a plain GET with an upgrade error — the status is irrelevant, the
# wire round trip is what's measured.
DEFAULT_ENDPOINTS: dict[str, str] = {
    "kalshi_rest": "https://api.elections.kalshi.com/trade-api/v2/exchange/status",
    "polymarket_clob": "https://clob.polymarket.com/time",
    "polymarket_ws_host": "https://ws-subscriptions-clob.polymarket.com/",
}

_READ_CAP_BYTES = 1_000_000


def probe_once(url: str, *, timeout_s: float = 10.0) -> dict[str, float | int | None]:
    """One fresh-connection probe: returns per-stage millisecond timings.

    ``tls_handshake_ms`` is None for plain-http URLs (the test stub server).
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    use_tls = parsed.scheme == "https"
    port = parsed.port or (443 if use_tls else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    t0 = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=timeout_s)
    t_tcp = time.perf_counter()
    tls_ms: float | None = None
    try:
        if use_tls:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
            tls_ms = (time.perf_counter() - t_tcp) * 1000.0
        t_req = time.perf_counter()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            "User-Agent: arbx-latency-study\r\nAccept: */*\r\nConnection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        chunks: list[bytes] = []
        received = 0
        while received < _READ_CAP_BYTES:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        t_done = time.perf_counter()
    finally:
        sock.close()
    head = b"".join(chunks)[:64].decode("latin-1", errors="replace")
    status = None
    if head.startswith("HTTP/"):
        parts = head.split(" ", 2)
        if len(parts) > 1 and parts[1][:3].isdigit():
            status = int(parts[1][:3])
    return {
        "tcp_connect_ms": (t_tcp - t0) * 1000.0,
        "tls_handshake_ms": tls_ms,
        "http_get_ms": (t_done - t_req) * 1000.0,
        "total_ms": (t_done - t0) * 1000.0,
        "status": status,
    }


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(values)
    n = len(ordered)

    def pick(q: float) -> float:
        return round(ordered[min(n - 1, round(q * (n - 1)))], 3)

    return {"p50": pick(0.50), "p95": pick(0.95), "p99": pick(0.99)}


def run_study(
    endpoints: dict[str, str], *, n: int, delay_s: float = 0.02
) -> dict[str, object]:
    results = []
    for name, url in endpoints.items():
        stage_values: dict[str, list[float]] = {
            "tcp_connect_ms": [],
            "tls_handshake_ms": [],
            "http_get_ms": [],
            "total_ms": [],
        }
        failures = 0
        statuses: dict[str, int] = {}
        for _ in range(n):
            try:
                sample = probe_once(url)
            except OSError:
                failures += 1
                continue
            for stage, bucket in stage_values.items():
                value = sample.get(stage)
                if isinstance(value, (int, float)):
                    bucket.append(float(value))
            key = str(sample.get("status"))
            statuses[key] = statuses.get(key, 0) + 1
            time.sleep(delay_s)
        results.append(
            {
                "name": name,
                "url": url,
                "probes": n,
                "failures": failures,
                "statuses": statuses,
                "stages": {stage: percentiles(vals) for stage, vals in stage_values.items()},
            }
        )
        print(f"[{name}] {n} probes, {failures} failures, "
              f"total p50={results[-1]['stages']['total_ms']['p50']}ms")
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "vantage": "local",
        "n_per_endpoint": n,
        "endpoints": results,
    }


def _write_markdown(report: dict, path: Path) -> None:
    lines = [
        f"# Local latency baseline — {report['generated_at_utc'][:10]}",
        "",
        f"Vantage: `{report['vantage']}` · {report['n_per_endpoint']} fresh-connection "
        "probes per endpoint · stages in ms.",
        "",
        "| endpoint | stage | p50 | p95 | p99 |",
        "|---|---|---|---|---|",
    ]
    for ep in report["endpoints"]:
        for stage, pct in ep["stages"].items():
            if pct["p50"] is None:
                continue
            lines.append(
                f"| {ep['name']} | {stage} | {pct['p50']} | {pct['p95']} | {pct['p99']} |"
            )
    lines += [
        "",
        "This public-GET baseline is not an order-routing or fill-latency measurement.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local venue-latency baseline (public GETs)")
    parser.add_argument("--n", type=int, default=500, help="probes per endpoint")
    parser.add_argument("--delay-ms", type=float, default=20.0, help="spacing between probes")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="default: reports/latency/<today>/")
    args = parser.parse_args(argv)

    out_dir = args.out_dir or (
        ROOT / "reports" / "latency" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    report = run_study(DEFAULT_ENDPOINTS, n=args.n, delay_s=args.delay_ms / 1000.0)
    json_path = out_dir / "local_baseline.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _write_markdown(report, out_dir / "local_baseline.md")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
