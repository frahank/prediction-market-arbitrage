# Capture-layer venue notes

## Kalshi WebSocket: authentication IS required (verified live 2026-07-03)

Unauthenticated handshakes are rejected with **HTTP 401** on both documented
endpoints:

- `wss://api.elections.kalshi.com/trade-api/ws/v2` → 401
- `wss://external-api-ws.kalshi.com/trade-api/ws/v2` → 401

The official docs (`docs.kalshi.com/getting_started/quick_start_websockets`)
confirm: the connection handshake requires `KALSHI-ACCESS-KEY`,
`KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`, with the signature an
RSA signing of `timestamp + "GET" + "/trade-api/ws/v2"`. There is no
unauthenticated market-data WebSocket.

Consequences:

- `arbx.capture.kalshi_ws.KalshiBookStream` is credential-parameterized
  (`auth_headers` callable) and raises `KalshiWsAuthError` with a clear
  message when used without credentials. The RSA-PSS signer and local
  credential loader are implemented under `arbx.accounts`; credential YAML
  lives outside the repo under `~/.arbx/credentials/`, and the PEM remains at
  the external path supplied by the operator.
- Without a Kalshi key, streaming mode pairs the **Polymarket WS** (public) with a
  **Kalshi REST poll leg** (`KalshiRestPollStream`, clearly a stopgap). The
  Polymarket leg gets push-latency updates; the Kalshi leg keeps REST
  cadence, so paired skew in this hybrid is bounded by the Kalshi poll
  interval, not by the WS. True two-sided event-driven recording needs a
  read-scoped Kalshi API key and RSA private key.

Message shapes (verified against `docs.kalshi.com/websockets/orderbook-updates`):
`orderbook_snapshot` carries `yes_dollars_fp` / `no_dollars_fp` bid ladders
as `[price_dollars, count]` string pairs (an earlier cents-integer assumption
was outdated; dollars-fp is current and the stream accepts both);
`orderbook_delta` carries `price_dollars`, `delta_fp`, `side`, `ts_ms`;
`seq` is contiguous per connection and any gap invalidates the local book
(the stream drops state and resubscribes).

## Polymarket CLOB WebSocket: public, verified working (2026-07-03)

- Endpoint `wss://ws-subscriptions-clob.polymarket.com/ws/market`, **no
  auth** (docs.polymarket.com, CLOB → WebSocket → market channel).
- Subscribe `{"assets_ids": [<yes_token_id>...], "type": "market"}`.
- Events arrive as objects **or JSON arrays** of objects: `book` (full
  YES-space bids/asks with epoch-ms `timestamp`) and `price_change`
  (`price_changes` list; `side` BUY/SELL; `size "0"` removes a level).
- 60s live smoke against approved-pair tokens shows book + price_change
  updates flowing.

## Concurrent REST skew findings (2026-07-03, this host)

- Sequential warm-connection RTTs: Kalshi ~35ms, Polymarket ~95ms, per-request
  jitter ≤15ms.
- Naively bursting 2×30 GETs per cycle doubles latencies and adds ~100ms
  jitter — the burst, not the venues, was most of the measured skew.
- `ConcurrentRestSource` therefore (a) staggers pairs evenly across the
  cycle and (b) fires the slower venue early by the per-venue EWMA RTT
  difference. `skew_ms` remains honestly measured, never assumed.
