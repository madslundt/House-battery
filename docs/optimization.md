# Optimizer, forecasts, and tuning

## What is optimized

House Battery recalculates its economic plan when a tariff interval changes,
published prices or usable forecasts change, or planning settings and scheduled
loads change. Quarter-hour feeds therefore normally replan every 15 minutes;
hourly feeds follow their hourly boundaries, including consecutive equal prices.
Price-entity events and a boundary timer trigger updates. Ordinary minute polls
reuse the economic plan. Telemetry, learning, accounting and safety checks still
run every minute; reserve/ceiling crossings, load infeasibility and recovery from
a fault can require an immediate replacement plan.

Each new deterministic dynamic-programming plan retains the source's interval
durations and starts from observed SOC and predicted connected load.
The plan respects SOC bounds, capacity,
charge/discharge power, round-trip losses, switching cost, degradation cost,
and the effective minimum required profit. With adaptive planning off, this
is the configured manual profit margin. Mode switching cost is fixed internally;
there is no user-configurable mode lock or daily transition budget.

Load energy is integrated over the learned quarter-hour profile inside each
price interval, including partial intervals and daylight-saving changes. A
declared additional load is added to a separate background estimate. While a
declaration is active, its measured total is excluded from background learning;
the last credible pre-event background is held for at most 24 hours. If neither
that background nor a qualified profile exists, planning waits rather than
counting an appliance twice. `load_forecast_status` explains this condition on
**Optimizer state**.

Direct-local planning and control share the 50 W discharge margin and native
integer-watt command rules. With a configured mains-input isolation plug,
Battery mode must serve the entire predicted load within the configured output
bound and available SOC; the plan never assumes grid support while isolated.
Fresh measured load can cancel the current discharge while later intervals
remain eligible. Conservative SOC discretization is reported separately as
`planning_rounding_loss_wh`, not as delivered energy or a measured battery loss.

Charging retains its existing combined-input planning convention. Whether the
FBP1200 custom charging setpoint has exactly that physical meaning still needs
independent input/output measurements; `control_capabilities` reports the
unverified semantics. Standby drain, separate leg efficiencies and taper curves
are not inferred from a single round-trip ratio. Accepted efficiency windows
now retain topology, durations and load/power ranges to support that analysis.

If observed SOC is below **Arbitrage reserve SOC**, restoring the reserve is
required even when that purchase cannot pay for itself through arbitrage. The
planner chooses the cheapest feasible recovery within the contiguous known-price
horizon, accounting for losses, power limits, and any held current interval.
Cheaper forecast prices cannot postpone recovery beyond confirmed prices. If
the known horizon is too short to restore the reserve, the plan recovers as much
as physically possible. Optional charging and cycling above the reserve still
use the normal profit rules. Recovery can make reported plan savings negative.

Charging may use a lower native SOC ceiling when that is enough for a profitable
peak. The planner considers integer ceilings supported by the device; legacy
provider controls retain their native bounds and step sizes. A published current
interval keeps its charge ceiling until the tariff boundary. Manual overrides
continue to use the configured normal ceiling.

The internal low-SOC charging premium and high-SOC discharge margin discount
change the optimizer's marginal energy value smoothly between their configured
SOC breakpoints. They can favor restoring a depleted battery and using energy
near full SOC. They do not change the physical reserve, charge ceiling, tariff
cost, or reported plan savings.

The switching cost discourages routine chattering. A partial first price slot
continues the current action through its tariff boundary. During automatic
operation, the integration applies the arbitrage reserve as the inverter's
native minimum SOC before changing modes. Actual SOC independently ends a held
discharge at the reserve and requests mains restoration. Native read-back does
not establish a separate backup-output cutoff while mains are isolated. The
lower absolute emergency SOC is requested only for a confirmed grid outage;
ordinary safe requests and data faults preserve the arbitrage reserve.

It therefore does not cycle the battery merely because the next interval is a
little more expensive. The normal economic floor is **Arbitrage reserve SOC**;
**Absolute emergency SOC** is the lower native limit preserved for an outage.

