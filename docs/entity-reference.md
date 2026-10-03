# Entity, action, and configuration reference

Home Assistant generates the exact entity IDs from your entry name. Use the
entity picker; names below are the stable, user-facing names.

## Live state and safety

| Entity | What it means | How to use it |
| --- | --- | --- |
| **Optimizer state** | `BOOTSTRAP`, `SHADOW`, `ACTIVE`, `RECOVERING`, `DEGRADED`, or `OUTAGE`. | Only `ACTIVE` permits automatic writes. `RECOVERING` is a two-minute, write-paused grace period for a lost direct local TCP connection; read its `reason` attribute when it is not active. Errors pause writes without turning off the automatic-control switch; valid inputs resume operation automatically. |
| **Current plan slot** | The current planner action: `charge`, `grid`, `battery`, or `safe`. The slot start/end, reason, and expected battery movement are attributes. | This is the single source for the current planned action. Compare its state with **Battery activity**, which is measured independently. |
| **Battery activity** | `charging`, `discharging`, or `idle` based on physical power telemetry. Direct-local FBP1200 entries use reported battery charge/output power because they lack a CT meter; legacy entries use the measured load/grid balance. | Compare with **Current plan slot** to see whether measured battery movement matches the plan. |
| **Operation mode** | House Battery override selector: `auto`, `charge`, `battery`, or `grid`. | `auto` follows the optimizer. Forced actions are available only while automatic control is on; disabling control or pressing Force safe returns this selector to `auto`. |
| **Grid available** | Whether an on-grid supply physically exists. | This is not grid import. `off` produces `OUTAGE` and stops economic control. |
| **Optimizer problem** | `on` when required telemetry is stale, invalid, faulted, offline, or grid status is unknown. | Treat it as a stop signal. Its `problems` attribute names the failed binding. |
| **Export detected** | `on` when a configured grid meter reports power flowing to the grid above the noise floor. | Direct-local FBP1200 entries without a CT meter report this as unavailable; their manual discharge is capped below connected load and fails closed when that load is unavailable. |
| **Export safety fault** | `on` after a configured meter reports export above the safety threshold. It latches and blocks inverter writes. | Meter-based fail-closed protection is available only when a grid meter is configured. After export stops, turn Automatic control off then on to reset the fault; the fault never turns the switch off by itself. |
| **Automatic control** | Explicit permission for House Battery to issue local mode/limit writes. | Leave off during setup. It cannot turn on until the entry is commissioned and native SOC controls pass validation. |
| **Use external price forecast** | Enables valid, fresh forecast intervals after the end of known prices. | Leave it off while measuring forecast quality. Its `status` attribute explains `used`, `stale`, `invalid`, `empty`, or another non-use result; none of these affects known-price planning. |
| **Allow opportunistic full charge** | Permits the optimizer to compare the normal target with the optional higher target. | Enable it only if occasional 100% charging is acceptable. Its attributes show whether the higher target is currently active, the incremental savings, and the reason. |
| **Force safe mode** | Button that turns automatic control off and requests the configured safe local mode. | Use immediately if real battery behavior disagrees with the plan. |

Example: if **Current plan slot** says `battery` while **Battery activity** is
`idle`, inspect **Optimizer problem**, the local provider, and the mode read-back.
The integration retries on its next refresh and reloads itself after repeated
local TCP or command failures.

## Telemetry and economics

