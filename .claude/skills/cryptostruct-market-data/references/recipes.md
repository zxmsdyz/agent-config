# Analysis recipes — CryptoStruct market data

Runnable code for common quant tasks on the two data products. All recipes
were validated against real production files.

**Engine rule of thumb:** below ~100k rows (one day of trades on most
instruments, 1440 API minutes) pandas and polars are both fine; at
book-update scale (millions of events) use **polars**, ideally after a
one-time conversion to Parquet. Every frame helper in
`scripts/cryptostruct_reader.py` accepts `engine="polars" | "pandas" | "auto"`
(auto prefers polars). The CLI needs no DataFrame library at all when writing
CSV.

Setup used by the Python snippets below (adjust the path to where this skill
lives):

```python
import sys
sys.path.insert(0, "scripts")          # <skill-dir>/scripts
from cryptostruct_reader import (
    read_header, iter_events, iter_trades, iter_liquidations, iter_marks,
    trades_frame, book_at, sample_book, book_frame, prices_frame,
    funding_frame, liquidations_frame, greeks_frame, file_stats,
    contract_scale, ts_to_datetime,
)
FILE = "1210_2026-06-28.txt.zst"       # any CryptoStruct day file
```

---

## 1. One day of trades as a DataFrame

```python
df = trades_frame(FILE)                # polars if installed, else pandas
print(df.head())
```

Columns: `ts` (recorder receive time, datetime UTC), `exchange_ts` (per-fill
matching-engine time, may be null), `side` (0 = buy aggressor, 1 = sell
aggressor), `price`, `qty` (venue-native), `base_qty`, `quote_value` (both
null unless the instrument is linear — see the header's
`is_inverse`/`is_quanto`), `trade_id`.

CLI equivalent (no Python session needed):

```bash
python3 scripts/cryptostruct_reader.py trades FILE --out trades.parquet   # or .csv
```

Caveats: `quote_value` is in the instrument's **native quote currency**
(e.g. USDT), not USD — see recipe 8 for the USD reconciliation. One trade
event can contain many fills; this frame is already flattened.

## 2. Exact order book at time T

```python
book = book_at(FILE, "2026-06-28T14:30:00Z")
print(book.best_bid(), book.best_ask(), book.mid(), book.spread())
bids, asks = book.top(10)              # [(price, qty), ...] best-first
print(f"suspect={book.suspect} (chain gap since last snapshot)")
```

Caveats: replays from the start of the file (~seconds to ~minutes depending
on file size). For many timestamps use recipe 3 once instead of repeated
`book_at` calls. Quantities are venue-native — multiply by the header
`multiplier` for base units on linear instruments.

## 3. Top-of-book / depth on a fixed time grid

```bash
python3 scripts/cryptostruct_reader.py book FILE --every 1s --depth 20 --out book_1s.parquet
```

```python
import polars as pl
book = pl.read_parquet("book_1s.parquet")
# ready-made: ts, mid, spread, spread_bps, bid_px_1..20, bid_qty_1..20,
#             ask_px_1..20, ask_qty_1..20, n_events, suspect
print(book.select("ts", "mid", "spread_bps", "n_events").describe())
```

Grid boundaries are epoch-aligned; each row is the last book state at or
before the boundary. Caveats: check the `suspect` column (event-chain gap
since the last snapshot) before microstructure conclusions; `n_events` = book
events since the previous sample (0 = quiet second, book unchanged).

## 4. Liquidity within N bps of mid, over time

USD-notional depth near the touch — the "how much can I trade without moving
the price" series:

```python
N_BPS = 10
mult, linear = contract_scale(read_header(FILE))
rows = []
for s in sample_book(FILE, every="1m", depth=20):
    if not s.bids or not s.asks:
        continue
    mid = (s.bids[0][0] + s.asks[0][0]) / 2
    lo, hi = mid * (1 - N_BPS / 1e4), mid * (1 + N_BPS / 1e4)
    bid_val = sum(px * qty * mult for px, qty in s.bids if px >= lo)
    ask_val = sum(px * qty * mult for px, qty in s.asks if px <= hi)
    rows.append({"ts": ts_to_datetime(s.ts), "mid": mid,
                 "bid_depth_quote": bid_val, "ask_depth_quote": ask_val,
                 "suspect": s.suspect})

import polars as pl
depth = pl.DataFrame(rows)
```

