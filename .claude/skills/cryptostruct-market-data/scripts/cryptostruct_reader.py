#!/usr/bin/env python3
"""Streaming reader for CryptoStruct full-day market-data files (*.txt.zst).

One file = one instrument x one UTC day. The file is zstd-compressed text:
line 1 is a JSON object with instrument masterdata + underlyings, every other
line is one event encoded as a JSON array:

    [msgType, instrumentId, prevEventId, eventId, adapterTs, exchangeTs, data, ...]

msgType: 0 book snapshot | 1 book update | 2 trades | 5 instrument state
         (different shape: [5, id, adapterTs, state, message]) | 6 top-of-book
         | 7 mark price (+ options greeks/vols) | 8 index price | 9 funding
         | 17 liquidations.
Timestamps are integer NANOSECONDS since epoch, UTC. Prices/quantities are
decimal strings. Lines may carry trailing elements past `data` depending on
the file's protocol version: snapshots end with a `forceReset` boolean
(schema v6), mark prices carry 7 greek/volatility fields (schema v7, null for
non-options). Known tails are captured in `Event.extras`; anything further is
ignored (parse positionally, tolerate unknown tails).

Dependencies: none required. `zstd` CLI or `pip install zstandard` for .zst
input (one of the two must be available); `polars` (preferred) or `pandas`
only for the *_frame helpers / parquet output.

Library quickstart:

    from cryptostruct_reader import read_header, iter_trades, trades_frame, book_at
    hdr = read_header('1210_2026-06-24.txt.zst')      # instrument masterdata
    for t in iter_trades('1210_2026-06-24.txt.zst'):  # flattened fills
        ...
    df = trades_frame('1210_2026-06-24.txt.zst')      # polars/pandas DataFrame
    book = book_at('1210_2026-06-24.txt.zst', '2026-06-24T14:30:00Z')
    liq = liquidations_frame('1210_2026-06-24.txt.zst')   # msgType 17 rows
    gk  = greeks_frame('8967995_2026-06-26.txt.zst')      # mark + options greeks

CLI quickstart:

    python3 cryptostruct_reader.py info    FILE            # header summary (instant)
    python3 cryptostruct_reader.py stats   FILE [--deep]   # counts, coverage, gaps
    python3 cryptostruct_reader.py trades  FILE --out trades.parquet
    python3 cryptostruct_reader.py book    FILE --every 1s --depth 20 --out book.parquet
    python3 cryptostruct_reader.py prices  FILE            # mark+index, CSV to stdout
    python3 cryptostruct_reader.py funding FILE
    python3 cryptostruct_reader.py liquidations FILE       # msgType 17 (empty pre-2026 files)
    python3 cryptostruct_reader.py greeks  FILE            # mark + options greeks/vols
    python3 cryptostruct_reader.py selftest                # offline format self-check

Changelog: 1.1 adds `Event.extras` (trailing tuple field, default `()`),
liquidations (msgType 17) and mark-price greeks support — existing frame
columns and `data` semantics are unchanged.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Iterator, NamedTuple, Optional, Union

__version__ = "1.1"

NS = 1_000_000_000

# --------------------------------------------------------------------------
# IO layer
# --------------------------------------------------------------------------


def open_text(path: Union[str, Path]) -> Iterator[str]:
    """Yield decoded text lines from a CryptoStruct day file.

    Handles `.zst` via the `zstandard` module when installed, otherwise via a
    `zstd -dc` subprocess (any zstd >= 1.0). Plain paths (already
    decompressed) are read directly. Streaming: constant memory even on
    multi-GB files.
    """
    p = Path(path)
    if p.suffix != ".zst":
        with open(p, "r", encoding="utf-8") as fh:
            yield from fh
        return

    try:
        import zstandard  # type: ignore
    except ImportError:
        yield from _open_zst_subprocess(p)
        return

    with open(p, "rb") as fh:
        dctx = zstandard.ZstdDecompressor()
        try:
            reader = dctx.stream_reader(fh, read_across_frames=True)
        except TypeError:  # older zstandard without the kwarg
            reader = dctx.stream_reader(fh)
        yield from io.TextIOWrapper(reader, encoding="utf-8")


def _open_zst_subprocess(p: Path) -> Iterator[str]:
    try:
        proc = subprocess.Popen(
            ["zstd", "-dc", "--", str(p)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "cannot decompress .zst: install the zstd CLI (apt-get install zstd / "
            "brew install zstd) or `pip install zstandard`"
        ) from None
    assert proc.stdout is not None
    try:
        yield from io.TextIOWrapper(proc.stdout, encoding="utf-8")
        rc = proc.wait()
        if rc != 0:
            err = proc.stderr.read().decode(errors="replace")[-500:] if proc.stderr else ""
            raise RuntimeError(f"zstd -dc failed with exit code {rc}: {err}")
    finally:
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        if proc.poll() is None:  # consumer stopped early
            proc.kill()
            proc.wait()


def read_header(path: Union[str, Path]) -> dict:
    """Return the first line's masterdata object: {"instrument": ..., "underlyings": ...}.

    O(1) — reads only the first line even on multi-GB files.
    """
    for line in open_text(path):
        return json.loads(line)
    raise ValueError(f"{path}: empty file")


# --------------------------------------------------------------------------
# Event parsing
# --------------------------------------------------------------------------


class Event(NamedTuple):
    msg_type: int
    instrument_id: int
    prev_event_id: Optional[str]  # None when absent/"0" (start of a chain)
    event_id: Optional[str]  # None for msgType 5
    adapter_ts: int  # ns since epoch (recorder receive time)
    exchange_ts: Optional[int]  # ns since epoch, None when the venue sent none
    data: Any  # raw payload; prices/quantities still strings
    extras: tuple = ()  # elements past `data`: snapshot forceReset, mark greeks/vols


def _msg_type_prefix(line: str) -> Optional[int]:
    """Cheap msgType peek without a full JSON parse ('[12,' -> 12)."""
    if not line.startswith("["):
        return None
    i = line.find(",", 1, 8)
    if i < 2:
        return None
    try:
        return int(line[1:i])
    except ValueError:
        return None


def parse_event(line: str) -> Event:
    """Parse one event line. Elements past `data` land in `extras` (snapshot
    forceReset, mark greeks/vols — arity varies with the file's protocol
    version); consumers that don't care can keep ignoring them."""
    m = json.loads(line)
    t = m[0]
    if t == 5:  # instrument state has its own, shorter shape
        return Event(5, m[1], None, None, m[2], None, (m[3], m[4]))
    prev = m[2]
    if prev in ("", "0", None):
        prev = None
    ex = m[5]
    tail = tuple(m[7:]) if len(m) > 7 else ()
    return Event(t, m[1], prev, m[3], m[4], ex if ex else None, m[6], tail)


