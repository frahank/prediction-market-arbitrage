# Scope: BOT_RUNTIME — Persist capture snapshots as recorder-compatible JSONL rows.
"""ObservationSink: write ``BookSnapshot`` rows to the recorder's landing.

Same Tier-1 layout the recorder writes (``data_<run>/raw/book/venue=*/
<date>.jsonl``, via the recorder's ``_DailyWriter``), so the existing DQ
report, edge derivation, and compaction tooling read capture output with no
changes. ``capture_seq`` is monotonically increasing per sink, resuming from
whatever is already in the data dir.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from arbx.capture.types import BookSnapshot, PairedSnapshot
from arbx.data.recorder import _DailyWriter, _resume_seq


class ObservationSink:
    def __init__(
        self,
        data_dir: Path,
        *,
        run_id: str,
        ntp_offset_ms: float | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._run_id = run_id
        self.ntp_offset_ms = ntp_offset_ms  # mutable: re-measured every 15 min
        self._writers: dict[str, _DailyWriter] = {}
        self._seq = _resume_seq(self._data_dir)
        self._last_written_ns: dict[tuple[str, str], int] = {}

    def write_snapshot(self, snapshot: BookSnapshot) -> dict[str, Any]:
        """Persist one snapshot; returns its observation row either way.

        A ``PairedSnapshot`` re-presents the unchanged leg's latest book on
        every counterpart update, so a snapshot already written (same venue,
        market, receive time) is not written twice — keeping
        ``recv_monotonic_ns`` unique per (venue, market) for joins.
        """
        key = (snapshot.venue, snapshot.market_id)
        already_written = self._last_written_ns.get(key) == snapshot.recv_monotonic_ns
        if not already_written:
            self._seq += 1
        row = snapshot.to_observation_row(
            run_id=self._run_id,
            capture_seq=self._seq,
            ntp_offset_ms=self.ntp_offset_ms,
        )
        if already_written:
            return row
        self._last_written_ns[key] = snapshot.recv_monotonic_ns
        writer = self._writers.get(snapshot.venue)
        if writer is None:
            writer = self._writers[snapshot.venue] = _DailyWriter(self._data_dir, snapshot.venue)
        writer.write(row)
        return row

    def write_paired(self, paired: PairedSnapshot) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.write_snapshot(paired.kalshi), self.write_snapshot(paired.polymarket)

    def close(self) -> None:
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()