| Entity | Plain-language description |
| --- | --- |
| **Battery state of charge** | Current usable battery percentage reported directly by the battery. |
| **Connected load power** | Power currently demanded by the load the battery can actually serve. It trains the forecast. |
| **Local load diagnostics** | Direct-local entries only. Comparison of the FOSSiBOT whole-site meter, smart-load total, backup-load total, and every storage unit's off-grid reading. The complete per-storage off-grid total is automatically used for learning, planning, and automatic control; incomplete stack data fails closed. |
| **Grid import power** | Current whole-site power bought from the grid; used for balanced evidence, export monitoring, and realized savings when paired with a whole-house load sensor. Optional for direct-local FBP1200 entries. |
| **Grid export power** | Whole-site power sent to the grid. Requires a measured grid meter; direct-local FBP1200 entries without a CT meter cannot report it. |
| **Battery output power** | Legacy entries infer battery output from the measured load/grid balance. Direct-local entries show the inverter's reported output, used for throughput accounting and actual-use history. A CT meter and whole-house load sensor are needed for balanced realized-savings accounting. |
| **Power source** | Where the load is served from right now: `charging` (battery charging), `battery` (battery serving load), `grid` (battery idle while load is present), or `off` (no measured load). Legacy entries use the load/grid balance; direct-local entries use reported battery direction. |
| **Native minimum/maximum SOC** | Read-only diagnostics for the battery's hardware SOC registers. | House Battery validates and writes limits through its local adapter; these sensors show the latest readback. |
| **Current electricity price** | Price for the current known price interval. |
| **Expected plan savings** | Forecast saving across the current planning horizon (known prices plus any enabled, valid extension) versus buying expected load from grid. It is a forecast, not cash earned. |
| **Estimated realized savings today/month/total** | Ledger estimate from sampled observed power and price. Direct-local FBP1200 entries need both a CT meter and whole-house load sensor for balanced grid/battery savings. Compare matching tariff periods, not one unusual day. |
| **Average charge price / discharge value** | Energy-weighted known tariff for charge/discharge energy estimated by integrating reported battery power. Corresponding DKK cost/value totals are available. These are battery-energy tariff estimates, not a complete household bill calculation. |
| **Known price spread** | Highest minus lowest price in the known future horizon. |
| **Best effective price margin** | Best spread after round-trip losses and degradation cost. It must clear **Minimum required profit** before battery cycling is worthwhile. |
| **Effective charge target SOC** | Actual ceiling for this plan: normal maximum or the temporary opportunistic ceiling. |
| **Extra storage policy** | `normal` or `active`, with the effective target and explanation in attributes. It becomes active only when the opt-in higher-target plan completes an additional profitable cycle using known prices and returns below the normal target within that horizon. |

Example: with prices of 0.20 and 4.00 DKK/kWh, 85% efficiency, and 0.35
DKK/kWh degradation cost, effective margin is about 3.41 DKK/kWh. If the
required profit is 0.75, **Extra storage policy** can become `active` when the
known expensive window has enough forecast load to use the extra energy. With
1.90 and 2.25 prices, it remains `normal`; cycling is not worth it.

## Battery use and learning

| Entity | Plain-language description |
| --- | --- |
| **Battery charge/discharge today/month/total** | Energy estimated by integrating measured battery charge/output telemetry. These are throughput estimates, not grid-meter billing totals. |
| **Equivalent full cycles** | Lifetime discharged energy divided by usable capacity. One 1.958 kWh discharge is roughly one equivalent cycle. |
| **Estimated battery degradation** | Capacity loss estimated from learned capacity when available, otherwise from cycle-life reference. It is not a BMS warranty value. |
| **Estimated remaining capacity** | 100% minus the estimated degradation. |
| **Learned usable capacity** | Capacity inferred from sufficiently stable charge/discharge observations. |
| **Learned round-trip efficiency** | Measured energy-out versus energy-in estimate once enough stable samples exist. |
| **Measured round-trip efficiency** | Read-only comparison estimate from measured charge and discharge energy over the recent seven-day window. It is shown only when the window begins and ends within 5 SOC percentage points; it does not affect planning. Its attributes include the configured fallback value and measurement details. |
| **Battery learning** | `learning` until both capacity and efficiency are reliable; attributes show sample counts/readiness. |
| **Load learning coverage** | Percentage of the 7-day, 15-minute demand profile with enough observations. Higher is better. |
| **Load forecast mean absolute error** | Typical absolute load-forecast error in W. A persistent high value means inspect the load sensor scope or add scheduled loads. |
| **External price forecast accuracy** | `unknown`, `excellent`, `good`, `fair`, or `poor` for the displayed primary source. The `price_forecast_sources` attribute provides independent MAE, bias, sample count, buffer coverage, update time, and validation status for every configured source. | A positive 0.18 DKK/kWh bias means that source is normally 0.18 too high; raise the uncertainty buffer or keep use disabled if that makes marginal cycles unsafe. |

Example: if cycles rise by 12/month while realized monthly savings stay near
zero, raise **Minimum required profit** from 0.75 to 1.00 DKK/kWh or raise
**Battery degradation cost**. If learned capacity falls materially while the
manufacturer/BMS reading does not, first verify charge/discharge telemetry and
SOC calibration; do not assume a warranty issue from this estimate alone.

## Planner entities and attributes