def parse_ts(ts: Union[int, str, datetime]) -> int:
    """Coerce int ns / ISO-8601 string / datetime (naive = UTC) to int ns."""
    if isinstance(ts, int):
        return ts
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.timestamp() * NS)
    raise TypeError(f"unsupported timestamp: {ts!r}")


def ts_to_datetime(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / NS, tz=timezone.utc)


def iter_events(
    path: Union[str, Path],
    *,
    msg_types: Optional[Collection[int]] = None,
    start_ts: Union[int, str, datetime, None] = None,
    end_ts: Union[int, str, datetime, None] = None,
) -> Iterator[Event]:
    """Stream events, optionally filtered by msgType and adapter_ts window
    (start inclusive, end exclusive).

    The msgType filter is applied with a cheap string peek BEFORE the JSON
    parse, so e.g. a trades-only pass over a file dominated by book updates
    skips ~98% of the parse cost.

    NOTE for book reconstruction: do NOT pass start_ts — the book at time T
    depends on every update since the previous snapshot. Use book_at() /
    sample_book(), which replay correctly.
    """
    wanted = set(msg_types) if msg_types is not None else None
    lo = parse_ts(start_ts) if start_ts is not None else None
    hi = parse_ts(end_ts) if end_ts is not None else None
    first = True
    for line in open_text(path):
        if first:  # masterdata header
            first = False
            continue
        if wanted is not None:
            t = _msg_type_prefix(line)
            if t is not None and t not in wanted:
                continue
        ev = parse_event(line)
        if wanted is not None and ev.msg_type not in wanted:
            continue
        if lo is not None and ev.adapter_ts < lo:
            continue
        if hi is not None and ev.adapter_ts >= hi:
            continue
        yield ev


class Trade(NamedTuple):
    adapter_ts: int  # ns, recorder receive time of the batch
    exchange_ts: Optional[int]  # ns, per-fill matching-engine time (None if absent)
    side: int  # aggressor: 0 = buy, 1 = sell
    price: float
    qty: float  # venue-native quantity — see header multiplier for base units
    trade_id: Optional[str]


def iter_trades(
    path: Union[str, Path],
    *,
    start_ts: Union[int, str, datetime, None] = None,
    end_ts: Union[int, str, datetime, None] = None,
) -> Iterator[Trade]:
    """Stream individual fills (trade events hold batches; this flattens them)."""
    for ev in iter_events(path, msg_types=(2,), start_ts=start_ts, end_ts=end_ts):
        for row in ev.data:
            ts = row[4] if len(row) > 4 and row[4] else None
            tid = row[3] if len(row) > 3 and row[3] not in ("", None) else None
            yield Trade(ev.adapter_ts, ts, row[0], float(row[1]), float(row[2]), tid)


class Liquidation(NamedTuple):
    adapter_ts: int  # ns, recorder receive time of the batch
    exchange_ts: Optional[int]  # ns, per-row liquidation time (None if absent)
    side: int  # 0 / 1 — same code as book rows; the spec does not say whether it
    #            names the liquidated position or the forced order
    price: float  # BANKRUPTCY price, not a trade price
    qty: float  # venue-native quantity — see header multiplier for base units


def iter_liquidations(
    path: Union[str, Path],
    *,
    start_ts: Union[int, str, datetime, None] = None,
    end_ts: Union[int, str, datetime, None] = None,
) -> Iterator[Liquidation]:
    """Stream individual liquidation rows (msgType 17 events hold batches;
    this flattens them). Files recorded before schema v5 have none. Some
    venues only publish samples — treat counts as a lower bound."""
    for ev in iter_events(path, msg_types=(17,), start_ts=start_ts, end_ts=end_ts):
        for row in ev.data:
            ts = row[3] if len(row) > 3 and row[3] else None
            yield Liquidation(ev.adapter_ts, ts, row[0], float(row[1]), float(row[2]))


class Mark(NamedTuple):
    adapter_ts: int  # ns, recorder receive time
    exchange_ts: Optional[int]  # ns, venue time (None if absent)
    mark_price: float
    delta: Optional[float]  # options only (schema v7); None otherwise
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    mark_volatility: Optional[float]
    bid_volatility: Optional[float]
    ask_volatility: Optional[float]


