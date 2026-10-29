# Architecture, economics, and evidence

## Local data flow

```text
Direct-local battery telemetry or legacy provider entities + price + load
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
      native SOC/power controls + local battery mode command
```

The integration never controls relays, grid wiring, or a transfer switch. The
direct-local TCP adapter supplies FBP1200 telemetry and control. Legacy entries
can use entities from an existing battery provider instead.

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
(stored-energy step, current action)
```

For each slot it considers `grid`, `charge`, and `battery`, constrained by:

- reserve and target SOC;
- learned/configured usable capacity;
- charge/discharge power limits;
- round-trip efficiency;
- expected battery-served load.

Ties are resolved deterministically by total cost, battery throughput, and
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
from the terminal value, which would double-count it). A fixed internal
switching penalty makes small, frequent changes unattractive. Energy left
above reserve receives a terminal value based on the latter part of the known
horizon, preventing the planner from emptying a useful battery solely because
the horizon ends.

### Battery telemetry and balanced household flow

The FBP1200 reports charge/output power in its EMS registers. House Battery
uses those readings as estimated battery throughput and in the persisted actual
activity timeline; it does not use them for battery-capacity or efficiency
learning. Verified Idle input is excluded from battery throughput when the full
control bank and every storage unit independently confirm zero battery flow;
the raw power readings remain available in diagnostics. A separate canonical household flow is computed when an independent
whole-house load measurement and grid meter are both available:

- whole-house load power (from the optional accounting-load entity for
  direct-local entries, or the configured load entity for legacy entries),
- grid power (signed at the grid meter: positive import, negative export), and
- the battery SOC.

Battery charge/output energy is paired with the known interval price to report
weighted average charge price, charge cost, discharge value, and actual
charge/discharge/idle blocks with observed SOC. These are battery telemetry
estimates. Balanced whole-house flow is still required for measured realized
savings. Direct-local entries need both a CT meter and a whole-house load
entity for that calculation; the CT meter alone enables export monitoring.

The **Measured round-trip efficiency** diagnostic divides recent measured
discharge energy by charge energy when the seven-day window starts and ends
within five SOC percentage points. It is kept separate from the planner's
efficiency input and exists only for comparison with the configured fallback.

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
  slightly higher safety floor while automatic control is enabled raises a
  latched `Export safety fault` that **pauses inverter writes**. This is
  fail-closed: an export can never continue through the next planning cycle.
  Authorization remains armed; after export stops, an operator off/on cycle
  resets the fault.
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
and cost model. The higher target wins only when the user has opted in, it adds
meaningful charging above the normal target, and the risk-adjusted objective
improves after losses, wear, switching, and the required profit hurdle. Enabled
forecasts retain their uncertainty buffers. Conservative terminal carryover value
can also justify the higher target; a complete extra cycle within known prices
is not required. Manual overrides retain the configured normal charge ceiling.

## Accounting and degradation

The evidence ledger is updated in 15-minute intervals. Direct-local battery
telemetry supplies charge/discharge energy and its interval-price valuation;
legacy entries use their balanced measured flow. A complete load/grid balance
is required before realized savings are recorded. Forecast load and future plan
values are not treated as observed ledger inputs. Export is reported in the
ledger but never credited: exported energy is not valued as avoided import
because there is no export contract to settle against. Separately, the actual
daily history coalesces measured charging, discharging, and idle samples into
blocks with SOC start/end/range and energy totals, retaining eight local days.

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

Retention runs on every refresh and storage load/save, including after offline
gaps or when planning inputs are unavailable:

| Data | Retention |
| --- | --- |
| Consumption model | At most 672 weekday/quarter-hour summaries, with a capped effective weight of 12 weekly observations so changing habits can replace old ones. Buckets unused for 90 days expire. Existing profiles are preserved on upgrade and start their expiry clock then. |
| Recent consumption | Eight interval averages; discarded after 30 minutes without a fresh observation. No raw minute-by-minute consumption archive. |
| Interval ledger | At most 5,952 intervals and 62 days, preserving current/previous calendar-period reporting. Lifetime counters survive detail pruning. |
| Actual activity | Eight local calendar dates of coalesced blocks. |
| Decisions / transitions | Seven days / 24 hours, also capped at 500 / 100 entries. |
| Forecast comparisons | At most 1,000 pending intervals per source; unmatched intervals expire after seven days. Completed scores are capped at 1,000. Removed sources are discarded. |
| Scheduled loads / daily plan | Expired scheduled loads are removed; only the current local day's plan persists. |

Explanatory entity attributes remain available live but are excluded from Home
Assistant Recorder, avoiding repeated copies of plans, timelines, and diagnostic
payloads. Entity states and numeric statistics metadata remain recordable.
Recorder's existing history and long-term statistics follow Home Assistant's
own retention settings; this integration does not purge that shared database.

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