Caveats: linear instruments only as written (`px × qty × multiplier` = quote
value); values are native-quote (≈ USD for USDT-quoted pairs). Depth beyond
level 20 is invisible here — raise `depth=` if you need more.

## 5. Trade-flow imbalance and CVD

Signed aggressor flow per minute + cumulative volume delta:

```python
import polars as pl
df = trades_frame(FILE, engine="polars")
flow = (
    df.with_columns(
        signed=pl.when(pl.col("side") == 0).then(pl.col("quote_value"))
                 .otherwise(-pl.col("quote_value")))
    .sort("ts")
    .group_by_dynamic("ts", every="1m")
    .agg(
        buy_value=pl.col("quote_value").filter(pl.col("side") == 0).sum(),
        sell_value=pl.col("quote_value").filter(pl.col("side") == 1).sum(),
        net_flow=pl.col("signed").sum(),
        trades=pl.len(),
    )
    .with_columns(cvd=pl.col("net_flow").cum_sum())
)
```

pandas variant: `df.set_index("ts").groupby(pd.Grouper(freq="1min"))` with the
same aggregations. Caveat: the aggressor convention here (0 = buy) is
identical to the minute API's `trades_buy`/`trades_sell` (verified), so this
series is directly comparable to API data.

## 6. Liquidation scan (tick events + API cross-check)

2026-era tick files carry liquidations as **their own events** (msgType 17) —
per-row side, bankruptcy price, quantity and exchange timestamp:

```python
liq = liquidations_frame(FILE, engine="polars")   # ts, exchange_ts, side, price, qty, base_qty, quote_value
# cluster into events of forced flow per minute
import polars as pl
clusters = (
    liq.sort("ts")
       .group_by_dynamic("ts", every="1m")
       .agg(rows=pl.len(), qty=pl.col("qty").sum(),
            sells=(pl.col("side") == 1).sum())
       .filter(pl.col("rows") > 0)
)
```

CLI equivalent: `python3 scripts/cryptostruct_reader.py liquidations FILE --out liq.csv`.

Cross-check / pre-2026 files — the minute API's `trades_liquidation` count:

```python
sys.path.insert(0, "scripts")
from fetch_minutes import fetch_minutes
import polars as pl

rows = fetch_minutes(67824)            # or fetch_minutes(id, date="YYYY-MM-DD")
m = pl.DataFrame(rows).with_columns(pl.col("time").str.to_datetime())
liq_api = (
    m.filter(pl.col("trades_liquidation") > 0)
     .with_columns(ret_next=(pl.col("close").shift(-1) / pl.col("close") - 1) * 1e4)
     .select("time", "trades_liquidation", "trades", "turnover_usd", "close", "ret_next")
)
```

Caveats: liquidation `price` is the **bankruptcy price** — never mix it into
trade/VWAP series. Some venues only publish **samples**, so tick-side counts
are a lower bound and match the API's `trades_liquidation` only
approximately (the API counts liquidation *trades*, type 17 counts
liquidation *orders*). Files recorded before 2026 have no type-17 events —
there the API is the only source.

## 7. Funding and basis series

```python
import polars as pl
hdr = read_header(FILE)
interval_s = int(hdr["instrument"]["funding_interval"])      # e.g. 28800 = 8h

f = (funding_frame(FILE, engine="polars")
     .unique(subset="next_funding_ts", keep="first")          # republications -> one row per interval
     .with_columns(annualized=pl.col("rate") * (365 * 24 * 3600 / interval_s)))

p = prices_frame(FILE, engine="polars")                       # ts, kind, price (mark/index)
basis = (
    p.sort("ts")
     .group_by_dynamic("ts", every="1m", group_by="kind").agg(pl.col("price").last())
     .pivot(values="price", index="ts", on="kind")
     .with_columns(basis_bps=(pl.col("mark") / pl.col("index") - 1) * 1e4)
)
```

Caveats: funding events are periodic republications of current state, not
payments — the `unique(subset="next_funding_ts")` dedupe is essential.
`predicted_rate` is null in pre-2026 files.

## 8. Join tick-derived series with API minutes (and reconcile)

