#!/usr/bin/env python3
"""Tiny client for the public cryptostruct.com minute-aggregate analyze API.

Endpoints used (no auth, be polite — data updates once per minute, do not
poll faster):

    GET /api/analyze/instrument-last-24h-minutes/{id}   rolling 1440 minutes
    GET /api/analyze/instrument-day-minutes/{id}?date=YYYY-MM-DD   one UTC day
    GET /api/search?q=&venue=&class=&base=&limit=       instrument-ID lookup

Both minute endpoints return the same per-minute rows; this module always
fetches JSON and normalizes the short JSON keys to the 22 canonical CSV
column names (COLUMN_MAP below), so rows look identical regardless of the
endpoint. Values may be None: minutes without trades have None
open/high/low/close/vwap (counts stay 0); minutes can also be missing from
the series entirely.

Usage:

    python3 fetch_minutes.py --search "btcusdt" --venue binance_swap
    python3 fetch_minutes.py 67824 --out minutes.csv
    python3 fetch_minutes.py 1210 --date 2026-06-24 --json

    from fetch_minutes import search_instruments, fetch_minutes, to_csv
    rows = fetch_minutes(67824)                    # last 24h
    rows = fetch_minutes(1210, date="2026-06-24")  # one UTC day

Stdlib only (urllib).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Optional

BASE = "https://cryptostruct.com"
USER_AGENT = "cryptostruct-skill/1.0 (+https://cryptostruct.com/download)"

# JSON key -> canonical CSV column, in canonical column order. The JSON
# variant additionally carries recording-health telemetry (max_no_trade_s,
# errors, latency_top_ms, latency_trades_ms) — operational signals, not
# market data; they are intentionally not mapped.
COLUMN_MAP = {
    "t": "time",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "vwap": "vwap",
    "trades": "trades",
    "trades_buy": "trades_buy",
    "trades_sell": "trades_sell",
    "trades_liquidation": "trades_liquidation",
    "usd": "turnover_usd",
    "usd_buy": "turnover_buy_usd",
    "usd_sell": "turnover_sell_usd",
    "spread_avg": "spread_avg",
    "spread_bps": "spread_bps",
    "spread_ticks": "spread_ticks",
    "top1_bid": "top1_bid_usd",
    "top1_ask": "top1_ask_usd",
    "top20_bid": "top20_bid_usd",
    "top20_ask": "top20_ask_usd",
    "top1_bid_qty": "top1_bid_qty",
    "top1_ask_qty": "top1_ask_qty",
}

COLUMNS = list(COLUMN_MAP.values())


def _get_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_instruments(
    query: str,
    *,
    venue: Optional[str] = None,
    klass: Optional[str] = None,
    base: Optional[str] = None,
    limit: int = 10,
    base_url: str = BASE,
) -> list[dict]:
    """Resolve instrument IDs. `query` matches the instrument code (e.g.
    'btcusdt'); venue (exchange code like 'binance_swap', 'bitmex'), klass
    ('spot' | 'perpetual' | ...) and base (base asset like 'BTC') are separate
    filters — do NOT put the venue into the query string.

    Returns the API's result objects: {instrument_id, code, type,
    exchange: {code, name}, base_underlying, counter_underlying, state,
    data: {days, first_day, last_day, total_bytes}, ...}.
    """
    params = {"q": query, "limit": str(limit)}
    if venue:
        params["venue"] = venue
    if klass:
        params["class"] = klass
    if base:
        params["base"] = base
    url = f"{base_url}/api/search?{urllib.parse.urlencode(params)}"
    return _get_json(url).get("results", [])


def fetch_minutes(
    instrument_id: int,
    *,
    date: Optional[str] = None,
    base_url: str = BASE,
) -> list[dict]:
    """Fetch per-minute rows with canonical CSV column names.

    date=None      -> rolling last 24h (instrument-last-24h-minutes)
    date='YYYY-MM-DD' -> that UTC day  (instrument-day-minutes)

    Raises RuntimeError when the response signals that upstream data is
    temporarily unavailable (needs_upstream with empty minutes) — retry later.
    A partial series (gaps) is returned as-is; the API's `note` is attached to
    the exception message only in the unavailable case.
    """
    if date is None:
        url = f"{base_url}/api/analyze/instrument-last-24h-minutes/{instrument_id}"
    else:
        url = f"{base_url}/api/analyze/instrument-day-minutes/{instrument_id}?date={date}"
    payload = _get_json(url)
    minutes = payload.get("minutes", [])
    if not minutes and payload.get("needs_upstream"):
        raise RuntimeError(
            f"no data returned for instrument {instrument_id}"
            + (f" ({payload.get('note')})" if payload.get("note") else "")
            + " — upstream temporarily unavailable, retry later"
        )
    return [{csv_key: row.get(json_key) for json_key, csv_key in COLUMN_MAP.items()} for row in minutes]


def to_csv(rows: list[dict]) -> str:
    """Serialize rows to CSV with the canonical 22-column header (None -> empty
    cell, CRLF line endings — byte-compatible with the API's ?format=csv)."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\r\n")
    w.writeheader()
    for row in rows:
        w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in COLUMNS})
    return buf.getvalue()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="fetch_minutes.py",
        description="Fetch CryptoStruct per-minute statistics (public API).",
    )
    ap.add_argument("instrument_id", nargs="?", type=int, help="instrument id (see --search)")
    ap.add_argument("--search", metavar="QUERY", help="search instruments by code instead of fetching")
    ap.add_argument("--venue", help="exchange code filter, e.g. binance_swap, bitmex")
    ap.add_argument("--class", dest="klass", help="instrument class filter, e.g. spot, perpetual")
    ap.add_argument("--base", help="base asset filter, e.g. BTC")
    ap.add_argument("--date", help="UTC day YYYY-MM-DD (default: rolling last 24h)")
    ap.add_argument("--out", help="write CSV to this file (default: stdout)")
    ap.add_argument("--json", action="store_true", help="print rows as JSON instead of CSV")
    ap.add_argument("--base-url", default=BASE, help=f"API origin (default {BASE})")
    args = ap.parse_args(argv)

    if args.search:
        hits = search_instruments(
            args.search, venue=args.venue, klass=args.klass, base=args.base, base_url=args.base_url
        )
        if not hits:
            print("no instruments found", file=sys.stderr)
            return 1
        for h in hits:
            d = h.get("data", {})
            print(
                f"{h['instrument_id']:>9}  {h['code']:<18} {h['type']:<10} "
                f"{h['exchange']['code']:<16} {d.get('days', 0):>5} days  "
                f"{d.get('first_day', '')} → {d.get('last_day', '')}"
            )
        return 0

    if args.instrument_id is None:
        ap.error("instrument_id or --search required")

    rows = fetch_minutes(args.instrument_id, date=args.date, base_url=args.base_url)
    print(f"{len(rows)} minutes for instrument {args.instrument_id}", file=sys.stderr)
    if args.json:
        out_text = json.dumps(rows, indent=2)
    else:
        out_text = to_csv(rows)
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            fh.write(out_text)
    else:
        sys.stdout.write(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