When **Allow opportunistic full charge** is enabled, the coordinator compares
the normal-target plan with a second plan using a fixed 100% target. The higher
target is selected when it adds meaningful charging and improves the planner's
risk-adjusted objective after losses, degradation, switching cost, and **Minimum
required profit**. That objective includes uncertainty-buffered forecasts when
enabled and a conservative terminal value for energy carried beyond the horizon.
The extra energy need not be discharged before known prices end, so estimated
future value can justify charging even when cash savings within the horizon fall.
For unusually cheap periods, this opt-in policy also compares upcoming prices
with recent confirmed import prices. With at least 24 contiguous future hours,
the duration-weighted lower price quartile over the next seven days, less a 25%
uncertainty haircut, provides a conservative floor for carryover value. Forecast
prices are reduced by their configured uncertainty before this comparison.
If at least 24 covered historical hours across three UTC dates are available
within the previous seven days, their weighted median, with the same haircut,
caps this additional reference. Each historical hour needs at least 90% coverage;
incomplete hours and old data are excluded. Higher historical prices never raise
the reference above what upcoming prices support. During initial learning,
future prices alone can provide the reference. Both candidate plans use the same
reference, and the existing terminal estimate remains the fallback for short
horizons. This can fill the battery to 100% even when only part of that energy
will be used within the current horizon; full charging still requires sufficient
time, power and economic benefit. Terminal credit remains separate from planned
bill savings and measured savings.
Manual operating overrides always retain the configured normal charge ceiling.

## Adaptive planning

**Adaptive planning** is an opt-in control. It starts disabled for existing
installations, retains every manual setting, and reports `learning` while the
evidence is insufficient. After at least three consistent, independent
matched-SOC periods qualify the real-meter efficiency estimate, its active
policy evaluates net savings after conversion losses, wear and switching
costs without adding an arbitrary extra profit margin or internal SOC-based
price preferences. Turning it off restores
the configured **Minimum required profit**; the number itself is never rewritten.

Efficiency learning combines a true active-power grid meter, connected load,
battery activity and matching starting/ending SOC. Charging energy is estimated
after subtracting the load and a bracketed idle baseline; only confirmed
battery-served load contributes delivered energy. Missing readings, unstable
baselines and implausible cycles are rejected. A whole-home active-power meter
is valid when its background load is sufficiently stable; a correctly scoped
phase reading is another option. Apparent-power smart-plug readings are unsuitable.
There is no fixed efficiency target: the configured fallback applies until the
measured evidence qualifies, and stale or inconsistent evidence returns planning
to that fallback.

Adaptive forecast handling can raise the configured uncertainty floor when
recent errors warrant it. It captures the first forecast revision seen in each
of the 0–24, 24–48, and 48–72 hour lead bands, using the entity's last update
time to measure the lead. A band needs 24 complete scored hours
across at least three days to use its own allowance. While that evidence
accumulates, the source-wide allowance applies. Automatic source priority
compares providers over the same scored hours. Known tariffs remain authoritative and the
forecast switch still controls whether forecast intervals can drive planning.
SOC reserves, charge ceilings, capacity, power limits and safety gates remain
under their existing controls.

**Adaptation status** explains `disabled`, `learning` or `active`, automatic
changes, recommendations and their evidence. The optional **Planned bill
savings**, **Planned wear cost** and **Planned switching cost** sensors separate
the current horizon's electricity-bill benefit from modeled costs; bill savings
do not include terminal credit for energy left in the battery. Compare these
components when assessing a plan rather than treating expected future energy
value as cash already earned.

## Battery sizing

**Battery sizing** is a read-only financial assessment, updated daily in the
background even when adaptive planning is off. It remains `learning` until
qualified real-meter round-trip efficiency and 28 complete days of hourly
measured connected load and known prices are available, including at least
eight weekend days. Evidence is retained for 90 days; incomplete days do not
qualify. The sensor's reason and evidence explain missing coverage.

The assessment replays half the current capacity, the current capacity, and the
current capacity plus **Sizing expansion capacity**. Each case starts and ends
at the same SOC, so stored energy is not treated as free inventory. It includes
conversion losses, modeled wear and switching costs. Extra capacity is judged
against **Sizing expansion cost** and **Sizing payback target**, defaulting to
1.958 kWh, 3000 DKK and ten years. A free expansion has zero payback years when
it provides a positive benefit.

`bigger_battery_better` means the expansion meets the configured payback target.
`size_is_fine` can still mean extra storage helps but does not justify its price.
`battery_too_big` means half the capacity retains at least 95% of the modeled
net savings while losing no more than 50 DKK per year. That conclusion concerns
price arbitrage only; it does not value backup runtime. The financial figures
annualize the recent period with hindsight, rather than forecast a full year's
return. No recommendation changes installed capacity, SOC or power limits.

## External price forecasts

