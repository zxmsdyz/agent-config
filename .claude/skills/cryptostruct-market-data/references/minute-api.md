# CryptoStruct minute-statistics API — reference

Public, no-auth endpoints on `https://cryptostruct.com` serving per-minute
(and daily) statistics derived from the same recordings the tick files
contain. Data updates once per minute and responses are server-cached (~60 s)
— **do not poll faster than once per minute per instrument.**

Quick pull:

```bash
curl -s 'https://cryptostruct.com/api/analyze/instrument-last-24h-minutes/67824?format=csv' -o minutes.csv
```

or with the bundled client (`scripts/fetch_minutes.py` — normalizes JSON to
the same 22 columns, adds `--date` access to single days):

```bash
python3 scripts/fetch_minutes.py --search btcusdt --venue binance_swap
python3 scripts/fetch_minutes.py 67824 --out minutes.csv
python3 scripts/fetch_minutes.py 1210 --date 2026-06-28 --out day.csv
```

## 1. The per-minute row: 22-column contract

CSV column order (the `?format=csv` header, verbatim):

```
time,open,high,low,close,vwap,trades,trades_buy,trades_sell,trades_liquidation,turnover_usd,turnover_buy_usd,turnover_sell_usd,spread_avg,spread_bps,spread_ticks,top1_bid_usd,top1_ask_usd,top20_bid_usd,top20_ask_usd,top1_bid_qty,top1_ask_qty
```

| Column | Unit / meaning | Empty when |
|---|---|---|
| `time` | minute **start**, ISO-8601 UTC (`2026-06-28T08:20:00Z`) | never |
| `open`, `high`, `low`, `close` | trade prices, quote currency | no trades in the minute |
| `vwap` | turnover-weighted mean trade price | no trades in the minute |
| `trades` | number of trades in the minute | never (0) |
| `trades_buy` / `trades_sell` | trade counts by **aggressor** side | never (0) |
| `trades_liquidation` | liquidation trades — a **subset** of `trades`, already counted there | never (0) |
| `turnover_usd` | traded notional, **USD-converted** | never (0) |
| `turnover_buy_usd` / `turnover_sell_usd` | aggressor-side split of turnover | never (0) |
| `spread_avg` | time-averaged bid/ask spread, quote units | book unavailable |
| `spread_bps` | time-averaged spread in basis points of mid | book unavailable |
| `spread_ticks` | time-averaged spread ÷ instrument `ticksize` | book unavailable |
| `top1_bid_usd` / `top1_ask_usd` | time-averaged USD notional resting at level 1 | book unavailable |
| `top20_bid_usd` / `top20_ask_usd` | time-averaged **TOTAL USD notional across the top 20 levels** | book unavailable |
| `top1_bid_qty` / `top1_ask_qty` | time-averaged level-1 size, **base-asset units** | book unavailable |

### The four rules

1. **Never rescale `top20_*`.** The ×20 (avg-per-level → total across 20
   levels) is already applied. Per-level average = value ÷ 20.
2. **Zero-trade minutes** keep counts `0` and keep the book columns populated
   (the resting book exists without trades); only the price columns
   (`open…close`, `vwap`) are empty.
3. **Minutes can be missing entirely** — the series skips them (no zero-fill).
   Reindex to a full minute grid when you need gaps explicit. Responses may
   carry a `note` like `"Partial · N of 1440 minutes available"`.
4. **All spread/depth columns are intra-minute time-averages**, not
   end-of-minute snapshots.

Cross-checked against raw tick files (verified): per-minute `trades` /
`trades_buy` / `trades_sell` equal a flatten-and-count of tick-file trade
batches bucketed by the fill's matching-engine timestamp, and
`turnover_usd ÷ (native quote turnover)` ≈ the counter currency's USD rate.
`buy`/`sell` = taker side, identical to the tick files' trade `side` 0/1.

## 2. Formats: CSV vs JSON

- `?format=csv`: the 22 columns above; header always present (even when
  empty); RFC-4180, CRLF line endings; nulls = empty cells. Served as an
  attachment named `{exchange}_{code}_1m_last24h_{date}.csv` (free 24h
  endpoint) or `{exchange}_{code}_1m_{date}.csv` (day endpoint, Premium).
- Default JSON: envelope
  `{instrument_id, start, end, minutes: [...], needs_upstream, note?, generated_at}`
  (day endpoint: `date` instead of `start`/`end`). `needs_upstream: true` with
  empty `minutes` = upstream temporarily unavailable — retry later.
- **JSON rows use short keys.** Map to CSV names:

