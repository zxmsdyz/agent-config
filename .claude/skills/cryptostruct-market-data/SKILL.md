---
name: cryptostruct-market-data
description: >-
  Parse and analyze CryptoStruct crypto market data. Covers (1) full-day tick
  files named {instrument_id}_{YYYY-MM-DD}.txt.zst — zstd-compressed JSON-lines
  with orderbook snapshots and updates, trades, liquidation events, mark/index
  prices incl. options greeks and implied vols, and funding — and (2) the
  cryptostruct.com minute-aggregate analyze API (OHLCV, spreads, depth,
  liquidations; CSV or JSON). Use whenever the user mentions CryptoStruct, a
  .txt.zst market data file, orderbook reconstruction from tick data, or
  cryptostruct.com API data. Provides exact message formats, the instrument
  masterdata header, units and notional math (multiplier, is_inverse),
  streaming parsers safe for multi-GB files, and ready-to-run Python tooling
  in scripts/.
license: MIT
metadata:
  version: "1.1"
  source: https://cryptostruct.com/download
---

# CryptoStruct market data

## The two data products

1. **Full-day tick files** — `{instrument_id}_{YYYY-MM-DD}.txt.zst` (shop
   downloads arrive as `{exchange_code}-{code}-{date}.txt.zst`). Every market
   event of one instrument for one UTC day: L2 order-book snapshots +
   incremental updates, trades, liquidations, mark/index prices (options:
   greeks + implied vols), funding. Free samples:
   <https://cryptostruct.com/download>. Full archive (any instrument, any
   day): <https://cryptostruct.com/shop>; purchased files:
   <https://cryptostruct.com/account>.
2. **Minute statistics API** — public, no auth:
   `https://cryptostruct.com/api/analyze/instrument-last-24h-minutes/{id}?format=csv`
   (rolling 24 h; JSON without the `format` param) and siblings for single
   days and daily windows.

**Which to use:** per-minute/hour/day statistics (OHLCV, turnover, spreads,
depth averages, liquidation counts) → the API. Anything intra-minute — book
states, event-level spreads, trade-by-trade, order-flow, individual
liquidation events, options greeks — → the tick files.

## Quickstart

```bash
# Tick file → what's inside / trades / sampled book (no dependencies needed)
python3 scripts/cryptostruct_reader.py info   1210_2026-06-24.txt.zst
python3 scripts/cryptostruct_reader.py stats  1210_2026-06-24.txt.zst --deep
python3 scripts/cryptostruct_reader.py trades 1210_2026-06-24.txt.zst --out trades.parquet
python3 scripts/cryptostruct_reader.py book   1210_2026-06-24.txt.zst --every 1s --depth 20 --out book_1s.parquet
```

```bash
# Minute API → CSV
curl -s 'https://cryptostruct.com/api/analyze/instrument-last-24h-minutes/67824?format=csv' -o minutes.csv
python3 scripts/fetch_minutes.py --search btcusdt --venue binance_swap   # find instrument ids
```

```python
import sys; sys.path.insert(0, "scripts")
from cryptostruct_reader import read_header, trades_frame, book_at
hdr  = read_header("1210_2026-06-24.txt.zst")     # instrument masterdata (line 1)
df   = trades_frame("1210_2026-06-24.txt.zst")    # polars if installed, else pandas
book = book_at("1210_2026-06-24.txt.zst", "2026-06-24T14:30:00Z")
```

## Tick-file format in brief

zstd-compressed text. **Line 1** = JSON object
`{"instrument": {...}, "underlyings": [...]}` — self-contained masterdata
(symbol, venue, `ticksize`, `lot_size`, `multiplier`, `is_inverse`,
`is_quanto`, `contract_value`, `funding_interval`, per-underlying USD
`fx_rate`). **Every other line** = one event:

```
[msgType, instrumentId, prevEventId, eventId, adapterTs, exchangeTs, data, ...extras]
```