```python
import polars as pl
from fetch_minutes import fetch_minutes

df = trades_frame(FILE, engine="polars")
tick_min = (
    df.drop_nulls("exchange_ts")                   # bucket by matching-engine time
      .with_columns(time=pl.col("exchange_ts").dt.truncate("1m"))
      .group_by("time")
      .agg(n=pl.len(),
           n_buy=(pl.col("side") == 0).sum(),
           quote=pl.col("quote_value").sum())
)
api = (pl.DataFrame(fetch_minutes(1210, date="2026-06-28"))
         .with_columns(time=pl.col("time").str.to_datetime(time_zone="UTC")))
joined = api.join(tick_min, on="time", how="left")

check = joined.select(
    count_match=(pl.col("trades") == pl.col("n")).mean(),
    usd_per_quote=(pl.col("turnover_usd") / pl.col("quote")).median(),
)
print(check)   # verified: count_match > 0.99, usd_per_quote ≈ counter fx (~0.9986 for USDT)
```

Caveats: bucket tick fills by `exchange_ts` (matching-engine clock — this is
what the upstream aggregator uses; verified 559/562 exact minute matches).
`turnover_usd ÷ native quote` reproduces the USD conversion only
approximately (the API uses continuous FX; the header `fx_rate` is a
snapshot).

## 9. Multi-day batch processing

Convert once per day, then lazy-scan the directory:

```bash
for f in data/1210_2026-06-*.txt.zst; do
  out="parquet/trades_$(basename "$f" .txt.zst).parquet"
  [ -f "$out" ] || python3 scripts/cryptostruct_reader.py trades "$f" --out "$out"
done
```

```python
import polars as pl
lf = pl.scan_parquet("parquet/trades_*.parquet")

# IMPORTANT: day files include ~8 min of the previous day and ~1 min of the
# next day (verified) — consecutive files OVERLAP. Trim each day to its
# nominal window before concatenating, or dedupe:
lf = lf.unique(subset=["trade_id", "exchange_ts"])          # dedupe variant
daily_flow = (
    lf.with_columns(day=pl.col("ts").dt.date())
      .group_by("day")
      .agg(quote=pl.col("quote_value").sum(), trades=pl.len())
      .sort("day")
      .collect()
)
```

Caveats: days are independent — parallelize the conversion loop with
`xargs -P`. Book replay (recipes 2/3) must always run per file from the
file's beginning; never concatenate raw event streams across files without
handling the overlap.

## 10. Audit a day file before research

```bash
python3 scripts/cryptostruct_reader.py stats FILE --deep
```

Interpretation guide (a healthy file, from a validated real example):

```
1870832 events, 0 parse errors
coverage 2026-06-23T23:51:55 → 2026-06-25T00:00:59  (86944 s)   # day + margins: normal
0 snapshot ... 3                                                 # >1 snapshot: failover(s), fine
fills 3423   snapshots 3   chain gaps: book 0, trades 0
crossed states 0/1836542 (0.0%)   max levels 1247
```

Rules of thumb before drawing microstructure conclusions:

- `parse_errors > 0` — file damaged in transit; re-download.
- `book chain gaps > 0` — inspect where (samples flagged `suspect`); the book
  is unreliable between a gap and the next snapshot.
- `crossed states > 0.1%` — treat spread/depth stats with suspicion.
- coverage far below ~86,400 s — recording gap that day; check the shop
  calendar or the minute API's `note` for that date.

## 11. Options greeks / implied-vol series

Mark-price events on option instruments carry greeks and implied vols
(schema v7; all-null on non-options and pre-2026 files):

```python
import polars as pl
gk = greeks_frame("8967995_2026-06-26.txt.zst", engine="polars")   # an OKX ETH call
iv = (
    gk.drop_nulls("delta")                       # initial events can be null-greeks
      .sort("ts")
      .group_by_dynamic("ts", every="1m")
      .agg(mark=pl.col("mark_price").last(),
           delta=pl.col("delta").last(),
           iv_mark=pl.col("mark_volatility").last(),
           iv_spread=(pl.col("ask_volatility") - pl.col("bid_volatility")).last())
)
```

CLI equivalent: `python3 scripts/cryptostruct_reader.py greeks FILE --out greeks.parquet`.

Caveats: vols are annualized implied vols as fractions (0.54 = 54%). On
coin-settled venues (e.g. OKX) the option **mark price is in the base
currency** (ETH) while the **index price is USD** — check `is_inverse` and
the underlyings before combining them. Delta of a call lies in [0, 1], puts
in [-1, 0]. Find option instrument ids via the search API
(`class=call|put`).

*Format details: [`day-file-format.md`](day-file-format.md) · API columns:
[`minute-api.md`](minute-api.md). Free sample files to try everything on:
<https://cryptostruct.com/download>.*