The optional ordered **External price forecast entities** use precisely the same list
attribute format as known prices: `prices`, `raw_today`, `raw_tomorrow`,
`today`, or `tomorrow`, with `start` (or `hour`/`time`) and `price` (or
`value`). Explicit `end` times are retained. Without an `end`, the interval is
inferred from the source's adjacent start times; only an isolated row defaults
to one hour. Known prices and forecasts may therefore use different cadences,
such as hourly known prices and quarter-hour forecasts.
For registered Strømligning forecast entities, adjacent hourly rows labeled
with 15-minute ends are repaired to hourly intervals. Missing hours still
stop the forecast extension. Confirmed tariff intervals keep their published ends.

Forecasts can extend the optimizer's horizon to at most **72 hours from the
current planning time**. A forecast interval crossing that boundary is clipped.
Confirmed prices remain authoritative even when they extend farther ahead.
Every appended forecast interval remains labeled `forecast` and carries the
configured uncertainty; it is never relabeled as a known tariff.

Known prices remain authoritative. When **Use price forecasts** is
off, forecast data are collected only for accuracy evidence. Overlapping
sources are each scored independently against the actual known price. When it
is on, the optimizer normally uses the first configured usable source that provides
an uninterrupted sequence of forecast intervals starting immediately after the
last known interval. Adaptive planning may change that priority using recent
accuracy evidence over common scored coverage. It never overwrites known
prices, fills a gap in them, or uses forecasts without a known-price horizon.
It also rejects an unavailable/unknown entity, malformed row, empty or expired
horizon, and data older than three hours. These are forecast-only conditions:
valid known-price planning remains in service.

Set **Price forecast uncertainty** to the likely absolute forecast
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
`fair`, or `poor` for the displayed primary source. Its `price_forecast_sources`
attribute includes the same sample count, MAE, bias (forecast minus actual),
and percentage within your uncertainty buffer for every configured source.
Positive bias means forecasts tend to overstate prices. Each source preserves
its first forecast seen for an interval, even if sources overlap. If cadences
differ, the comparison uses the duration-weighted actual price over each
forecast interval. Use this evidence even while the forecast switch is disabled.

Check the forecast switch's `status` attribute before relying on a late-horizon
plan. `used` means valid forecast slots extended the known horizon. `stale`,
`invalid`, `empty`, `expired`, and `unavailable` mean they were deliberately
ignored; `no_contiguous_extension` means valid rows did not join the published
horizon exactly.

## Learned price patterns

The integration also learns its configured **confirmed all-in import prices**
by local weekday and hour. It does not assume weekends or overnight hours are
cheap. Completed hours need at least 90% price coverage; repeated polls and
duplicate rows do not add independent observations. At least four distinct
matching weekday dates are required before a bucket can predict a price.

Historical coverage alone does not enable control. First-issued hourly
predictions compete with a flat seven-day baseline at separate 0–24, 24–48 and
48–72 hour leads. The approved 28-day weekday/weekend/hour median needs at
least 24 matched hours across three local dates, each with at least six hours,
and must improve MAE by at least 10% and 0.01 DKK/kWh. Ninety-day weekday,
shorter-window and recency-weighted alternatives stay in shadow diagnostics;
the integration does not automatically select whichever recently scored best.

With **Use price forecasts** enabled, a qualified pattern can continue the
planning horizon after published prices and the selected external forecast end,
up to the same 72-hour cap. External forecasts take priority. Learned estimates
never replace either source, repair a known-price gap, or stand in for a missing
current tariff. With the switch off, history still accumulates but cannot drive
planning. Existing entity IDs and the switch's stored on/off choice are retained.

The model retains up to 90 days of history, uses the approved 28-day profile
for control, and requires an actual observation within three days. Its
uncertainty is at least the configured allowance and 0.25 DKK/kWh, increased
by historical dispersion, first-issued errors at the matching lead and
disagreement with the published/forecast prefix. These allowances can make a
pattern too uncertain to justify charging. Patterns are labeled `forecast` in plan slots;
`price_patterns` on **Optimizer state** and `learned_pattern` in forecast-source
diagnostics identify their origin, coverage and validation error.

The planner also checks the proposed learned extension for actual battery
movement in those forecast intervals. If its only effect is changing the
value assigned to end-of-horizon inventory, it replans the trusted prefix and
keeps the learned prices in shadow. `control_status` and `control_reason` explain
that decision. A credible forecast peak can still justify charging during
confirmed cheap hours. This guard prevents the demonstrated terminal-only
regressions; it does not establish household savings from price patterns.

Forecast uncertainty values existing battery inventory and newly acquired
energy separately. Existing stock retains its confirmed continuation value
unless the forecast interval supports a credible decline; extra energy gets
only the cautious lower quote. This avoids liquidation solely because an
uncertain extension was appended, without using protected stock value to
finance speculative purchases. Both quotes and the exact carryover credit
are exposed separately; replay stores the actual caller-supplied floor.