| JSON | CSV | | JSON | CSV |
|---|---|---|---|---|
| `t` | `time` | | `spread_avg` | `spread_avg` |
| `o` `h` `l` `c` | `open` `high` `low` `close` | | `spread_bps` | `spread_bps` |
| `vwap` | `vwap` | | `spread_ticks` | `spread_ticks` |
| `trades` | `trades` | | `top1_bid` | `top1_bid_usd` |
| `trades_buy` | `trades_buy` | | `top1_ask` | `top1_ask_usd` |
| `trades_sell` | `trades_sell` | | `top20_bid` | `top20_bid_usd` |
| `trades_liquidation` | `trades_liquidation` | | `top20_ask` | `top20_ask_usd` |
| `usd` | `turnover_usd` | | `top1_bid_qty` | `top1_bid_qty` |
| `usd_buy` | `turnover_buy_usd` | | `top1_ask_qty` | `top1_ask_qty` |
| `usd_sell` | `turnover_sell_usd` | | | |

JSON additionally carries `max_no_trade_s`, `errors`, `latency_top_ms`,
`latency_trades_ms` — recording-health telemetry, not market data; generally
ignore.

## 3. Loading

```python
import pandas as pd
df = pd.read_csv("minutes.csv", parse_dates=["time"])   # empty cells -> NaN
```

```python
import polars as pl
df = pl.read_csv("minutes.csv", try_parse_dates=True)   # empty cells -> null
```

```python
from fetch_minutes import fetch_minutes            # scripts/fetch_minutes.py
rows = fetch_minutes(67824)                        # last 24 h, CSV-named keys
rows = fetch_minutes(1210, date="2026-06-28")      # one UTC day
```

## 4. Finding instrument IDs

`GET /api/search` — `q` matches the instrument code; the venue is a
**separate parameter** (do not put it into `q`):

```
https://cryptostruct.com/api/search?q=btcusdt&venue=binance_swap&class=perpetual&limit=10
```

| Param | Meaning |
|---|---|
| `q` | code query, e.g. `btcusdt`, `xrp` |
| `venue` | exchange code: `binance_swap`, `binance_spot`, `bitmex`, `bybit`, `okx`, … |
| `class` | `spot`, `perpetual`, `future`, `call`, `put`, `prediction` (Kalshi/Polymarket event contracts, prices 0..1, turnover USD-normalized) |
| `base` | base asset, e.g. `BTC` |
| `limit`, `offset` | paging (limit ≤ 100) |

Response: `{total, results: [{instrument_id, code, type, exchange: {code,
name}, base_underlying, counter_underlying, state, data: {days, first_day,
last_day, total_bytes}}]}`. `data.total_bytes` is the compressed archive size
across all recorded days. IDs are stable for an instrument's lifetime and
match tick-file names and shop listings.

## 5. Endpoint catalog

All analyze endpoints are JSON; `format=csv` exists on
`instrument-last-24h-minutes` (free) and `instrument-day-minutes` (Premium —
requires an `Authorization: Bearer` token of a Premium account; tokenless
requests get `403 PREMIUM_REQUIRED`).

| Endpoint | Granularity | Window | Status |
|---|---|---|---|
| `/api/analyze/instrument-last-24h-minutes/{id}` | minute | rolling 1440 min | confirmed, CSV+JSON |
| `/api/analyze/instrument-day-minutes/{id}?date=YYYY-MM-DD` | minute | one UTC day | confirmed, JSON free · CSV Premium |
| `/api/analyze/instrument-daily/{id}?window=30d\|90d\|180d` (or `from`/`to`) | day | up to 180 d | confirmed, JSON |
| `/api/analyze/instrument-liquidity-profile/{id}` | profile | — | JSON |
| further `/api/analyze/*` (hourly, intraday, exchange-level, movers, …) | — | — | JSON, subject to change |

### `/api/analyze/instrument-last-24h-minutes/{id}`

Rolling trailing-1440-minute window ending at the current minute. Params:
`format=csv` (optional). Recent minutes may still be filling — treat the last
1–2 rows as provisional.

### `/api/analyze/instrument-day-minutes/{id}?date=YYYY-MM-DD`

One UTC calendar day, `[00:00, 24:00)`, same row shape. JSON is free;
`format=csv` is Premium (empty days return a header-only CSV). Tokenless
agents get the identical rows from the free JSON — `fetch_minutes.py --date`
writes a byte-equivalent 22-column CSV locally. Retention of the minute
backend is limited (~30 days) — very old dates can return an empty series
(`needs_upstream` false/true with note); the tick files are the permanent
record. Days older than the retention are purchasable as archived 1-minute
CSVs in the Data Shop (€1 per instrument-day, same 22-column format):
https://cryptostruct.com/shop — agents can buy them via the MCP
`create_checkout` tool with `kind: "minute_csv"`.

### `/api/analyze/instrument-daily/{id}`

Daily OHLC/turnover rows: `{date, o, h, l, c, vwap, trades, usd, delta_pct}`
plus a summary block. Params: `window=30d|90d|180d` or explicit
`from`/`to` (`YYYY-MM-DD`).

<!-- ENDPOINT SECTIONS: append below, one ### block per endpoint -->

---

*Companion references: [`day-file-format.md`](day-file-format.md) (raw tick
files), [`recipes.md`](recipes.md) (runnable analysis code).*
