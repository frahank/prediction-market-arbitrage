# Clock discipline

## What the bot does

- At every recorder / capture-source start, and every 15 minutes thereafter
  (`arbx.data.recorder.NTP_REFRESH_SECONDS`), the host wall clock's offset
  from an NTP reference is measured via a pure-stdlib SNTP query
  (`arbx.capture.clock.measure_ntp_offset_ms`, default server
  `time.google.com`).
- The latest offset is stamped into **every** `book_observations` row as
  `ntp_offset_ms` (schema §1: host wall clock **minus** NTP reference, in
  milliseconds) and into the `recorder_start` continuity event.
- A WARNING is logged when `|offset| > 250 ms` — at that magnitude the wall
  clock blurs latency buckets and cross-run comparisons.
- A failed measurement (no network, DNS, timeout) keeps the previous value
  and logs it; rows are never dropped over a missing offset.

## What the bot does NOT do

The bot **only measures and records** — it never adjusts, slews, or steps
the host clock. Time discipline belongs to the OS: run on a host with NTP
enabled (the macOS default, System Settings → General → Date & Time →
"Set time and date automatically").

## Why monotonic joins are unaffected

`recv_monotonic_ns` (the primary join key, schema §1) comes from
`time.monotonic_ns()` and is immune to NTP corrections. `ntp_offset_ms`
exists so downstream analysis can flag *runs* where the wall-clock
(`capture_ts_utc`) uncertainty exceeds a latency-bucket width — the DQ
report's `ntp_offset` threshold checks `|offset| ≤ 100 ms` per run.
