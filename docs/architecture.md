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

### The power-flow model: battery flow is derived, never read from the device

The FBP1200 reports raw charge/output power in its EMS registers, but those
figures include converter loss and are not a trustworthy measure of energy
actually moving in or out of the cells. The integration therefore does not feed
any device-reported charge/discharge power into planning, accounting, or
learning. Instead it computes a canonical power flow every refresh from three
first-class inputs:

- connected-load power (from the configured load entity),
- grid power (signed at the grid meter: positive import, negative export), and
- the battery SOC.

Battery charge power is what the load *isn't* taking from the grid-plus-battery
sum while SOC is rising; battery output power is the residual the battery
supplies when load exceeds grid import. The raw device charge/output powers are
kept as diagnostics only (`battery_charge_power_w` / `battery_output_power_w`
downstream of the device) and are exposed for inspection, never for control.

### Zero export enforcement

The control method depends on the available hardware feedback:

- **Native integrations use Self-Gen / Zero Export.** When a trustworthy native
  meter is available, the inverter owns the real-time zero-export loop.
- **Direct-local FBP1200 entries use a bounded manual slot.** This device has no
  CT/grid-meter signal and its Self-Gen mode remains idle. The actuator caps
  Discharge below the fresh connected-load reading by 50 W and writes Idle/0 W
  when that reading is missing or invalid.
- **A configured meter independently verifies the result.** The coordinator
  decomposes the grid-meter reading into import and export. Any export above a
  small noise floor lights an `Export detected` binary sensor; export above a
  slightly higher safety floor while automatic control is enabled latches an
  `Export safety fault` that **disables all inverter writes** until the operator
  resets execution. This is fail-closed: an export can never continue through
  the next planning cycle.
- **The planner still keeps a head of safety.** The planner does not schedule
  more discharge than forecast load, while the actuator independently uses the
  current measured load and a safety margin to
  avoid exporting; it can value stored energy on its genuine opportunity cost.
- **Accounting and telemetry report grid export as a first-class quantity** so
  any export stays immediately visible rather than being silently clamped away.

The battery's energy flow used everywhere downstream (planning targets,
savings, degradation, learning, the `Battery activity` sensor, and the
`Power source` sensor) is therefore derived from the load/grid/SOC balance and
is reconciled against the meter, never copied from the device.

See `docs/commissioning.md` for how to verify the invariant on the physical unit.

### Opportunistic full-charge policy

The policy is an explainable adjustment around—not a bypass of—the planner. It
builds normal-target and higher-target candidates with the same constraints
and cost model. The higher target wins only when the user has opted in, known
prices support both the extra charge and later discharge, the energy returns
below the normal target within the known horizon, and realized savings improve
after losses, wear, switching, and the required profit hurdle. Terminal value
and forecast-only opportunities cannot activate it.

## Accounting and degradation

The evidence ledger is updated in 15-minute intervals from sampled observed
connected-load power, grid power (decomposed into import and export), the
derived battery charge/output power, SOC, the current price, and the
optimizer's current action. It records estimated realized savings and
charge/discharge energy for today, month, and lifetime. Forecast load and future
plan values are not treated as observed ledger inputs. Export is reported in the
ledger but never credited: exported energy is not valued as avoided import
because there is no export contract to settle against.

Equivalent full cycles are lifetime battery discharge energy divided by usable
capacity. Estimated degradation is applied to discharged energy through the
learned capacity when it is ready; otherwise it is a conservative
cycle-life-reference estimate. These are estimates for decision support—not an
authoritative battery-health report from the battery BMS.

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