With both **Adaptive planning** and **Use price forecasts** enabled, external
sources can also receive source-specific bias correction. Dated residuals
must pass chronological validation, coverage, tail-error and recent-stability
checks. Corrections are shrunk and capped at 0.75 DKK/kWh. Their uncertainty
contains the complete original source interval, so a shifted mean cannot
create a better trading bound by itself. Raw provider predictions remain the
independent accuracy evidence. Missing or unqualified evidence leaves the
source unchanged; configured-source priority still uses raw accuracy.

Compatible retained measured price history can warm-start learning. Changing
price bindings, reported units/currency or local timezone clears the incompatible
profile. Existing all-in prices are used directly; the integration does not
download spot prices or add another tariff layer. See the
[DK2 research and reproducible analysis](research/dk2-price-patterns.md) for the
observed weekend and seasonal effects and their exceptions.

## A realistic week

| Day | Price pattern | Expected conservative outcome |
| --- | --- | --- |
| Monday | 1.10–1.45 DKK/kWh all day | Stay on grid; spread does not cover wear and profit. |
| Tuesday | 0.25 at 03:00, 2.60 at 18:00 | Charge overnight and discharge into the evening peak if load needs it. |
| Wednesday | 0.80–1.30, brief 1.55 peak | Preserve energy; the single peak is normally not worth a cycle. |
| Thursday | Known prices end at 14:00; forecast shows 0.35 then 2.40 | Use forecast only after 14:00 and only after the uncertainty buffer. |
| Friday | 0.10 overnight, 3.00 at 17:00 | The opt-in higher target may activate if known load uses the extra energy profitably. |
| Saturday | Negative midday price, low evening load | Charge only if capacity/headroom and later demand make it useful. |
| Sunday | Flat 0.95–1.20 | Prefer no cycle; retain reserve for resilience. |

The actual plan will differ with SOC, learned load, your tariff, and safety
limits. Inspect **Operation plan** and its block explanations rather than
assuming a price pattern mandates battery use.

## Evaluate degradation, ROI, and later tuning

**Load forecast mean absolute error** now scores predictions saved before their
target interval against measured total demand with at least 90% coverage. It
remains unavailable until such evidence exists. **Optimizer state** exposes
`issued_load_forecast_accuracy`, with separate 0–2, 2–6, 6–12, 12–24 and 24–72
hour lead bands, sample counts, coverage, signed bias (actual minus predicted)
and residual quantiles. The first prediction per target/band is retained;
minute replanning cannot replace a poor prediction with hindsight. The earlier
online smoothing metric remains separately available as
`load_model_smoothing_error_w`; old smoothing scores are not migrated into issued
forecast accuracy. Uncertainty evidence does not automatically change dispatch.

Actual financial values require a covering validated known-price interval.
When it is missing, measured energy remains recorded but is unpriced: a numeric
price-entity state or a forecast is not substituted. The supplied current price
already includes VAT, tariffs and spot energy. Source diagnostics report
explicit unit contradictions and conflicting duplicate intervals without adding
fees or assuming absent metadata is wrong.

For offline policy comparison, export new evidence and use the bounded rolling
replay described in [the evaluation guide](llm-evaluation.md#offline-policy-replay).
Capacity sizing remains a separate hindsight assessment.

Review at least two representative tariff weeks. **Battery charge/discharge
total** and **Equivalent full cycles** show throughput; rising throughput
without realized savings suggests increasing minimum profit or degradation
cost. **Estimated degradation**, **Learned usable capacity**,
and **Learned round-trip efficiency** should be compared with the BMS or
manufacturer diagnostics.

Compare **Expected plan savings** with realized savings and **Load forecast
mean absolute error**. Persistent load error suggests correcting sensor scope,
adding scheduled loads, or allowing more history. Compare forecast MAE/bias
with its uncertainty buffer: adaptive planning can raise the floor from recent
errors; otherwise increase the buffer or leave external forecasting disabled.
If accuracy is consistently good but
useful late-horizon opportunities are missed, cautiously reduce the buffer.

Estimate ROI from comparable periods: `lifetime realized savings ÷ installed
battery cost`. Do not use a single day. Change one policy number at a time,
observe multiple similar horizons, and export the evidence through
`house_battery.export_data` for offline/LLM analysis. Keep a log of tariff,
settings, cycles, savings, forecast accuracy, and observed battery behaviour.
