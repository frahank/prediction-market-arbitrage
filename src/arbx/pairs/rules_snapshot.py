# Scope: BOT_RUNTIME — P4-T2 rules snapshot: pin both venues' resolution rules per pair.
"""Fetch and pin the resolution rules a pair-equivalence audit runs against.

Public GETs only:
  Kalshi market:   GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}
                   (fields ``rules_primary``, ``rules_secondary``, ``close_time``)
  Polymarket Gamma: GET https://gamma-api.polymarket.com/markets?condition_ids={id}
                   (fields ``description``, ``endDate``/``endDateIso``, ``resolutionSource``;
                   the response is a JSON *array* of market objects)

The snapshot's ``sha256`` covers the normalized rule texts only (not fetch
time), so an unchanged market re-snapshots to the same hash and any venue
rules edit changes it — that hash is what ``equivalence.rules_snapshot_sha256``
in the registry pins. Missing fields become empty strings plus a ``warnings``
entry; they never raise.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from arbx.pairs.registry import PairSpec

KALSHI_MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?condition_ids={condition_id}"
# Gamma 403s the default urllib agent; same public UA the venue adapters use.
PUBLIC_HEADERS = {"User-Agent": "arbx-public-data/0.1", "Accept": "application/json"}


class RulesProvider(Protocol):
    """Injectable fetch seam — tests supply canned payloads."""

    def fetch_kalshi_market_json(self, ticker: str) -> dict[str, Any] | None: ...

    def fetch_gamma_markets_json(self, condition_id: str) -> list[dict[str, Any]] | None: ...


class PublicRulesProvider:
    def _get(self, url: str) -> Any:
        try:
            request = Request(url, headers=PUBLIC_HEADERS, method="GET")
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None

    def fetch_kalshi_market_json(self, ticker: str) -> dict[str, Any] | None:
        payload = self._get(KALSHI_MARKET_URL.format(ticker=ticker))
        return payload if isinstance(payload, dict) else None

    def fetch_gamma_markets_json(self, condition_id: str) -> list[dict[str, Any]] | None:
        payload = self._get(GAMMA_MARKETS_URL.format(condition_id=condition_id))
        return payload if isinstance(payload, list) else None


@dataclass(frozen=True)
class RulesSnapshot:
    pair_key: str
    kalshi_market_id: str
    polymarket_condition_id: str
    kalshi_rules_text: str
    kalshi_close_time: datetime | None
    poly_description: str
    poly_resolution_source: str
    poly_end_date: datetime | None
    fetched_at: datetime
    sha256: str
    warnings: tuple[str, ...] = ()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _rules_sha256(kalshi_rules: str, poly_description: str, poly_source: str) -> str:
    blob = "\n".join((_normalize(kalshi_rules), _normalize(poly_description),
                      _normalize(poly_source)))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_rules(pair: PairSpec, provider: RulesProvider | None = None) -> RulesSnapshot:
    provider = provider or PublicRulesProvider()
    warnings: list[str] = []

    kalshi_payload = provider.fetch_kalshi_market_json(pair.kalshi_market_id)
    market = (kalshi_payload or {}).get("market") or {}
    if not market:
        warnings.append(f"kalshi market payload missing for {pair.kalshi_market_id}")
    rules_primary = str(market.get("rules_primary") or "")
    rules_secondary = str(market.get("rules_secondary") or "")
    kalshi_rules = (rules_primary + ("\n\n" + rules_secondary if rules_secondary else "")).strip()
    if not kalshi_rules:
        warnings.append("kalshi rules_primary/rules_secondary empty")
    kalshi_close = _parse_ts(market.get("close_time"))
    if kalshi_close is None:
        warnings.append("kalshi close_time missing/unparseable")

    gamma = provider.fetch_gamma_markets_json(pair.polymarket_condition_id) or []
    gm = gamma[0] if gamma else {}
    if not gm:
        warnings.append(f"gamma market payload missing for {pair.polymarket_condition_id}")
    description = str(gm.get("description") or "")
    if not description:
        warnings.append("gamma description empty")
    resolution_source = str(gm.get("resolutionSource") or "")
    if not resolution_source:
        warnings.append("gamma resolutionSource empty (UMA-resolved markets often omit it)")
    end_date = _parse_ts(gm.get("endDate") or gm.get("endDateIso"))
    if end_date is None:
        events = gm.get("events") or []
        if events and isinstance(events[0], dict):
            end_date = _parse_ts(events[0].get("endDate"))
    if end_date is None:
        warnings.append("gamma endDate missing — fall back to registry polymarket_close_time")

    return RulesSnapshot(
        pair_key=pair.pair_key,
        kalshi_market_id=pair.kalshi_market_id,
        polymarket_condition_id=pair.polymarket_condition_id,
        kalshi_rules_text=kalshi_rules,
        kalshi_close_time=kalshi_close,
        poly_description=description,
        poly_resolution_source=resolution_source,
        poly_end_date=end_date,
        fetched_at=datetime.now(timezone.utc),
        sha256=_rules_sha256(kalshi_rules, description, resolution_source),
        warnings=tuple(warnings),
    )


def save(snapshot: RulesSnapshot, evidence_dir: Path) -> Path:
    """Write ``rules_snapshot.json`` into an evidence-pack directory."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(snapshot)
    for key in ("kalshi_close_time", "poly_end_date", "fetched_at"):
        value = payload[key]
        payload[key] = value.isoformat() if value is not None else None
    payload["warnings"] = list(snapshot.warnings)
    out = evidence_dir / "rules_snapshot.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _main(argv: list[str]) -> int:
    import argparse

    from arbx.pairs.registry import load_pairs

    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Snapshot both venues' resolution rules for one pair")
    parser.add_argument("market", help="Kalshi market id (e.g. KXWCCONTINENT-26-NA) or pair_key")
    parser.add_argument("--pairs", type=Path, default=root / "configs" / "pairs.approved.yaml")
    parser.add_argument("--evidence-root", type=Path, default=root / "evidence")
    args = parser.parse_args(argv)

    pairs = load_pairs(args.pairs)
    match = next((p for p in pairs
                  if p.kalshi_market_id == args.market or p.pair_key == args.market), None)
    if match is None:
        print(f"pair {args.market!r} not found in {args.pairs}")
        return 1
    snapshot = fetch_rules(match)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = save(snapshot, args.evidence_root / match.kalshi_market_id / date)
    print(f"rules snapshot written: {out}")
    print(f"  sha256:   {snapshot.sha256}")
    print(f"  warnings: {list(snapshot.warnings) or 'none'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv[1:]))
