# CryptoStruct full-day tick file — format reference

Complete reference for the per-instrument, per-day recording files sold in the
[CryptoStruct Data Shop](https://cryptostruct.com/shop) and offered as
[free samples](https://cryptostruct.com/download). Distilled from the official
[Market Data Specification](https://docs.cryptostruct.com/market-data-api/protocol/)
(version 3.88.0, protocol schema v7) and verified against production files
from multiple recording eras (2025 and 2026). Where the two disagree, this
document describes what real files contain.

## 1. File identity

- One file = **one instrument × one UTC calendar day, plus a small margin**.
  Verified on production files: recordings start ~8 minutes *before* midnight
  (e.g. `23:51:55` of the previous day) and end ~1 minute *after* the next
  midnight — so **consecutive day files overlap**. When stitching multiple
  days, trim each file to its nominal `[00:00, 24:00)` UTC window (by
  `adapterTs`) or deduplicate on `eventId`.
- Names you will encounter (identical inner format):
  - `{instrument_id}_{YYYY-MM-DD}.txt.zst` — canonical (e.g. `1210_2026-06-24.txt.zst`)
  - `{exchange_code}-{code}-{YYYY-MM-DD}.txt.zst` — as delivered by shop downloads (e.g. `binance_swap-BTCUSDT-2026-06-24.txt.zst`)
  - `{instrument_id}_{date}_..._{market_type}-master-{a|b}.txt.zst` — older archive naming
- Container: **zstd** frame ([facebook/zstd](https://github.com/facebook/zstd)).
  Decompress with `zstd -d file.txt.zst`, or stream with `zstd -dc file.txt.zst | head`,
  or on-the-fly via any libzstd binding (`pip install zstandard`).
- Content: UTF-8 text, `\n` line endings. **Line 1 is a JSON object**
  (instrument masterdata, section 3). **Every other line is one event**, a
  JSON array (section 5). Instrument IDs are stable for the lifetime of an
  instrument and match the IDs used on cryptostruct.com.

## 2. Scale and performance

| Example | Compressed | Events | Composition |
|---|---|---|---|
| BitMEX XRPUSDT perp, one day | 59 MB | 1.87 M | 1.83 M book updates, 2.6 k trade events, 15.7 k mark, 15.7 k index, 198 funding, 3 snapshots |
| Binance BTCUSDT perp, one day | 1–2 GB | tens of millions | plus a top-of-book stream |
| Small/illiquid instruments | KB–MB | thousands | |

Compression ratio is roughly **4–8×** (a 59 MB file decompresses to ~254 MB of
text); flagship days decompress to **many GB**. Consequences:

- **Never** read a whole file into memory; stream line-by-line through a zstd
  decompressor (the bundled `scripts/cryptostruct_reader.py` does this).
- Filter by message type *before* JSON-parsing where possible (check the
  characters between `[` and the first `,`) — a trades-only pass then skips
  ~98% of the parse cost.
- A full pure-Python pass over a 59 MB day takes on the order of one minute.
  For repeated analysis convert once to Parquet
  (`cryptostruct_reader.py trades FILE --out trades.parquet`) and work from
  that.

## 3. Header line (line 1): instrument masterdata

Shape: `{"instrument": {...}, "underlyings": [...]}` — self-contained: no
external lookup is needed to interpret the file. Newer files may add fields;
read by name, ignore unknowns.

### `instrument` fields

| Field | Meaning |
|---|---|
| `id` | CryptoStruct instrument id (matches the filename and shop/API ids) |
| `code`, `code_alias` | normalized symbol, e.g. `BTCUSDT`; optional alias |
| `native_code` | venue-native symbol(s), object with key `default` |
| `exchange_id`, `exchange_code` | venue, e.g. `bitmex`, `binance_swap`, `gateio_spot` |
| `type` | `spot`, `perpetual`, `future`, … Note: prediction-market contracts (Kalshi/Polymarket) carry `type: "perpetual"` inside the raw day file (recorder vocabulary) even though the shop/search APIs class them as `prediction` — match on `exchange_code` if you need to detect them. Prices are event probabilities in 0..1 (Kalshi tick 0.01, Polymarket 0.001). |
| `state`, `active` | lifecycle state (`open`, …), active flag |
| `is_inverse`, `is_quanto` | contract style — see section 4 before computing notionals |
| `contract_value`, `multiplier` | contract math inputs — see section 4 |
| `strike_price` | options only, else null |
| `start`, `expiry` | listing/expiry datetimes; perpetuals carry a far-future sentinel (e.g. `2100-12-25`) — treat as "no expiry" |
| `ticksize` | minimum price increment (the unit behind the minute API's `spread_ticks`) |
| `tick_table` | optional price-banded ticksizes: `{"e":[{"p":<band-floor>,"t":<ticksize>},…],"i":<bool>}`; when present, the effective increment depends on price level |
| `significant_digits`, `max_decimals` | price formatting hints, may be null |
| `lot_size`, `min_order`, `max_order`, `min_value` | quantity grid and order limits (venue-native units) |
| `funding_interval` | seconds between funding events **as a string** (e.g. `"28800"` = 8 h); null/absent for spot |
| `base_underlying_id`, `counter_underlying_id`, `valuation_underlying_id`, `fee_underlying_id` | joins into `underlyings[]` |

### `underlyings[]` entries

| Field | Meaning |
|---|---|
| `id`, `code`, `name` | e.g. `100`/`BTC`/`Bitcoin` |
| `type` | `coin`, `stable_coin`, `fiat` |
| `fx_rate` | **USD per 1 unit — a static snapshot taken at `ts_fx`**, good for order-of-magnitude USD conversion, not for precise historical PnL |
| `ts_fx` | when `fx_rate` was sampled |
| `verified`, `about`, `meta` | informational; ignore for analysis |

## 4. Notional math (read before summing quantities)

Event quantities are **venue-native contract quantities**, not necessarily
base-asset amounts. Use the header:

- **Linear** (`is_inverse: false`, `is_quanto: false`):
  `base_amount = qty × multiplier` and `quote_value = qty × multiplier × price`.
  Worked example (real): BitMEX XRPUSDT has `multiplier: 0.01`, so a book
  level of `qty "901800"` at price `1.1091` is 9,018 XRP ≈ 10,003 USDT — *not*
  901,800 XRP. Binance BTCUSDT perp has `multiplier: 1`, so `qty "0.016"` is
  0.016 BTC.
- **Inverse** (`is_inverse: true`): contracts are quote-denominated; commonly
  `base_value ≈ qty × contract_value / price` — conventions differ per venue.
  Confirm against `contract_value`/`multiplier` and the venue's contract specs
  before PnL-grade use.
- **Quanto** (`is_quanto: true`): settles in a third currency
  (`valuation_underlying_id`) scaled by `contract_value` — same caution.
- **USD conversion**: multiply the quote value by the counter (or valuation)
  underlying's `fx_rate`. This approximates the minute API's `turnover_usd`
  (the API uses its own continuous FX, so expect ≈, not ==).

## 5. Event line envelope

Every line after the header (one exception: msgType 5):

```
[msgType, instrumentId, prevEventId, eventId, adapterTs, exchangeTs, data, ...extras]
```

| Index | Field | Type | Notes |
|---|---|---|---|
| 0 | `msgType` | int | see table below |
| 1 | `instrumentId` | int | same for every line of the file |
| 2 | `prevEventId` | string | `"0"` = start of a chain (no predecessor) |
| 3 | `eventId` | string | venue-dependent: numeric sequence **or** opaque hash |
| 4 | `adapterTs` | int | **nanoseconds** since epoch, UTC — receive time at the CryptoStruct recorder (colocated at the venue); always present; not guaranteed strictly monotonic |
| 5 | `exchangeTs` | int | ns since epoch as reported by the venue; `0` = not available |
| 6 | `data` | varies | per-type payload, below |
| 7+ | tail | varies | **schema-version-dependent**, documented per type below — ignore anything beyond |

**Iron rule: parse positionally from the front and tolerate unknown trailing
elements.** The known tails (protocol schema versions in parentheses):
snapshots end with a `forceReset` boolean (v6, 2026-era files); mark prices
carry 7 greek/volatility fields after the price (v7 — real values for
options, `null` otherwise); 2025-era files have neither. All prices and
quantities inside `data` are **decimal strings** — parse explicitly (keep the
raw strings when you need exactness); they may use scientific notation
(`"1.0E-4"`), which `float()` handles. Event ids can carry the sentinel
`"9223372036854775807"` (int64 max) meaning "no id" — equality-based gap
detection is unaffected.

### Message types

| msgType | Name | `data` shape |
|---|---|---|
| 0 | Book snapshot | `[[side, price, qty, ordercount], …]` — full book; tail: `forceReset` bool |
| 1 | Book update | same shape — level deltas (never a tail) |
| 2 | Trades | `[[side, price, qty, tradeId, exchangeTradeTs], …]` |
| 5 | Instrument state | *whole line is* `[5, instrumentId, adapterTs, state, message]` |
| 6 | Top-of-book | `[[side, price, qty, ordercount], …]` — at most one level per side |
| 7 | Mark price | `"price"`; tail: `delta, gamma, vega, theta, markVol, bidVol, askVol` |
| 8 | Index price | `"price"` |
| 9 | Funding | `[rate, nextFundingTs, predictedRate?]` |
| 17 | Liquidations | `[[side, price, qty, exchangeLiquidationTs], …]` — price = bankruptcy price |

`side` is always `0 = bid / buy` and `1 = ask / sell`. `ordercount` is
currently unused and always `1`.

## 6. Per-message semantics (with real sample lines)

### 0 — Book snapshot

```
[0,1210,"0","8328425028614922983",1782345115505070405,1782345115504140603,[[0,"1.0715","730000",1],[0,"1.0714","668700",1],[1,"1.0719","205100",1],[1,"1.072","3172800",1]],false]
```

- The complete book state. **Reset your book on every snapshot** — several per
  day are normal (recorder failover/reconnect; typically preceded by a state
  message like `"ready after failover"`).
- Levels are not ordered; both sides in one array (`side` field
  discriminates).
- The trailing boolean is **`forceReset`** (schema v6; absent in 2025-era
  files): when `true`, reset the book even if `prevEventId` doesn't chain —
  relevant only when arbitrating multiple feeds. On a single recorded file
  every snapshot resets the book anyway, so most parsers can ignore it.

### 1 — Book update

```
[1,1210,"8328425028614922983","7087889941297585337",1782345115843612834,1782345115843000000,[[0,"1.0698","1039100",1],[1,"1.0719","0",1]]]
```

- Level-based **absolute** updates: `qty` REPLACES the resting quantity at
  `price`; **`qty "0"` deletes the level**. Quantities are never deltas.
- One line is one atomic batch — evaluate book state only at line
  boundaries (mid-batch states can look crossed).
- Note the chain: this update's `prevEventId` equals the snapshot's `eventId`.

### 2 — Trades

```
[2,1210,"7697470040428040547","6584934061829438687",1782345122476611101,1782345122476000000,[[0,"1.0719","19200","00000000-006d-1000-0000-0033b50c7876",1782345122476000000]]]
```

- `side` is the **aggressor** (taker): `0` = buy (taker lifted the ask), `1` =
  sell (taker hit the bid). Same convention as the minute API's
  `trades_buy`/`trades_sell`.
- One event may batch **many fills** (one market order sweeping several
  levels); flatten `data` when counting trades.
- `exchangeTradeTs` (ns) is the per-fill matching-engine time and can differ
  from the message's `exchangeTs`. `tradeId` may be empty/null on some venues.
- Trade rows carry **no per-fill liquidation flag** — liquidations arrive as
  their own events (msgType 17, below); the minute API's `trades_liquidation`
  remains the aggregated count.

### 5 — Instrument state (different shape!)

```
[5,1210,1782345115505070404,"READY","ready after failover"]
```

- `[5, instrumentId, adapterTs, state, message]` — no event ids, no exchange
  timestamp. Handle before generic envelope parsing.
- `state` is `"READY"` or `"ERROR"` (e.g. lost venue connection). After an
  `ERROR`, treat book state as suspect until the next snapshot.

### 6 — Top-of-book

```
[6,67824,"0","7441981275789",1746575483925798368,1746575483924000002,[[0,"96806.5","4.703",1],[1,"96806.6","14.799",1]]]
```

- Standalone best-bid/best-ask samples (0, 1 or 2 rows; side order
  undefined). Present on some venues (e.g. Binance), absent on others — the
  depth stream (types 0/1) is the authoritative book.
- **Do not** feed these into your depth book — separate stream, own event-id
  space.

### 7 / 8 — Mark price / Index price (+ options greeks)

```
[7,1210,"0","7349456101763923293",1782345115505070407,1782345115500000003,"1.07258",null,null,null,null,null,null,null]
[8,1210,"0","8050104725181629121",1782345115505070406,1782345115493190275,"1.07252"]
[7,8967995,"1161823613938688246","4380261518099630876",1782431454751616000,1782431454735000000,"0.0070174072573915","0.3288191987","0.0070104172","0.3430565826","-7.0212090894","0.5437670545","0.5444425048","0.5664151171"]
```

- `data` (index 6) is the mark/index price as a decimal string.
- Mark-price lines carry **7 more fields** at indices 7–13 (schema v7; absent
  in 2025-era files): `delta, gamma, vega, theta, markVolatility,
  bidVolatility, askVolatility` — decimal strings for **options** (third
  sample: a real OKX ETH call — delta 0.33, mark IV 0.544), `null` for
  everything else (first sample: a perpetual). Vols are annualized implied
  volatilities as fractions (0.544 = 54.4%).
- Note on option marks: coin-settled venues (e.g. OKX) quote the option mark
  in the base currency (0.0070 ETH above) while the index price is USD —
  check the header's `is_inverse`/underlyings before mixing them.
- Emitted periodically (roughly every few seconds, venue-dependent);
  derivatives venues only.

### 9 — Funding

```
[9,1210,"0","3613991425845795139",1782345115505070408,1782345115500253321,["0.00010",1782360000000000000,"0.000011"]]
[9,67824,"0","681352840083568157",1746575483925798371,1746575483897227167,["0.00000757",1746576000000000000]]
```

- `[rate, nextFundingTs, predictedRate?]` — the rate applies per funding
  interval (header `funding_interval`, seconds); `nextFundingTs` is ns since
  epoch. `predictedRate` is absent in older files (second line, 2025 era).
- Events are periodic **republications of current state, not payments** —
  deduplicate on `nextFundingTs` for one row per interval. To annualize:
  `rate × (365×24×3600 / funding_interval)`.

### 17 — Liquidations

```
[17,2591,"0","4369112899956650000",1782864902273483642,1782864902271000000,[[1,"74.09","5.8",1782864902101000000]]]
```

- Liquidation orders as their own event stream (schema v5; **absent in files
  recorded before 2026**). One event may batch several rows.
- Row fields: `[side, price, qty, exchangeLiquidationTs]` — note there is
  **no tradeId**, unlike trade rows.
- `price` is the **bankruptcy price**, not a trade price — don't mix it into
  trade/VWAP series.
- `side` is the generic `0` / `1` code; the spec does not define whether it
  names the liquidated position or the forced order — don't over-interpret.
- `exchangeLiquidationTs` (ns) is per row and can differ from the envelope's
  `exchangeTs`.
- **Some venues only publish samples, not every liquidation** — treat counts
  as a lower bound and cross-check against the minute API's
  `trades_liquidation`.

- `prevEventId → eventId` forms per-stream chains. Verified behavior:
  - **Book stream (types 0 + 1)**: one chain; a snapshot's `eventId` is the
    `prevEventId` of the first following update. A snapshot starts a fresh
    chain (`prevEventId: "0"`).
  - **Trades (type 2)**: chain among themselves on some venues.
  - **Liquidations (type 17)**: chain among themselves (verified in
    production).
  - **Types 6/7/8/9**: usually unchained (`prevEventId: "0"` on every event).
- Event-id **texture is venue-specific**: numeric sequences on some venues
  (Binance), opaque hashes on others (BitMEX). Therefore detect gaps only by
  equality — `prevEventId == last seen eventId of the same stream` — never by
  arithmetic (+1).
- After a detected gap, distrust the book until the next snapshot.
  `cryptostruct_reader.py stats FILE` reports per-stream gap counts;
  `sample_book()` flags affected samples via `suspect`.

## 8. Which timestamp to use

| Purpose | Use |
|---|---|
| Ordering events, as-of joins, sessionization | `adapterTs` (always present, recorder colo clock; not guaranteed strictly monotonic, but complete) |
| Venue-clock studies, cross-venue latency | `exchangeTs` — guard against `0` (= unavailable) |
| Trade times | per-fill `exchangeTradeTs` inside trade batches |

Convert: `datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)` (Python) or
`pl.from_epoch(col, time_unit="ns")` (polars).

## 9. Offline parse fixture

Hand-check any parser against these real lines (no data file needed). The
bundled reader validates exactly these via
`python3 scripts/cryptostruct_reader.py selftest`.

Input lines:

```
[0,1210,"0","8328425028614922983",1782345115505070405,1782345115504140603,[[0,"1.0715","730000",1],[0,"1.0714","668700",1],[1,"1.0719","205100",1],[1,"1.072","3172800",1]],false]
[1,1210,"8328425028614922983","7087889941297585337",1782345115843612834,1782345115843000000,[[0,"1.0698","1039100",1],[1,"1.0719","0",1]]]
[2,1210,"7697470040428040547","6584934061829438687",1782345122476611101,1782345122476000000,[[0,"1.0719","19200","00000000-006d-1000-0000-0033b50c7876",1782345122476000000]]]
[5,1210,1782345115505070404,"READY","ready after failover"]
[9,67824,"0","681352840083568157",1746575483925798371,1746575483897227167,["0.00000757",1746576000000000000]]
[17,2591,"0","4369112899956650000",1782864902273483642,1782864902271000000,[[1,"74.09","5.8",1782864902101000000]]]
[7,8967995,"1161823613938688246","4380261518099630876",1782431454751616000,1782431454735000000,"0.0070174072573915","0.3288191987","0.0070104172","0.3430565826","-7.0212090894","0.5437670545","0.5444425048","0.5664151171"]
```

Expected results:

| Line | Expectation |
|---|---|
| snapshot | type 0; 4 levels; trailing `false` = `forceReset`; best bid `1.0715 × 730000`, best ask `1.0719 × 205100`; `adapterTs` = `2026-06-24T23:11:55.505070405Z` |
| update | type 1; chains from the snapshot (`prev == snapshot.eventId`); inserts bid `1.0698 × 1039100`; **deletes** ask `1.0719` (qty `"0"`) → best ask becomes `1.072` |
| trade | type 2; one fill; buy aggressor (side 0); price `1.0719`, qty `19200` (× multiplier `0.01` = 192 XRP) |
| state | type 5; special shape; `("READY", "ready after failover")` |
| funding | type 9, 2025 era; rate `0.00000757`; next funding `2025-05-07T00:00:00Z` + no predicted rate (2-element data) |
| liquidation | type 17; one row; side `1`; bankruptcy price `74.09`, qty `5.8`; row ts = `2026-07-01T00:15:02.101Z` |
| greeks mark | type 7 (real OKX ETH call); mark `0.0070174072573915`; delta `0.3288191987`, gamma, vega, theta, mark/bid/ask vols all non-null (mark IV `0.5437670545`) |

---

*Companion references: [`minute-api.md`](minute-api.md) (per-minute
statistics API), [`recipes.md`](recipes.md) (runnable analysis code).
Canonical URLs: free samples & docs — <https://cryptostruct.com/download>,
full archive — <https://cryptostruct.com/shop>.*