| msgType | Meaning | `data` |
|---|---|---|
| 0 | Book **snapshot** — full state, **reset your book** | `[[side, price, qty, ordercount], …]` |
| 1 | Book **update** — absolute level replace; qty `"0"` deletes | same shape |
| 2 | **Trades** — side = aggressor; one event batches many fills | `[[side, price, qty, tradeId, exchangeTradeTs], …]` |
| 5 | Instrument **state** — DIFFERENT shape: `[5, id, adapterTs, state, message]` | `READY`/`ERROR` |
| 6 | Top-of-book (separate BBO stream; venue-dependent) | up to 2 levels |
| 7 / 8 | Mark / index price — mark carries options greeks + vols (null otherwise) | price string (+7 tail fields on mark) |
| 9 | Funding | `[rate, nextFundingTs, predictedRate?]` |
| 17 | **Liquidations** — own stream; price = bankruptcy price; some venues sample | `[[side, price, qty, exchangeLiquidationTs], …]` |

`side`: 0 = bid/buy, 1 = ask/sell. Files cover the UTC day **plus margins**
(~8 min before midnight, ~1 min after) — consecutive days overlap; trim or
dedupe when stitching.

**Five iron rules**

1. **Stream, never slurp.** Files decompress to 4–8× their size; flagship
   days are 1–2 GB compressed. Read line-by-line through a zstd stream.
2. **Prices/quantities are decimal strings** — convert explicitly; keep the
   raw strings when exactness matters.
3. **Timestamps are integer nanoseconds since epoch, UTC.** `adapterTs` =
   recorder receive time (always present — use for sequencing); `exchangeTs`
   = venue time (`0` = unavailable).
4. **Parse positionally and tolerate trailing elements.** Known tails:
   snapshots end with a `forceReset` boolean (schema v6), mark prices carry
   7 greek/vol fields (schema v7 — real values for options, nulls otherwise);
   ignore anything beyond the fields you know.
5. **Reset the book at every snapshot** (msgType 0). Several per day are
   normal (failover/reconnect).

Full reference (header glossary, per-message semantics with real sample
lines, chains, notional math, offline parse fixture):
`references/day-file-format.md`.

## Minute API in brief

22 CSV columns, verbatim header:

```
time,open,high,low,close,vwap,trades,trades_buy,trades_sell,trades_liquidation,turnover_usd,turnover_buy_usd,turnover_sell_usd,spread_avg,spread_bps,spread_ticks,top1_bid_usd,top1_ask_usd,top20_bid_usd,top20_ask_usd,top1_bid_qty,top1_ask_qty
```

Groups: OHLC+VWAP (trade prices, quote currency) · trade counts by aggressor
side incl. liquidations · USD turnover with buy/sell split · time-averaged
spread in three units (quote, bps, ticks) · time-averaged resting depth
(level-1 and top-20 totals in USD, level-1 size in base units).

Row semantics: `time` = minute start (ISO-8601 UTC). Zero-trade minutes have
**empty price cells** but counts `0` and populated book columns. Minutes can
be **missing entirely** (no zero-fill). JSON uses short keys (`t`, `o`, `usd`,
`top1_bid`, …) — mapping table and endpoint catalog:
`references/minute-api.md`.

## Critical pitfalls (read before computing anything)

- `top20_bid_usd` / `top20_ask_usd` are **already the TOTAL notional of the
  top 20 levels**. Do NOT multiply by 20 again and do NOT sum them with other
  level columns. Per-level average = value ÷ 20.
- All book-derived minute columns (`spread_*`, `top*`) are **intra-minute
  time-averages**, not closing snapshots.
- In tick book updates, qty `"0"` means **delete that price level**; any other
  qty **replaces** the level absolutely (never a delta).
- Trade `side` is the **aggressor**: 0 = buy (taker lifted the ask), 1 = sell.
  Same 0/1 = bid/ask convention as book rows, and identical to the API's
  `trades_buy`/`trades_sell` (verified against production data).
- Tick trade rows carry **no per-fill liquidation flag** — liquidations are
  their own events (msgType 17; **bankruptcy price**, not a trade price; some
  venues only publish samples). The API's `trades_liquidation` stays the
  aggregated count (a subset of `trades`); expect ≈, not ==, between the two.
- **Notional math:** event quantities are venue-native contract quantities.
  Linear instruments: `base = qty × multiplier`, `quote = qty × multiplier ×
  price`. Real example: BitMEX XRPUSDT has `multiplier 0.01` — a qty of
  `901800` at 1.1091 is ≈ 9,018 XRP ≈ 10,003 USDT, *not* 901,800 XRP. Check
  `is_inverse`/`is_quanto`/`contract_value` in the header before assuming.
