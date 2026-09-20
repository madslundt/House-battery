# Architecture, economics, and evidence

## Local data flow

```text
Local FBP1200 entities + price forecast + load measurement
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

The integration never issues raw TCP commands or controls relays, grid wiring,
or a transfer switch. The Home Assistant local adapter is the only actuator.

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

The pure planner operates over only valid, chronological, contiguous,
15-minute price slots. It builds a dynamic-programming state space:

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
- expected load minus expected PV.

Ties are resolved deterministically by cost, transition count, throughput, and
action name. Given the same telemetry, stored learning state, settings, and
price rows, the plan is reproducible.

### Economic guard rails

Battery discharge is eligible only when it clears this price floor:

```text
lowest known price / round-trip efficiency
  + degradation cost + minimum required profit
```

Discharge carries the configured degradation cost and required-profit margin in
the optimization objective. A switching penalty and minimum mode duration make
small, frequent changes unattractive. Energy left above reserve receives a
terminal value based on the latter part of the known horizon, preventing the
planner from emptying a useful battery solely because the horizon ends.

### Extra-storage policy

The policy is an explainable adjustment around—not a bypass of—the planner. It
can raise the effective target from normal maximum SOC to extra-storage SOC
only if the raw known spread and loss/wear-adjusted margin both meet the user
settings. The planner then applies the same constraints and cost model to the
higher ceiling.

## Accounting and degradation

The evidence ledger is updated in 15-minute intervals using observed
charge/discharge power, current price, expected load, and planned actions. It
records estimated realized savings and charge/discharge energy for today,
month, and lifetime.

Equivalent full cycles are lifetime battery discharge energy divided by usable
capacity. Estimated degradation uses learned capacity when it is ready;
otherwise it is a conservative cycle-life-reference estimate. These are
estimates for decision support—not an authoritative battery-health report from
the FBP1200 BMS.

## Explainability and export

Every plan slot has an action, price, predicted load, energy flows, SOC start
and end, baseline/interval cost, and textual reason. The public plan entity
groups adjacent identical actions into readable blocks. A bounded decision
history stores changes in state/reason with timestamp, SOC, price, and command
outcome.

Call `fossibot_fbp1200.export_data` to return the complete local evidence
bundle:

- current settings and status;
- load and battery learner state;
- interval ledger;
- decision history; and
- scheduled loads.

Home Assistant diagnostics provides the same model with common sensitive
configuration fields redacted. This supports offline/LLM analysis without
requiring cloud telemetry from the battery.