| Entity | What to inspect |
| --- | --- |
| **Operation plan** | `blocks` attribute: contiguous start/end times, action, expected savings, energy, SOC start/end, and reason. Also exposes baseline cost, expected cost, terminal price, and horizon length. |
| **Actual battery history** | Measured charge/discharge/idle blocks with start/end time and SOC start/end/min/max; energy is estimated by integrating reported battery power. The sensor exposes today; `export_data` retains up to eight local days. The timeline is based on physical telemetry, not the plan or commanded mode. |
| **Decision history** | Recent state/reason changes with timestamp, SOC, price, action, and command result. Use it to explain why the plan changed. |
| **Current plan slot** | Its state is the current planner action. Attributes include the active executable price interval, planned load, grid import, charge/discharge energy, SOC path, price provenance, conservative forecast buffer, costs, and reason. | Use this entity for the current action and its supporting evidence; the separate **Operation plan** contains the full-day timeline. |
| **Planned load power** | Average connected-load forecast for the active plan slot, including scheduled loads. | Compare it with **Connected load power** over matching intervals to find systematic forecast bias. |
| **Plan execution** | A live comparison between planned battery movement and measured charge/discharge power. | `matching` is encouraging; investigate a repeated non-matching state over a completed price slot. A momentary mismatch is not proof of a failed command. |

Example: a plan block of `charge`, 01:00–03:00, SOC 25→90, followed by
`battery`, 17:00–20:00, is evidence that House Battery found a complete,
profitable cycle. A `grid` block during a small price spread is intentional,
not a missed optimization.

## Configuration numbers

All values are persistent inputs. This ordering is enforced:

```text
absolute emergency SOC ≤ arbitrage reserve SOC
  < maximum charge SOC ≤ 100
```

| Number | What changing it does | Realistic change |
| --- | --- | --- |
| **Nominal battery capacity** | Fallback usable capacity until learning is ready. | Set 1.958 kWh for one module; use the installed usable capacity, not a marketing nominal total. |
| **Absolute emergency SOC** | Native lower hardware floor written during automatic commands. | Raise 10→20% if outage reserve is more valuable than arbitrage. |
| **Arbitrage reserve SOC** | Planner's no-discharge floor. | Raise 20→35% before a storm; the optimizer keeps more backup but has less energy to sell against peak prices. |
| **Maximum charge SOC** | Normal charge ceiling. | Lower 90→80% to reduce high-SOC dwell time; it may skip otherwise profitable evening coverage. |
| **Maximum AC charge power** | Maximum power budget used for charging. | The available range tops out at 1200 W; lower it if the battery or electrical installation needs a conservative cap. |
| **Maximum discharge power** | Planner and native command power cap. | Lower discharge 800→500 W if the load path or battery behaves better at a lower sustained output. |
| **Fallback round-trip efficiency** | Used before measured efficiency is ready. | Set 80% rather than 85% to make early plans more conservative. |
| **Battery degradation cost** | Wear cost charged to each discharged kWh. | Raise 0.35→0.60 DKK/kWh if avoiding wear matters more than short-term savings. |
| **Minimum required profit** | Extra margin required for discharge. | Raise 0.75→1.25 DKK/kWh to reject marginal cycles. |
| **Cycle-life reference** | Legacy fallback degradation model denominator. | Available only for legacy provider entries. Direct-local entries use an internal default because they lack a trustworthy grid/battery throughput balance. |
| **External price forecast uncertainty** | Conservative error allowance for external price forecasts. Disabled by default. | Set 0.25 DKK/kWh if the forecast's MAE is about 0.20; forecast charging is evaluated 0.25 higher and discharge 0.25 lower. |

The mode-switching penalty is an internal fixed cost of 0.05 DKK per action
change. Opportunistic full-charge planning uses a fixed 100% target. Detailed
throughput, learning, decision
history, and local-load diagnostic entities are disabled by default; enable
them from the entity registry when investigating behavior. These settings and
diagnostics remain out of the default control surface because changing or
watching them adds tuning surface without improving routine operation.

Change one number at a time. Observe at least several similar tariff days, then
compare **realized savings**, **equivalent cycles**, forecast error, and the
plan/decision history before keeping or reverting it.

## Services

| Service | Action | Example result |
| --- | --- | --- |
| `house_battery.schedule_load` | Adds known future demand and immediately replans. | Add a dishwasher at 800 W from 19:00–21:00; the planner can preserve or charge energy before the peak. |
| `house_battery.clear_scheduled_loads` | Removes all manually scheduled future loads and replans. | Run it after cancelling an EV charge so the battery does not retain unnecessary energy. |
| `house_battery.export_data` | Returns settings, plan, learning models, ledger, decisions, and scheduled loads as JSON. | Export monthly evidence before changing degradation or profit settings. |

With multiple House Battery entries, provide `config_entry_id` to every
service. See [examples](examples.md) for exact service YAML.