- API `turnover_*` is **USD**; tick-file math yields the **native quote
  currency**. Reconcile via the header underlying's `fx_rate` (a static
  snapshot at `ts_fx`) — expect ≈, not ==.
- Use `adapterTs` for ordering and as-of joins; `exchangeTs` may be `0`. For
  per-fill times use `exchangeTradeTs` inside trade batches; when comparing
  with API minutes, bucket fills by `exchangeTradeTs` (verified to match).
- **Never assume event ids increment** — they are sequences on some venues,
  hashes on others. Detect gaps only via `prevEventId == last eventId of the
  same stream`; after a gap, distrust the book until the next snapshot.
- msgType 5 lines have a **different shape** — handle before generic parsing.
- Don't feed msgType 6 (top-of-book) into your depth book — separate stream.
- JSON and CSV use **different key names** (`t`→`time`, `usd`→`turnover_usd`,
  `top1_bid`→`top1_bid_usd`, …) — map via `references/minute-api.md`.

## Bundled tooling

`scripts/cryptostruct_reader.py` — streaming tick-file reader, no required
dependencies (uses the `zstandard` module when present, else the `zstd` CLI).

- Library: `read_header`, `iter_events`, `iter_trades`, `iter_liquidations`,
  `iter_marks`, `Book`, `book_at`, `sample_book`, `trades_frame`,
  `book_frame`, `prices_frame`, `funding_frame`, `liquidations_frame`,
  `greeks_frame`, `file_stats`, `contract_scale` (signatures and column docs
  in the file).
- CLI: `info` · `stats [--deep]` · `trades` · `book --every 1s --depth 20` ·
  `prices` · `funding` · `liquidations` · `greeks` — each with
  `--out x.csv|x.parquet` (default CSV to stdout) — and `selftest` (offline
  format check + dependency report).

`scripts/fetch_minutes.py` — minute-API client (stdlib urllib):
`--search QUERY [--venue V --class C]` to resolve instrument ids;
`ID [--date YYYY-MM-DD] [--out x.csv | --json]` to fetch rows with canonical
CSV column names regardless of endpoint.

Dependency policy: **stdlib always works**. `pip install zstandard`
recommended (faster than the subprocess fallback); `polars` (preferred) or
`pandas` (+ `pyarrow`) only for DataFrame/Parquet helpers.

## Recipes index (`references/recipes.md`)

1. One day of trades as a DataFrame
2. Exact order book at time T
3. Top-of-book/depth on a fixed grid → Parquet
4. Liquidity within N bps of mid over time
5. Trade-flow imbalance and CVD
6. Liquidation scan (tick events + API cross-check)
7. Funding and basis series
8. Join tick series with API minutes (+ reconciliation)
9. Multi-day batch processing (overlap-safe)
10. Audit a day file before research
11. Options greeks / implied-vol series

## Finding instrument IDs

- Tick-file name prefix (`1210_…` → id 1210) or header `instrument.id`.
- Search API: `https://cryptostruct.com/api/search?q=btcusdt&venue=binance_swap&class=perpetual`
  (`q` = code only; venue/class/base are separate params; `class=prediction`
  selects Kalshi/Polymarket event contracts — 0..1 probability prices), or
  `python3 scripts/fetch_minutes.py --search btcusdt --venue binance_swap`.
- IDs are stable for an instrument's lifetime and identical across tick
  files, the API, and the shop.

Everything except the API works fully offline. Canonical URLs: samples & docs
<https://cryptostruct.com/download> · archive <https://cryptostruct.com/shop>
· spec
<https://docs.cryptostruct.com/market-data-api/protocol/>.

## References

- `references/day-file-format.md` — complete tick-file reference: header
  glossary, per-message tables with real sample lines, event chains, notional
  math, offline parse fixture.
- `references/minute-api.md` — API endpoint catalog, 22-column contract,
  JSON↔CSV key map, loading snippets.
- `references/recipes.md` — the eleven recipes above, copy-paste runnable.
