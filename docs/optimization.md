# Optimizer, forecasts, and tuning

## What is optimized

Every minute, House Battery validates local telemetry, retains each price
source's valid interval duration, predicts the connected load, and uses a
deterministic dynamic-programming plan. The plan respects SOC bounds, capacity,
charge/discharge power, round-trip losses, minimum mode duration, maximum
daily transitions, switching cost, degradation cost, and the minimum required
profit.

It therefore does not cycle the battery merely because the next interval is a
little more expensive. The normal economic floor is **Arbitrage reserve SOC**;
**Absolute emergency SOC** is the lower native limit preserved for an outage.

Extra storage is permitted only when the full known price spread clears the
configured **Extra-storage price spread** *and* the effective margin after
losses and degradation clears **Minimum required profit**. The planner still
has to find a useful charge interval before it stores more energy.

## External price forecasts

The optional **External price forecast entity** uses precisely the same list
attribute format as known prices: `prices`, `raw_today`, `raw_tomorrow`,
`today`, or `tomorrow`, with `start` (or `hour`/`time`) and `price` (or
`value`). Explicit `end` times are retained. Without an `end`, the interval is
inferred from the source's adjacent start times; only an isolated row defaults
to one hour. Known prices and forecasts may therefore use different cadences,
such as hourly known prices and quarter-hour forecasts.

Known prices remain authoritative. When **Use external price forecast** is
off, forecast data are collected only for accuracy evidence. When it is on,
the optimizer may use only an uninterrupted sequence of forecast intervals
starting immediately after the last known interval. It never overwrites known
prices, fills a gap in them, or uses forecasts without a known-price horizon.

Set **External price forecast uncertainty** to the likely absolute forecast
error in DKK/kWh. In a forecast interval, the optimizer evaluates charging at
`forecast + uncertainty` and discharge at `forecast − uncertainty`. This
makes charging look less cheap and discharge less valuable, so an uncertain
forecast must still demonstrate a worthwhile spread.

Example: the current published horizon ends at 14:00. An external entity has
15:00–20:00 forecasts, including 0.30 DKK/kWh at 16:00 and 2.20 at 18:00. With
a 0.25 buffer, a charge is evaluated as 0.55 and a discharge as 1.95. If the
round-trip losses, 0.35 DKK/kWh wear cost, and required 0.75 profit do not fit
inside that conservative spread, the battery remains idle/grid-powered.

**External price forecast accuracy** reports `unknown`, `excellent`, `good`,
`fair`, or `poor` from the first forecast recorded for each interval once the
actual known price arrives. Its attributes include sample count, MAE, bias
(forecast minus actual), and the percentage of samples inside your uncertainty
buffer. Positive bias means forecasts tend to overstate prices. If the source
cadences differ, the comparison uses the duration-weighted actual price over
each forecast interval. Use its evidence even while the forecast switch is
disabled.

## A realistic week

| Day | Price pattern | Expected conservative outcome |
| --- | --- | --- |
| Monday | 1.10–1.45 DKK/kWh all day | Stay on grid; spread does not cover wear and profit. |
| Tuesday | 0.25 at 03:00, 2.60 at 18:00 | Charge overnight and discharge into the evening peak if load needs it. |
| Wednesday | 0.80–1.30, brief 1.55 peak | Preserve energy; the single peak is normally not worth a cycle. |
| Thursday | Known prices end at 14:00; forecast shows 0.35 then 2.40 | Use forecast only after 14:00 and only after the uncertainty buffer. |
| Friday | 0.10 overnight, 3.00 at 17:00 | Extra-storage policy may allow the higher target if its own threshold passes. |
| Saturday | Negative midday price, low evening load | Charge only if capacity/headroom and later demand make it useful. |
| Sunday | Flat 0.95–1.20 | Prefer no cycle; retain reserve for resilience. |

The actual plan will differ with SOC, learned load, your tariff, and safety
limits. Inspect **Operation plan** and its block explanations rather than
assuming a price pattern mandates battery use.

## Evaluate degradation, ROI, and later tuning

Review at least two representative tariff weeks. **Battery charge/discharge
total** and **Equivalent full cycles** show throughput; rising throughput
without realized savings suggests increasing minimum profit, degradation cost,
or switching penalty. **Estimated degradation**, **Learned usable capacity**,
and **Learned round-trip efficiency** should be compared with the BMS or
manufacturer diagnostics.

Compare **Expected plan savings** with realized savings and **Load forecast
mean absolute error**. Persistent load error suggests correcting sensor scope,
adding scheduled loads, or allowing more history. Compare forecast MAE/bias
with its uncertainty buffer: if MAE is routinely larger, increase the buffer
or leave external forecasting disabled. If accuracy is consistently good but
useful late-horizon opportunities are missed, cautiously reduce the buffer.

Estimate ROI from comparable periods: `lifetime realized savings ÷ installed
battery cost`. Do not use a single day. Change one policy number at a time,
observe multiple similar horizons, and export the evidence through
`house_battery.export_data` for offline/LLM analysis. Keep a log of tariff,
settings, cycles, savings, forecast accuracy, and observed battery behaviour.
