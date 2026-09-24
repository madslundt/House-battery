# Architecture, economics, and evidence

## Local data flow

```text
Local battery-provider entities + price forecast + load measurement
                         │
                         ▼
                  coordinator (1 min)
       health gate → forecasting → deterministic planner
                         │
                         ├── shadow-plan entities and evidence ledger
                         ▼
           commissioned automatic control only
                         │
                         ▼
      native SOC/power controls + local Operating Mode select
```

The integration never controls relays, grid wiring, or a transfer switch. The
optimizer consumes verified local battery-provider entities; its direct TCP
compatibility layer remains behind hardware validation and is not an automatic
fallback.

## Forecasting

`LoadLearner` continuously records observed load in 15-minute buckets. Its
forecast combines a time-of-week profile over the preceding seven days with a
recent-observation blend. The plan adds any future manual scheduled loads to
that baseline. The integration exposes coverage and mean absolute forecast
error so poor evidence is observable rather than hidden.

`BatteryLearner` accumulates stable observed charge/discharge windows to
estimate usable capacity and round-trip efficiency. Until sufficient evidence
exists, it uses the configured nominal capacity and fallback efficiency. The
estimate and readiness/sample counts are exposed by **Battery learning**.

## Planner

The pure planner operates over only valid, chronological, contiguous price
slots. It retains each provider's interval duration, so hourly known prices can
join quarter-hour external forecasts. It builds a dynamic-programming state
space:

```text
(stored-energy step, current action, remaining mode lock, transitions used)
```

For each slot it considers `grid`, `charge`, and `battery`, constrained by:

- reserve and target SOC;
- learned/configured usable capacity;
- charge/discharge power limits;
- round-trip efficiency;
- minimum mode duration;
- maximum daily transitions; and
- expected battery-served load.

Ties are resolved deterministically by cost, transition count, throughput, and
action name. Given the same telemetry, stored learning state, settings, and
price rows, the plan is reproducible.

### Economic guard rails

Stored energy is valued by its future opportunity cost, not by the cheapest
future *charging* price. A discharge is eligible whenever it is genuinely better
than holding that energy for a later interval or for its terminal value: the
discharge price minus degradation must exceed the value of keeping the energy
(the part of the horizon that follows). The cheapest-charge price is therefore
no longer a universal discharge prohibition; it survives only in two defensive
places, not as a gate on already-stored energy:

- the physical `force_grid_exit`: if the battery is locked in Self-Gen and
  would drop toward the reserve, it exits to Grid instead of discharging into a
  falling price; and
- the conservative forecast charge gate: an uncertain forecast charge is
  rejected when its worst-case price is above the round-trip floor.

Discharge still carries the configured degradation cost and required-profit
margin in the optimization objective (the required margin is *not* subtracted
from the terminal value, which would double-count it). A switching penalty and
minimum mode duration make small, frequent changes unattractive. Energy left
above reserve receives a terminal value based on the latter part of the known
horizon, preventing the planner from emptying a useful battery solely because
the horizon ends.

### Zero export is enforced at the command boundary, not only by the plan

The plan already caps discharge at the *forecast* load, but a fixed-power local
slot can still export when the actual load drops below the setpoint. Zero export
is therefore a physical invariant enforced in every layer:

- the planner caps discharge at the predicted load;
- the execution layer caps the commanded discharge at the *measured* load minus
  a small safety margin, on every refresh, so a load dip below the setpoint
  cannot push energy back to the grid; and
- accounting and telemetry report grid export as a first-class quantity so any
  export is immediately visible rather than clamped away.

See `docs/commissioning.md` for how to verify the invariant on the physical unit.

### Extra-storage policy

The policy is an explainable adjustment around—not a bypass of—the planner. It
can raise the effective target from normal maximum SOC to extra-storage SOC
only if the raw known spread and loss/wear-adjusted margin both meet the user
settings. The planner then applies the same constraints and cost model to the
higher ceiling.

## Accounting and degradation

The evidence ledger is updated in 15-minute intervals from sampled observed
connected-load power, grid-import power, battery charge/discharge power,
SOC, the current price, and the optimizer's current action. It records
estimated realized savings and charge/discharge energy for today, month, and
lifetime. Forecast load and future plan values are not treated as observed
ledger inputs.

Equivalent full cycles are lifetime battery discharge energy divided by usable
capacity. Estimated degradation uses learned capacity when it is ready;
otherwise it is a conservative cycle-life-reference estimate. These are
estimates for decision support—not an authoritative battery-health report from
the battery BMS.

## Explainability and export

Every plan slot has an action, price, predicted load, energy flows, SOC start
and end, baseline/interval cost, and textual reason. The public plan entity
groups adjacent identical actions into readable blocks. A bounded decision
history stores changes in state/reason with timestamp, SOC, price, and command
outcome.

Call `house_battery.export_data` to return the complete local evidence
bundle:

- current settings and status;
- load and battery learner state;
- interval ledger;
- decision history; and
- scheduled loads.

Home Assistant diagnostics provides the same model with common sensitive
configuration fields redacted. This supports offline/LLM analysis without
requiring cloud telemetry from the battery.