def _mark_from_event(ev: Event) -> Mark:
    """Decode a msgType-7 event into a Mark. The 7 greek/volatility fields
    live in `ev.extras` (schema v7); older files have no tail — all None."""
    tail = ev.extras[:7] + (None,) * (7 - min(len(ev.extras), 7))
    return Mark(
        ev.adapter_ts,
        ev.exchange_ts,
        float(ev.data),
        *(float(x) if x is not None else None for x in tail),
    )


def iter_marks(
    path: Union[str, Path],
    *,
    start_ts: Union[int, str, datetime, None] = None,
    end_ts: Union[int, str, datetime, None] = None,
) -> Iterator[Mark]:
    """Stream mark-price events incl. options greeks/vols where present."""
    for ev in iter_events(path, msg_types=(7,), start_ts=start_ts, end_ts=end_ts):
        yield _mark_from_event(ev)


# --------------------------------------------------------------------------
# Order book
# --------------------------------------------------------------------------


class Book:
    """Level-2 order book replayed from snapshot (type 0) + update (type 1) events.

    Prices/quantities are floats keyed per side. `suspect` turns True when an
    event-chain gap is detected and clears at the next snapshot — treat book
    states while suspect with care.
    """

    __slots__ = ("bids", "asks", "last_event_id", "gap_count", "suspect", "snapshots")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_event_id: Optional[str] = None
        self.gap_count = 0
        self.suspect = False
        self.snapshots = 0

    def apply(self, ev: Event) -> None:
        """Apply a book event (types 0/1); other msg types are ignored."""
        if ev.msg_type == 1:
            if (
                self.last_event_id is not None
                and ev.prev_event_id is not None
                and ev.prev_event_id != self.last_event_id
            ):
                self.gap_count += 1
                self.suspect = True
            self.last_event_id = ev.event_id
            for side, price, qty, *_ in ev.data:
                book = self.bids if side == 0 else self.asks
                q = float(qty)
                p = float(price)
                if q == 0.0:
                    book.pop(p, None)
                else:
                    book[p] = q
        elif ev.msg_type == 0:
            self.bids.clear()
            self.asks.clear()
            for side, price, qty, *_ in ev.data:
                q = float(qty)
                if q != 0.0:
                    (self.bids if side == 0 else self.asks)[float(price)] = q
            self.last_event_id = ev.event_id
            self.suspect = False
            self.snapshots += 1

    def best_bid(self) -> Optional[tuple[float, float]]:
        if not self.bids:
            return None
        p = max(self.bids)
        return (p, self.bids[p])

    def best_ask(self) -> Optional[tuple[float, float]]:
        if not self.asks:
            return None
        p = min(self.asks)
        return (p, self.asks[p])

    def mid(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (b[0] + a[0]) / 2 if b and a else None

    def spread(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return a[0] - b[0] if b and a else None

    def crossed(self) -> bool:
        b, a = self.best_bid(), self.best_ask()
        return bool(b and a and b[0] >= a[0])

    def top(self, depth: int = 20) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(bids desc, asks asc), each a list of (price, qty), at most `depth` long."""
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        return bids, asks


def book_at(path: Union[str, Path], ts: Union[int, str, datetime]) -> Book:
    """Replay the book up to (and including) adapter_ts == ts.

    Replays from the start of the file; for many timestamps on one day use
    sample_book() instead of repeated book_at() calls.
    """
    cutoff = parse_ts(ts)
    book = Book()
    for ev in iter_events(path, msg_types=(0, 1)):
        if ev.adapter_ts > cutoff:
            break
        book.apply(ev)
    return book


class BookSample(NamedTuple):
    ts: int  # grid boundary, ns since epoch
    bids: list  # [(price, qty), ...] descending, at most `depth`
    asks: list  # [(price, qty), ...] ascending, at most `depth`
    n_events: int  # book events applied since the previous sample
    suspect: bool  # chain gap since last snapshot — book state unreliable


_EVERY_UNITS = {"ms": NS // 1000, "s": NS, "m": 60 * NS, "h": 3600 * NS}


def parse_every(every: Union[str, int]) -> int:
    """'250ms' | '1s' | '5s' | '1m' | '1h' | int ns -> int ns."""
    if isinstance(every, int):
        return every
    s = every.strip().lower()
    for unit in ("ms", "s", "m", "h"):
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * _EVERY_UNITS[unit])
    raise ValueError(f"cannot parse interval {every!r} (use e.g. '250ms', '1s', '1m')")


def sample_book(
    path: Union[str, Path],
    every: Union[str, int] = "1s",
    depth: int = 1,
    *,
    start_ts: Union[int, str, datetime, None] = None,
    end_ts: Union[int, str, datetime, None] = None,
) -> Iterator[BookSample]:
    """Sample the book state on a fixed, epoch-aligned time grid.

    Emits the last state at-or-before each grid boundary, starting at the
    first boundary >= the first book event, ending at the last event (no
    trailing repeats). start_ts/end_ts limit which samples are EMITTED — the
    replay always starts at the beginning of the file so state is correct.
    """
    step = parse_every(every)
    lo = parse_ts(start_ts) if start_ts is not None else None
    hi = parse_ts(end_ts) if end_ts is not None else None
    book = Book()
    next_b: Optional[int] = None
    n_events = 0
    for ev in iter_events(path, msg_types=(0, 1)):
        if next_b is None:
            next_b = (ev.adapter_ts // step + 1) * step
        while ev.adapter_ts > next_b:
            if (lo is None or next_b >= lo) and (hi is None or next_b < hi):
                bids, asks = book.top(depth)
                yield BookSample(next_b, bids, asks, n_events, book.suspect)
                n_events = 0
            next_b += step
            if hi is not None and next_b >= hi:
                return
        book.apply(ev)
        n_events += 1


# --------------------------------------------------------------------------
# DataFrame helpers (optional polars/pandas)
# --------------------------------------------------------------------------


def _engine(engine: str = "auto"):
    """Resolve 'auto'|'polars'|'pandas' to (name, module)."""
    if engine in ("auto", "polars"):
        try:
            import polars as pl  # type: ignore

            return "polars", pl
        except ImportError:
            if engine == "polars":
                raise ImportError("pip install polars") from None
    try:
        import pandas as pd  # type: ignore

        return "pandas", pd
    except ImportError:
        raise ImportError(
            "DataFrame helpers need polars (recommended: pip install polars) or pandas"
        ) from None


def _to_frame(cols: dict, ts_cols: Collection[str], engine: str):
    name, mod = _engine(engine)
    if name == "polars":
        df = mod.DataFrame(cols)
        for c in ts_cols:
            df = df.with_columns(
                mod.from_epoch(mod.col(c), time_unit="ns").dt.replace_time_zone("UTC").alias(c)
            )
        return df
    df = mod.DataFrame(cols)
    for c in ts_cols:
        df[c] = mod.to_datetime(df[c], unit="ns", utc=True)
    return df


def contract_scale(header: dict) -> tuple[Optional[float], bool]:
    """(multiplier, linear?) from a masterdata header.

    linear means neither inverse nor quanto: base = qty * multiplier and
    quote = qty * multiplier * price hold. For inverse/quanto instruments the
    venue's contract convention applies — check `contract_value` /
    `valuation_underlying_id` before computing notionals.
    """
    inst = header.get("instrument", header)
    mult = inst.get("multiplier")
    mult = float(mult) if mult is not None else 1.0
    linear = not inst.get("is_inverse") and not inst.get("is_quanto")
    return mult, linear


def trades_frame(path: Union[str, Path], engine: str = "auto"):
    """All fills of the day as a DataFrame.

    Columns: ts (adapter, datetime UTC), exchange_ts (datetime UTC or null),
    side (0 buy-aggressor / 1 sell-aggressor), price, qty (venue-native),
    base_qty, quote_value (both null unless the instrument is linear),
    trade_id.
    """
    mult, linear = contract_scale(read_header(path))
    cols: dict = {k: [] for k in ("ts", "exchange_ts", "side", "price", "qty", "base_qty", "quote_value", "trade_id")}
    for t in iter_trades(path):
        cols["ts"].append(t.adapter_ts)
        cols["exchange_ts"].append(t.exchange_ts)
        cols["side"].append(t.side)
        cols["price"].append(t.price)
        cols["qty"].append(t.qty)
        cols["base_qty"].append(t.qty * mult if linear else None)
        cols["quote_value"].append(t.qty * mult * t.price if linear else None)
        cols["trade_id"].append(t.trade_id)
    return _to_frame(cols, ("ts", "exchange_ts"), engine)


def book_frame(path: Union[str, Path], every: Union[str, int] = "1s", depth: int = 20, engine: str = "auto"):
    """Sampled book as a wide DataFrame: ts, mid, spread, spread_bps,
    bid_px_1..depth, bid_qty_1.., ask_px_1.., ask_qty_1.., n_events, suspect."""
    cols: dict = {"ts": [], "mid": [], "spread": [], "spread_bps": []}
    for i in range(1, depth + 1):
        for c in (f"bid_px_{i}", f"bid_qty_{i}", f"ask_px_{i}", f"ask_qty_{i}"):
            cols[c] = []
    cols["n_events"] = []
    cols["suspect"] = []
    for s in sample_book(path, every=every, depth=depth):
        cols["ts"].append(s.ts)
        bb = s.bids[0][0] if s.bids else None
        ba = s.asks[0][0] if s.asks else None
        mid = (bb + ba) / 2 if bb is not None and ba is not None else None
        spread = ba - bb if bb is not None and ba is not None else None
        cols["mid"].append(mid)
        cols["spread"].append(spread)
        cols["spread_bps"].append(spread / mid * 1e4 if spread is not None and mid else None)
        for i in range(1, depth + 1):
            b = s.bids[i - 1] if len(s.bids) >= i else (None, None)
            a = s.asks[i - 1] if len(s.asks) >= i else (None, None)
            cols[f"bid_px_{i}"].append(b[0])
            cols[f"bid_qty_{i}"].append(b[1])
            cols[f"ask_px_{i}"].append(a[0])
            cols[f"ask_qty_{i}"].append(a[1])
        cols["n_events"].append(s.n_events)
        cols["suspect"].append(s.suspect)
    return _to_frame(cols, ("ts",), engine)


def prices_frame(path: Union[str, Path], kinds: Collection[str] = ("mark", "index"), engine: str = "auto"):
    """Mark/index price events, long format: ts, kind, price."""
    type_for = {"mark": 7, "index": 8}
    wanted = {type_for[k]: k for k in kinds}
    cols: dict = {"ts": [], "kind": [], "price": []}
    for ev in iter_events(path, msg_types=tuple(wanted)):
        cols["ts"].append(ev.adapter_ts)
        cols["kind"].append(wanted[ev.msg_type])
        cols["price"].append(float(ev.data))
    return _to_frame(cols, ("ts",), engine)


def funding_frame(path: Union[str, Path], engine: str = "auto"):
    """Funding events: ts, rate, next_funding_ts, predicted_rate (null in
    older files). Events are periodic republications of current state, not
    payments — deduplicate on next_funding_ts for one row per interval."""
    cols: dict = {"ts": [], "rate": [], "next_funding_ts": [], "predicted_rate": []}
    for ev in iter_events(path, msg_types=(9,)):
        d = ev.data
        cols["ts"].append(ev.adapter_ts)
        cols["rate"].append(float(d[0]) if d[0] is not None else None)
        cols["next_funding_ts"].append(d[1] if len(d) > 1 and d[1] else None)
        pr = d[2] if len(d) > 2 else None
        cols["predicted_rate"].append(float(pr) if pr is not None else None)
    return _to_frame(cols, ("ts", "next_funding_ts"), engine)


def liquidations_frame(path: Union[str, Path], engine: str = "auto"):
    """All liquidation rows of the day as a DataFrame (msgType 17; empty for
    files recorded before schema v5).

    Columns: ts (adapter, datetime UTC), exchange_ts (per-row liquidation
    time), side, price (BANKRUPTCY price, not a trade price), qty
    (venue-native), base_qty, quote_value (both null unless the instrument is
    linear). Some venues only publish samples — a lower bound, not a complete
    liquidation tape.
    """
    mult, linear = contract_scale(read_header(path))
    cols: dict = {k: [] for k in ("ts", "exchange_ts", "side", "price", "qty", "base_qty", "quote_value")}
    for l in iter_liquidations(path):
        cols["ts"].append(l.adapter_ts)
        cols["exchange_ts"].append(l.exchange_ts)
        cols["side"].append(l.side)
        cols["price"].append(l.price)
        cols["qty"].append(l.qty)
        cols["base_qty"].append(l.qty * mult if linear else None)
        cols["quote_value"].append(l.qty * mult * l.price if linear else None)
    return _to_frame(cols, ("ts", "exchange_ts"), engine)


def greeks_frame(path: Union[str, Path], engine: str = "auto"):
    """Mark-price events incl. options greeks/vols as a DataFrame.

    Columns: ts, exchange_ts, mark_price, delta, gamma, vega, theta,
    mark_volatility, bid_volatility, ask_volatility. The greek/vol columns are
    all-null for non-option instruments and for files recorded before schema
    v7. `prices_frame` stays the lean mark+index price accessor.
    """
    cols: dict = {
        k: []
        for k in (
            "ts", "exchange_ts", "mark_price", "delta", "gamma", "vega", "theta",
            "mark_volatility", "bid_volatility", "ask_volatility",
        )
    }
    for m in iter_marks(path):
        cols["ts"].append(m.adapter_ts)
        cols["exchange_ts"].append(m.exchange_ts)
        cols["mark_price"].append(m.mark_price)
        cols["delta"].append(m.delta)
        cols["gamma"].append(m.gamma)
        cols["vega"].append(m.vega)
        cols["theta"].append(m.theta)
        cols["mark_volatility"].append(m.mark_volatility)
        cols["bid_volatility"].append(m.bid_volatility)
        cols["ask_volatility"].append(m.ask_volatility)
    return _to_frame(cols, ("ts", "exchange_ts"), engine)


# --------------------------------------------------------------------------
# Integrity / stats
# --------------------------------------------------------------------------


def file_stats(path: Union[str, Path], deep: bool = False) -> dict:
    """One full pass over the file: per-type counts, coverage, snapshots,
    chain gaps, parse errors. deep=True additionally replays the book and
    reports crossed-state and level-count statistics (slower)."""
    counts: dict[int, int] = {}
    parse_errors = 0
    first_ts = last_ts = None
    snapshot_ts: list[int] = []
    trades_fills = 0
    liquidation_rows = 0
    trade_last_id: Optional[str] = None
    trade_gaps = 0
    book = Book() if deep else None
    crossed = 0
    checks = 0
    max_levels = 0
    lines = 0
    for line in open_text(path):
        lines += 1
        if lines == 1:
            continue
        try:
            ev = parse_event(line)
        except (ValueError, IndexError, KeyError):
            parse_errors += 1
            continue
        counts[ev.msg_type] = counts.get(ev.msg_type, 0) + 1
        if first_ts is None:
            first_ts = ev.adapter_ts
        last_ts = ev.adapter_ts
        if ev.msg_type == 0:
            snapshot_ts.append(ev.adapter_ts)
        elif ev.msg_type == 2:
            trades_fills += len(ev.data)
            if trade_last_id is not None and ev.prev_event_id is not None and ev.prev_event_id != trade_last_id:
                trade_gaps += 1
            trade_last_id = ev.event_id
        elif ev.msg_type == 17:
            liquidation_rows += len(ev.data)
        if book is not None and ev.msg_type in (0, 1):
            book.apply(ev)
            checks += 1
            if book.crossed():
                crossed += 1
            n = len(book.bids) + len(book.asks)
            if n > max_levels:
                max_levels = n
    out = {
        "path": str(path),
        "events": lines - 1 if lines else 0,
        "counts": dict(sorted(counts.items())),
        "parse_errors": parse_errors,
        "first_adapter_ts": first_ts,
        "last_adapter_ts": last_ts,
        "coverage_s": round((last_ts - first_ts) / NS, 3) if first_ts is not None else None,
        "snapshots": len(snapshot_ts),
        "snapshot_ts": snapshot_ts,
        "trades_fills": trades_fills,
        "liquidation_rows": liquidation_rows,
        "book_chain_gaps": book.gap_count if book is not None else None,
        "trade_chain_gaps": trade_gaps,
    }
    if book is not None:
        out.update(
            {
                "book_states_checked": checks,
                "crossed_states": crossed,
                "crossed_pct": round(100 * crossed / checks, 4) if checks else None,
                "max_book_levels": max_levels,
                "final_best_bid": book.best_bid(),
                "final_best_ask": book.best_ask(),
            }
        )
    return out


# --------------------------------------------------------------------------
# Self-test — real (truncated) lines from production files of both eras
# --------------------------------------------------------------------------

_FIXTURE_HEADER = (
    '{"instrument":{"id":1210,"code":"XRPUSDT","type":"perpetual","is_inverse":false,'
    '"is_quanto":false,"state":"open","exchange_id":1,"exchange_code":"bitmex",'
    '"multiplier":0.01,"ticksize":1.0E-4,"lot_size":100,"funding_interval":"28800"},'
    '"underlyings":[{"id":103,"name":"Tether","code":"USDT","type":"stable_coin","fx_rate":0.99846}]}'
)

_FIXTURE_LINES = [
    # 2026-era snapshot: arity 8 (trailing false), truncated to 2+2 levels
    '[0,1210,"0","8328425028614922983",1782345115505070405,1782345115504140603,'
    '[[0,"1.0715","730000",1],[0,"1.0714","668700",1],[1,"1.0719","205100",1],[1,"1.072","3172800",1]],false]',
    # book update: level set + delete
    '[1,1210,"8328425028614922983","7087889941297585337",1782345115843612834,1782345115843000000,'
    '[[0,"1.0698","1039100",1],[1,"1.0719","0",1]]]',
    # trades: one fill, buy aggressor
    '[2,1210,"7697470040428040547","6584934061829438687",1782345122476611101,1782345122476000000,'
    '[[0,"1.0719","19200","00000000-006d-1000-0000-0033b50c7876",1782345122476000000]]]',
    # state (special shape)
    '[5,1210,1782345115505070404,"READY","ready after failover"]',
    # 2026-era mark price: arity 14 (7 trailing nulls)
    '[7,1210,"0","7349456101763923293",1782345115505070407,1782345115500000003,"1.07258",'
    "null,null,null,null,null,null,null]",
    # index price
    '[8,1210,"0","8050104725181629121",1782345115505070406,1782345115493190275,"1.07252"]',
    # 2026-era funding: 3 elements
    '[9,1210,"0","3613991425845795139",1782345115505070408,1782345115500253321,'
    '["0.00010",1782360000000000000,"0.000011"]]',
    # 2025-era funding: 2 elements (no predicted rate)
    '[9,67824,"0","681352840083568157",1746575483925798371,1746575483897227167,'
    '["0.00000757",1746576000000000000]]',
    # 2025-era snapshot: arity 7 (no trailing flag)
    '[0,67824,"0","7441981274431",1746575483925798367,1746575483922755942,'
    '[[0,"96806.5","0.26",1],[1,"96806.6","14.799",1]]]',
    # top-of-book (2025 Binance)
    '[6,67824,"0","7441981275789",1746575483925798368,1746575483924000002,'
    '[[0,"96806.5","4.703",1],[1,"96806.6","14.799",1]]]',
    # liquidations, msgType 17 (2026 Bybit SOLUSDT perp): one row, bankruptcy price
    '[17,2591,"0","4369112899956650000",1782864902273483642,1782864902271000000,'
    '[[1,"74.09","5.8",1782864902101000000]]]',
    # mark price with options greeks/vols (2026 OKX ETH call, schema v7)
    '[7,8967995,"1161823613938688246","4380261518099630876",1782431454751616000,1782431454735000000,'
    '"0.0070174072573915","0.3288191987","0.0070104172","0.3430565826","-7.0212090894",'
    '"0.5437670545","0.5444425048","0.5664151171"]',
]


def selftest() -> int:
    """Offline self-check against embedded real fixture lines. Returns 0/1."""
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        if not cond:
            failures.append(what)

    hdr = json.loads(_FIXTURE_HEADER)
    check(hdr["instrument"]["id"] == 1210, "header instrument id")
    mult, linear = contract_scale(hdr)
    check(mult == 0.01 and linear, "contract scale (multiplier 0.01, linear)")

    evs = [parse_event(ln) for ln in _FIXTURE_LINES]
    snap26, upd, trade, state, mark, index, fund26, fund25, snap25, tob, liq, greeks = evs

    check(snap26.msg_type == 0 and len(snap26.data) == 4, "2026 snapshot parses (extra `false` ignored)")
    check(snap26.extras == (False,), "2026 snapshot forceReset lands in extras")
    check(mark.msg_type == 7 and mark.data == "1.07258", "arity-14 mark price parses")
    check(mark.extras == (None,) * 7, "non-option mark: 7 null greeks in extras")
    check(index.data == "1.07252", "index price")
    check(index.extras == () and upd.extras == (), "no-tail events have empty extras")
    check(state.msg_type == 5 and state.data == ("READY", "ready after failover"), "state special shape")
    check(fund26.data[2] == "0.000011", "2026 funding has predicted rate")
    check(len(fund25.data) == 2, "2025 funding has no predicted rate")
    check(snap25.msg_type == 0 and len(snap25.data) == 2, "2025 arity-7 snapshot parses")
    check(snap25.extras == (), "2025 snapshot has no forceReset tail")
    check(tob.msg_type == 6, "top-of-book parses")
    check(upd.prev_event_id == snap26.event_id, "snapshot chains into first update")

    check(liq.msg_type == 17 and len(liq.data) == 1, "liquidation event parses")
    lrow = liq.data[0]
    check(lrow[0] == 1 and float(lrow[1]) == 74.09 and float(lrow[2]) == 5.8, "liquidation row fields")
    check(ts_to_datetime(lrow[3]).year == 2026, "liquidation row timestamp is ns")
    check(liq.prev_event_id is None, 'prevEventId "0" normalizes to None (chain start)')

    m26 = _mark_from_event(greeks)
    check(abs(m26.mark_price - 0.0070174072573915) < 1e-15, "greeks mark price")
    check(m26.delta is not None and 0.0 <= m26.delta <= 1.0, "call delta in [0, 1]")
    check(m26.gamma is not None and m26.vega is not None and m26.theta is not None, "greeks decoded")
    check(
        m26.mark_volatility is not None
        and m26.mark_volatility > 0
        and m26.bid_volatility is not None
        and m26.ask_volatility is not None
        and m26.bid_volatility <= m26.ask_volatility,
        "vols decoded (bid <= ask)",
    )
    check(_mark_from_event(mark) == Mark(mark.adapter_ts, mark.exchange_ts, 1.07258, *(None,) * 7),
          "non-option mark decodes to all-None greeks")

    book = Book()
    book.apply(snap26)
    check(book.best_bid() == (1.0715, 730000.0), "snapshot best bid")
    check(book.best_ask() == (1.0719, 205100.0), "snapshot best ask")
    book.apply(upd)
    check(1.0698 in book.bids, "update inserts level")
    check(1.0719 not in book.asks, 'qty "0" deletes level')
    check(book.best_ask() == (1.072, 3172800.0), "best ask after delete")
    check(not book.suspect and book.gap_count == 0, "chain intact")

    fills = []
    for row in trade.data:
        fills.append(row)
    check(len(fills) == 1 and fills[0][0] == 0, "trade fill flattens, buy aggressor")
    check(ts_to_datetime(trade.adapter_ts).year == 2026, "ns timestamp converts")
    check(parse_ts("2026-06-24T00:00:00Z") == 1782259200 * NS, "ISO timestamp parses")

    deps = []
    for mod in ("zstandard", "polars", "pandas", "pyarrow"):
        try:
            __import__(mod)
            deps.append(f"{mod}: ok")
        except ImportError:
            deps.append(f"{mod}: not installed")
    try:
        subprocess.run(["zstd", "--version"], capture_output=True, check=True)
        deps.append("zstd CLI: ok")
    except (FileNotFoundError, subprocess.CalledProcessError):
        deps.append("zstd CLI: not found")

    print(f"cryptostruct_reader {__version__} selftest: ", end="")
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
    else:
        print("all checks passed")
    print("optional dependencies: " + "; ".join(deps))
    return 1 if failures else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_rows(rows: Iterator[tuple], headers: list[str], out: Optional[str]) -> None:
    """Stream rows to stdout as CSV, or materialize to --out (.csv/.parquet)."""
    import csv

    if out is None or out.endswith(".csv"):
        fh = sys.stdout if out is None else open(out, "w", newline="", encoding="utf-8")
        try:
            w = csv.writer(fh)
            w.writerow(headers)
            for r in rows:
                w.writerow(r)
        finally:
            if fh is not sys.stdout:
                fh.close()
        return
    if out.endswith(".parquet"):
        name, mod = _engine("auto")
        cols = {h: [] for h in headers}
        for r in rows:
            for h, v in zip(headers, r):
                cols[h].append(v)
        df = mod.DataFrame(cols)
        if name == "polars":
            df.write_parquet(out)
        else:
            df.to_parquet(out, index=False)
        return
    raise SystemExit(f"unsupported --out extension: {out} (use .csv or .parquet)")


def _iso(ns: Optional[int]) -> str:
    return ts_to_datetime(ns).isoformat() if ns is not None else ""


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="cryptostruct_reader.py",
        description="Streaming reader for CryptoStruct full-day market-data files (*.txt.zst).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_file_cmd(name: str, help_: str):
        p = sub.add_parser(name, help=help_)
        p.add_argument("file")
        return p

    add_file_cmd("info", "print the masterdata header summary (instant)")
    p = add_file_cmd("stats", "full-pass event counts, coverage, chain gaps")
    p.add_argument("--deep", action="store_true", help="also replay the book (crossed states, levels)")
    p.add_argument("--json", action="store_true")
    p = add_file_cmd("trades", "flattened fills")
    p.add_argument("--out", help=".csv or .parquet (default: CSV to stdout)")
    p = add_file_cmd("book", "book sampled on a fixed grid")
    p.add_argument("--every", default="1s", help="grid step: 250ms, 1s, 5s, 1m, ... (default 1s)")
    p.add_argument("--depth", type=int, default=20)
    p.add_argument("--out", help=".csv or .parquet (default: CSV to stdout)")
    p = add_file_cmd("prices", "mark + index price events")
    p.add_argument("--out", help=".csv or .parquet (default: CSV to stdout)")
    p = add_file_cmd("funding", "funding-rate events")
    p.add_argument("--out", help=".csv or .parquet (default: CSV to stdout)")
    p = add_file_cmd("liquidations", "flattened liquidation rows (msgType 17)")
    p.add_argument("--out", help=".csv or .parquet (default: CSV to stdout)")
    p = add_file_cmd("greeks", "mark price + options greeks/vols (msgType 7)")
    p.add_argument("--out", help=".csv or .parquet (default: CSV to stdout)")
    sub.add_parser("selftest", help="offline format self-check + dependency report")

    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "info":
        hdr = read_header(args.file)
        inst = hdr["instrument"]
        mult, linear = contract_scale(hdr)
        print(f"{inst.get('code')}  ({inst.get('type')}) on {inst.get('exchange_code')}  — instrument_id {inst.get('id')}")
        print(f"  ticksize {inst.get('ticksize')}   lot_size {inst.get('lot_size')}   multiplier {mult}")
        kind = "linear" if linear else ("inverse" if inst.get("is_inverse") else "quanto")
        print(f"  contract: {kind}   contract_value {inst.get('contract_value')}   funding_interval {inst.get('funding_interval')} s")
        for u in hdr.get("underlyings", []):
            print(f"  underlying {u.get('code'):>6}  fx_rate {u.get('fx_rate')}")
        return 0

    if args.cmd == "stats":
        st = file_stats(args.file, deep=args.deep)
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        names = {0: "snapshot", 1: "book_update", 2: "trades", 5: "state", 6: "top_of_book", 7: "mark", 8: "index", 9: "funding", 17: "liquidations"}
        print(f"{st['path']}: {st['events']} events, {st['parse_errors']} parse errors")
        print(f"  coverage {_iso(st['first_adapter_ts'])} → {_iso(st['last_adapter_ts'])}  ({st['coverage_s']} s)")
        for t, n in st["counts"].items():
            print(f"  {t} {names.get(t, '?'):<12} {n}")
        liq = f"   liq rows {st['liquidation_rows']}" if st["counts"].get(17) else ""
        print(f"  fills {st['trades_fills']}{liq}   snapshots {st['snapshots']}   chain gaps: book {st['book_chain_gaps']}, trades {st['trade_chain_gaps']}")
        if args.deep:
            print(f"  crossed states {st['crossed_states']}/{st['book_states_checked']} ({st['crossed_pct']}%)   max levels {st['max_book_levels']}")
            print(f"  final best bid {st['final_best_bid']}   final best ask {st['final_best_ask']}")
        return 0

    if args.cmd == "trades":
        rows = (
            (_iso(t.adapter_ts), _iso(t.exchange_ts), t.side, t.price, t.qty, t.trade_id)
            for t in iter_trades(args.file)
        )
        _write_rows(rows, ["ts", "exchange_ts", "side", "price", "qty", "trade_id"], args.out)
        return 0

    if args.cmd == "book":
        depth = args.depth
        headers = ["ts", "mid", "spread", "spread_bps"]
        for i in range(1, depth + 1):
            headers += [f"bid_px_{i}", f"bid_qty_{i}", f"ask_px_{i}", f"ask_qty_{i}"]
        headers += ["n_events", "suspect"]

        def rows():
            for s in sample_book(args.file, every=args.every, depth=depth):
                bb = s.bids[0][0] if s.bids else None
                ba = s.asks[0][0] if s.asks else None
                mid = (bb + ba) / 2 if bb is not None and ba is not None else None
                spread = ba - bb if bb is not None and ba is not None else None
                r = [_iso(s.ts), mid, spread, spread / mid * 1e4 if spread is not None and mid else None]
                for i in range(depth):
                    b = s.bids[i] if len(s.bids) > i else (None, None)
                    a = s.asks[i] if len(s.asks) > i else (None, None)
                    r += [b[0], b[1], a[0], a[1]]
                r += [s.n_events, int(s.suspect)]
                yield r

        _write_rows(rows(), headers, args.out)
        return 0

    if args.cmd == "prices":
        rows = (
            (_iso(ev.adapter_ts), {7: "mark", 8: "index"}[ev.msg_type], float(ev.data))
            for ev in iter_events(args.file, msg_types=(7, 8))
        )
        _write_rows(rows, ["ts", "kind", "price"], args.out)
        return 0

    if args.cmd == "funding":
        def frows():
            for ev in iter_events(args.file, msg_types=(9,)):
                d = ev.data
                pr = d[2] if len(d) > 2 else None
                yield (
                    _iso(ev.adapter_ts),
                    float(d[0]) if d[0] is not None else None,
                    _iso(d[1] if len(d) > 1 and d[1] else None),
                    float(pr) if pr is not None else None,
                )

        _write_rows(frows(), ["ts", "rate", "next_funding_ts", "predicted_rate"], args.out)
        return 0

    if args.cmd == "liquidations":
        rows = (
            (_iso(l.adapter_ts), _iso(l.exchange_ts), l.side, l.price, l.qty)
            for l in iter_liquidations(args.file)
        )
        _write_rows(rows, ["ts", "exchange_ts", "side", "price", "qty"], args.out)
        return 0

    if args.cmd == "greeks":
        rows = (
            (
                _iso(m.adapter_ts), _iso(m.exchange_ts), m.mark_price, m.delta, m.gamma,
                m.vega, m.theta, m.mark_volatility, m.bid_volatility, m.ask_volatility,
            )
            for m in iter_marks(args.file)
        )
        _write_rows(
            rows,
            ["ts", "exchange_ts", "mark_price", "delta", "gamma", "vega", "theta",
             "mark_volatility", "bid_volatility", "ask_volatility"],
            args.out,
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
