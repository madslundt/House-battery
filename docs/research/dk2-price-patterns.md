# DK2 price patterns — 2026-11-02

DK2 has useful weekday and time-of-day price tendencies, but neither weekends nor
overnight hours are reliably cheapest on every date. The integration should learn
patterns from its configured import prices and treat them as uncertain forecasts,
with published prices authoritative and external forecasts preferred.

## Data and reproducibility

This analysis downloaded **35,040 quarter-hour DK2 prices over 365 complete local
days, 2025-11-01 through 2026-10-31**. Prices are Energinet's
`DayAheadPriceDKK / 1000`, in **DKK/kWh spot, excluding VAT, network tariffs,
taxes and supplier additions**. The official metadata defines the original unit
as DKK/MWh and notes that its DKK conversion uses Nationalbank exchange rates.
The dataset has quarter-hour resolution; older hourly observations are in
`Elspotprices`. See [Energinet dataset metadata](https://api.energidataservice.dk/meta/dataset/DayAheadPrices)
and [Day-Ahead Prices](https://www.energidataservice.dk/tso-electricity/DayAheadPrices).

The start date is inclusive and end date exclusive in Danish local time, as
documented by the [official API guide](https://www.energidataservice.dk/guides/api-guides).
The script parses `TimeUTC` as UTC, converts to `Europe/Copenhagen` for calendar
features, checks contiguous UTC quarters, and weights each actual quarter equally.
Spring's missing and autumn's repeated local hour retain their actual elapsed
time; repeated quarters receive one averaged daily observation during training.
Only complete ISO weeks enter the weekday/weekend weekly comparison.

Reproduce with the standard-library-only script; it makes one historical request
and prints JSON without retaining raw data:

```sh
python3 scripts/analyze_dk2_prices.py --start 2025-11-01 --end 2026-11-01
```

The official query is [DK2, 2025-11-01 to 2026-11-01](https://api.energidataservice.dk/dataset/DayAheadPrices?start=2025-11-01&end=2026-11-01&filter=%7B%22PriceArea%22%3A%5B%22DK2%22%5D%7D&sort=TimeUTC&limit=0).
The following numbers are our calculations from that query, rather than claims
published by Energinet. API revisions can change a later reproduction.

## Observed tendencies

| Comparison | Mean spot price, DKK/kWh |
| --- | ---: |
| Monday–Friday | 0.8918 |
| Saturday–Sunday | 0.6490 |
| Saturday | 0.6698 |
| Sunday | 0.6279 |
| Cheapest annual mean local hour, 13:00–14:00 | 0.4849 |
| Most expensive annual mean local hour, 19:00–20:00 | 1.1944 |
| April–September, 13:00–14:00 | 0.2857 |
| October–March, 13:00–14:00 | 0.6852 |
| April–September, 04:00–05:00 | 0.8601 |
| October–March, 04:00–05:00 | 0.6356 |

Weekend prices averaged **27.2% below weekdays**. A weekend was cheaper than
the Monday–Friday period preceding it in **43 of 51 complete ISO weeks**; eight
weeks contradicted the rule. For example, 2026 ISO week 11 had weekday mean
0.6760 and weekend mean 0.8143. An annual average therefore cannot justify
automatically waiting until Sunday to charge.

The seasonal midday/overnight reversal is material: this sample's summer midday
prices were much lower than overnight prices, while winter overnight prices
were lower than midday. Seasonal windows and recent price levels matter.
Energinet independently describes increased intraday volatility from solar,
low-renewables price exposure in DK2, and midday negative prices coinciding with
wind/solar output. See its [Electricity Market Report 2025, pp. 6 and 15–17](https://en.energinet.dk/media/v2fhaaxv/250226-electricity-market-report-2025.pdf).

The tariff component has a different, deterministic schedule. For example,
Radius' published summer-2026 C-customer grid rates are 13.27 øre/kWh at
00:00–06:00, 19.91 at 06:00–17:00/21:00–00:00, and 51.76 at 17:00–21:00,
including VAT; its notice also describes the winter/summer switch. The
integration must use the user's complete configured import prices rather than
adding spot-only history to them. See [Radius' tariff notice](https://radiuselnet.dk/radius-haever-tarifferne/).

## Does a learned pattern predict better than a flat recent price?

A simple chronological replay fits one mean for each local weekday/quarter from
the **previous eight weeks**, once at each day's midnight. No prices from the
predicted day enter training. After 56 warmup days, 29,632 quarters have eight
independent matching dates available; DST leaves some buckets temporarily short.
On identical scored quarters:

| Day-ahead predictor | Mean absolute error, DKK/kWh |
| --- | ---: |
| Same weekday/quarter mean over previous eight weeks | 0.2861 |
| Flat mean over previous seven complete local days | 0.3623 |

The profile reduces MAE by approximately **21%** against that simple baseline.
Residual error remains substantial. This is one year of spot data, with strongly
autocorrelated quarters and changing price levels; it is not a confidence
interval, a comparison with the installed external forecasts, or evidence of
battery savings. Real acceptance should compare first-issued forecasts against
subsequent known prices, by lead time, and replay battery decisions after losses
and wear. Avoid fitting and evaluating on the same observations.

## Integration design implications

1. Learn configured **known all-in prices** in local weekday/time buckets using
   unique settled interval timestamps. Hourly rows can supply their four covered
   quarters, but repeated minute polls and provider revisions must not inflate
   independent weekly sample counts. Reject malformed/gapped coverage.
2. Preserve published prices and prefer usable external forecasts. Use historical
   estimates only as explicitly labeled uncertain continuation where those
   sources end. Do not replace a valid known price or silently fill a source gap.
   Weather-aware external forecasts can capture conditions a calendar model cannot.
3. Require several independent matching dates, apply a recent rolling window,
   expire stale profiles, and adapt to tariff/price-level changes. Daily/hourly
   shrinkage can help sparse buckets, but needs honest qualification. Clear or
   isolate history when source bindings or units change.
4. Apply at least the configured forecast uncertainty; increase it for observed
   prediction error and variation across weeks. Score predictions before adding
   their realized prices to training. Expose readiness, observation count, source
   attribution, coverage and errors. A learned mean is never a confirmed tariff.
5. Replan on tariff interval boundaries and meaningful source/configuration
   changes, including new tomorrow prices or forecast revisions. Keep telemetry,
   reserve enforcement and fault handling on their existing frequent loop.
   Consecutive equal-price quarters still have distinct elapsed-energy windows.

These recommendations are engineering inferences from the evidence above, not
claims that a fixed weekend rule will improve every battery's economics.
Nord Pool confirms the [15-minute day-ahead transition for delivery on 1 October 2025](https://www.nordpoolgroup.com/en/message-center-container/newsroom/exchange-message-list/2025/q3/market-coupling-steering-committee-confirms-go-live-of--15-minute-mtu-in-sdac-on-trading-day-30-september-2025-for-delivery-day-1-october-2025/).
Energinet's API is not intended to receive identical requests from every device;
its [API guidance](https://www.energidataservice.dk/guides/api-guides) favors update-aware
requests. This research script does not add an external polling dependency to
the Home Assistant integration.

## Independent implementation validation — 2026-11-02

This section records the earlier implementation and its discovered defects.
The hardened implementation and subsequent validation are described below.

Three independent agents reviewed the uncommitted implementation over commit
`dabff29`: one checked correctness and cadence, one compared price predictors,
and one replayed battery dispatch. No production behavior was changed during
this audit. The earlier eight-week quarter-hour mean result above is separate
from the production learner's ninety-day weekday/hour median.

### Actual learner and alternative patterns

The walk-forward replay uses the same official 35,040-quarter dataset. At each
Copenhagen midnight, only elapsed actual prices train the production learner;
the current local day's published prices form the known prefix. After a 56-day
warmup, predictions at 24–72 hours are scored on 59,036 matched forecast
instances. Overlapping targets are not independent statistical observations.
All compared candidates cover the same eligible targets, including both DST
transitions. Units remain spot DKK/kWh, excluding tariffs, VAT and additions.

| Predictor | MAE, DKK/kWh |
| --- | ---: |
| Production ninety-day weekday/hour median | 0.289191 |
| Flat previous-seven-day mean | 0.376317 |
| Previous week, same local quarter | 0.338232 |
| Previous-56-day weekday/quarter mean | 0.286701 |
| Previous-28-day weekday/weekend/hour median | 0.262574 |
| Recency-weighted weekday/hour mean | 0.283429 |
| Production profile adjusted by published-prefix level | 0.283742 |

The production model improves MAE by 23.2% over the flat baseline and 14.5%
over weekly persistence. The simpler 28-day weekday/weekend grouping improves
another 9.2%, including both seasonal groups and every covered month. This is
exploratory selection on one year; a separate holdout is needed before promoting
a candidate. The production model's expensive-decile MAE is 0.687740, compared
with 0.530945 for that grouped candidate, so peak prediction remains a weakness.

The production uncertainty covers 96.03% of scored outcomes, with a mean
allowance of 1.027087 and median 0.822887 DKK/kWh. Wide allowances can suppress
otherwise useful cycles. The existing rolling chronological hourly diagnostic
averages 0.246419; it describes a different window and granularity, and must not
be presented as issued 24–72-hour forecast accuracy. Hourly-target MAE for those
actual issued multi-day predictions is 0.281961.

The model recognizes weekday/hour patterns only. It has no explicit holiday,
seasonal or price-regime predictor, no competition with a baseline before
activation, and no proven-savings gate. Recent regimes affect its uncertainty;
they do not directly adjust its historical median prediction.

### Economic replay and a terminal-value defect

The economic replay compares known-only and learned-tail policies over four
continuous weeks starting 2026-01-05, 2026-03-09, 2026-07-06 and 2026-10-19.
Each price basis covers 673 actual hours. It uses the real optimizer and existing
physical replay plant, a 1.958 kWh battery, independent carried SOC, conversion
losses, wear, switching costs, and common final-inventory settlement at
1 DKK/kWh. Observed prices are aggregated hourly; planning uses 50 Wh steps and
a deterministic shared load with perfect load forecasts. Tomorrow's prices are
assumed available at 14:00 local. Opportunistic full charge is off.

| Price basis | Known-only net cost | Learned-tail net cost | Learned improvement |
| --- | ---: | ---: | ---: |
| Official DK2 spot | 179.9984 DKK | 181.3389 DKK | −1.3406 DKK |
| Spot plus explicit synthetic import additions | 434.2768 DKK | 432.6084 DKK | +1.6684 DKK |

The synthetic basis adds 1 DKK/kWh plus 0.50 at 17:00–21:00 and 0.10 otherwise;
it is not claimed to reproduce the installation's tariff. The spot result
depends on final inventory value and remains negative at replacement prices
of 0.5–2 DKK/kWh. These are small, scenario-dependent simulated differences,
not established household savings or annual estimates.

All 28 differing first-action hours had zero planned energy throughput in the
learned tail. Changes were driven by terminal stored-energy valuation. No
reserve violations occurred; numerical energy-balance error stayed below
1e-12 Wh. A zero-profit-hurdle check on the stylized March week produced no
policy difference.

A deterministic case reproduces unnecessary cycling without prediction error:
train the real learner on 42 elapsed days at 3 DKK/kWh, then supply twelve known
hours also at 3. The learned median remains 3, with its ordinary 0.25 uncertainty.
Known-only planning holds a 90% battery. Adding the learned tail lowers the
terminal price from 3 to 2.0625, triggers discharge during the known period,
and ends at 23.52% SOC in the physical replay. Common-inventory-adjusted cost
rises from 3.60 to 4.705294 DKK. No forecast-tail throughput is planned.

`planner._terminal_price` compares known 3 with uncertainty-adjusted forecast
2.75 as if the difference were a genuine price swing, then applies an extra
25% haircut. This contradicts its intended flat-price safeguard. Preserving
the known terminal reference removes this example's cycling, isolating the
cause; that diagnostic is not a validated universal fix for genuine future
price declines. The terminal-value interaction requires correction and a
mixed known/forecast flat-price invariant before claiming overall improvement.

### Applicability to the saved installation

The saved 2026-11-01 capture in
`.playwright-mcp/battery-live-capture-20261101.json` has a selected external
forecast extending to 2026-11-07. Reconstructing its inputs fills the complete
72-hour optimizer horizon, leaving zero room for learned continuation. All
eight full-coverage controls in the economic replay were unchanged by learning.
Thus this implementation cannot improve those normal full-forecast plans:
it acts as a fallback for shorter or unavailable external forecasts. This is
evidence about the saved capture, not a live check of the present installation.

Useful follow-up work is to fix terminal valuation, compare shorter and
recency-weighted profiles on a holdout, and evaluate learned corrections or
challengers against first-issued external forecasts at matching lead times.
Activation should depend on predictive skill and net dispatch value, not only
four matching dates. Seasonal, holiday and quarter-hour variants need their own
evidence rather than additional calendar rules by assumption.

### Reproduction

Download the official query linked above into a temporary JSON file, then run:

```sh
.venv/bin/python scripts/validate_price_patterns.py --input /tmp/house_battery_dk2_validation_20261102.json
.venv/bin/python scripts/validate_pattern_economics.py /tmp/house_battery_dk2_validation_20261102.json --basis both --settlement-price 1 --output /tmp/house_battery_pattern_economics.json
.venv/bin/python -m pytest -q tests/test_house_battery_pattern_validation.py
```

The no-shock terminal example is also reproducible through
`scripts.validate_pattern_economics.terminal_shock_case(shock_price=3, known_hours=12)`.
The scripts retain no raw official data in the repository. No archived external
forecast series or measured historical household load is available here, so
these checks cannot establish improvement over the installed forecast service
or actual household savings.

## Hardened implementation and independent-year check — 2026-11-02

The follow-up work implemented and tested stronger models, repaired terminal
valuation, added source-specific external calibration, and independently audited
the resulting pipeline. The candidate pool was declared before the new runs:
90-day weekday/hour median, 28- and 14-day weekday/weekend/hour medians,
56-day recency-weighted day-type/hour mean, and a flat seven-day baseline.

The 28-day day-type model was selected using target dates before 2026-08-01,
excluding the July economic holdout. Its parameters were locked before obtaining
the independent 2024 archive. The August–October 2026 split is an internal
temporal holdout within a year already explored by this research. The 2024
archive was not examined in that earlier work: it contains 8,784 native hourly
DK2 observations from [Energinet Elspotprices](https://www.energidataservice.dk/tso-electricity/Elspotprices).
It is expanded to four identical quarters only for common reporting; it provides
no intra-hour information. Do not compare absolute MAE across the two different
market years and native cadences as if they were the same experiment.

| Frozen 28-day predictor | New MAE | Previous 90-day MAE | Change |
| --- | ---: | ---: | ---: |
| August–October 2026, 17,372 matched forecast instances | 0.328557 | 0.371613 | 11.6% lower |
| March–December 2024, 58,420 common reporting instances | 0.236743 | 0.243016 | 2.58% lower |

All values are DKK/kWh spot. The experimental automatic candidate switcher did
not improve consistently on the independent year, so it was not promoted.
Production uses the frozen 28-day candidate; the others remain shadow models.
This is a validated fixed policy, not an automatically best model selector.
In particular, pooling weekdays can lose a stable Monday-versus-Tuesday effect
that the exact-weekday shadow model captures better.

Control is now stricter than historical profile readiness. Immutable first-issued
predictions must beat the flat seven-day baseline by at least 10% and
0.01 DKK/kWh at the applicable lead band, using at least 24 distinct target
hours and three local dates with six covered hours each. The approved model
activated on 100% of eligible 2026 holdout instances and 77.36% in 2024.
On the 2024 active subset it was slightly worse than the old model:
0.232205 versus 0.230274, although it beat the flat baseline's 0.308913.
Thus neither the aggregate result nor activation proves universal superiority.

The first-issued lead-specific uncertainty covered 90.79% of active 2026
outcomes and 92.65% in 2024, with mean allowances of 0.76474 and 0.58512.
These are empirical coverage figures, not an independence-based guarantee.
History-only migration starts with no issued skill and collects shadow evidence
before contributing control. Repeated polls, duplicate restored scores, Danish
offsets and repeated DST hours cannot inflate qualification.

### Guarded dispatch results

Terminal credit now distinguishes original inventory from net acquired energy.
An existing holding reference is projected into the forecast price interval;
extra purchases receive only its lower quote. This fixes the flat-price and
recent-shock reproductions without preventing credible price declines or peak
preparation. The former 3.60 → 4.705294 DKK flat-price loss is now exactly zero.
Explicit caller floors are captured separately from displayed quotes for replay.

Forecast skill alone was still insufficient: ungated stronger forecasts changed
charging through terminal acquisition credits and lost money in some replay
windows. Production therefore rejects a learned extension with no meaningful
planned battery movement in its forecast period and replans the trusted prefix
using the same SOC, settings and commitment. It does not copy another policy's
simulated SOC. This is a necessary opportunity guard, not proof of future profit.

| Locked internal economic window | Known-only cost | Guarded cost |
| --- | ---: | ---: |
| 2026-07-20–23, spot | 24.388907 DKK | 24.388907 DKK |
| 2026-10-26–29, spot | 41.319967 DKK | 41.319967 DKK |
| 2026-07-20–23, stylized import tariff | 60.314064 DKK | 60.314064 DKK |
| 2026-10-26–29, stylized import tariff | 77.873115 DKK | 77.873115 DKK |

Across 384 evaluation hours, qualified learned tails were available for 362;
all 362 were terminal-only and were rejected. Actions, targets, SOC and physical
metrics matched the trusted-price baseline, preventing four potential action
and five charge-target changes. No historical learned cycle was accepted in
these windows: the result demonstrates neutrality, not extra savings.
A separate explicit toy forecast with cheap confirmed charging and an expensive
forecast peak was accepted and improved common-inventory-adjusted simulated
cost by 1.215882 DKK. It tests the opportunity path, not market forecasting skill.

### External calibration and independent correctness review

With the existing adaptive and forecast opt-ins, each external source can
qualify a capped, shrunk residual-bias correction at each lead band. Its held-out
validation uses only outcomes available before each prediction was issued,
requires independent covered dates, and rejects unstable or worse tail errors.
The calibrated interval contains the complete original interval, so the shifted
mean cannot create a better trading bound by itself. Synthetic stable biases
of ±0.40 DKK/kWh fell to 0.08 MAE; unbiased noise remained unchanged. A bias
reversal temporarily worsened errors before delayed outcomes revealed it.
The saved configured sources have no sufficient dated residual archive, so
this remains `learning`; no real external-forecast uplift is established.

Independent review additionally found and verified repairs for UTC-versus-Danish
target identity, backdated evidence from using the tariff anchor as issue time,
live-reference exports, duplicate/unfinished-hour evidence, stale-baseline
uncertainty, and forecasts first recorded outside the 72-hour planning window.
Real issuance time is now separate from the stable horizon cap. Persisted
out-of-window vintages are discarded rather than relabeled as valid evidence.

Reproduce model comparison using `scripts/compare_pattern_models.py`; use
`--cutoff 2024-03-01 --locked-model day_type_28d` with the independent hourly
archive. `scripts/validate_pattern_economics.py` applies the same production
opportunity guard by default; `--allow-terminal-only` retains the unguarded
comparison. `scripts/validate_forecast_calibration.py` reproduces the synthetic
feedback checks. The generic `validate_price_patterns.py` now labels the
current qualified production model explicitly; its outputs are not the old
ungated ninety-day audit results above.

The evidence supports a stronger guarded implementation and more honest
forecast qualification. It does not establish a globally optimal algorithm,
reliable holiday prediction, or additional measured household savings.
