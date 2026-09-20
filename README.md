# House Battery for Home Assistant

Local, deterministic battery arbitrage for a home battery. The integration
learns the connected load, evaluates every currently known electricity-price
interval, and selects `charge`, `grid`, or `battery` while accounting for
battery losses, wear, operating limits, and anti-chatter limits.

House Battery connects its optimization layer to a local Home Assistant battery
provider selected during setup. The bundled local-TCP compatibility layer is
kept fail-closed and is being validated against real hardware; until its direct
setup flow is released, use a proven local provider for the device connection.
Project source, releases, and issue tracking are at
[github.com/madslundt/House-battery](https://github.com/madslundt/House-battery).

It is deliberately conservative: battery energy is not spent to save a few
cents, and extra charging is allowed only when the full known price spread is
profitable after losses and degradation.

## What it provides

- A 15-minute battery plan across all contiguous known price intervals.
- Local control through already-proven battery Home Assistant entities; no
  cloud account, raw network command implementation, or electrical wiring
  control is added here.
- Continuous load-profile learning and conservative usable-capacity and
  round-trip-efficiency estimates.
- Expected and realized savings, charge/discharge throughput, equivalent
  cycles, estimated degradation, a decision history, diagnostics, and a JSON
  evidence export.
- An independent **Grid available** entity. It measures whether an on-grid
  supply exists, not whether the load is currently using grid power.
- Explicit commissioning and a separate **Automatic control** switch. New
  entries start in safe shadow mode.

## Safety boundary

This is an economic controller, not a transfer switch, UPS safety controller,
or battery-management system. The battery firmware and your installed local
integration remain responsible for electrical protection and device limits.

Do not enable automatic control until every configured mode, SOC limit,
read-back, load path, and local electrical/export setting has been tested. Use
only a physical on-grid status for **Grid available**; never substitute a grid
power sensor. If grid availability is unknown, telemetry is stale, the device
is offline/faulted, or grid availability becomes false, the optimizer stops
economic execution. An outage changes the state to `OUTAGE`, clears the plan,
requests safe mode when automatic control was enabled, and latches automatic
control off.

The **Absolute emergency SOC** setting is written to the configured native
minimum-SOC control whenever automatic control issues a command. Its final
cut-off behavior is defined by the battery firmware, so verify that behavior
on your equipment before relying on it during an outage.

## Installation

This repository is a Home Assistant custom-integration project. Copy the
integration directory into your Home Assistant configuration directory, then
restart Home Assistant:

```text
<config>/custom_components/house_battery/
```

Add **House Battery** from *Settings → Devices & services → Add
integration*. It does not discover a battery; instead, its config flow binds
the local entities already supplied by your battery/local-control setup and
your electricity-price integration.

For development, this repository keeps the component in
`custom_components/house_battery` so Home Assistant can load it directly
from the project configuration directory.

## First configuration

The config flow requires the following bindings.

| Binding | Purpose |
| --- | --- |
| Battery state of charge | Current battery SOC in percent. |
| Connected/house load power | Load that the battery can actually serve, in W. |
| Grid import power | Imported grid power, in W. |
| Grid available / on-grid state | A physical or device-reported on-grid state. |
| Battery Operating Mode | The verified local `select` control. |
| Electricity price forecast entities | Entities with dated price intervals. |
| Battery charge power | Measured battery charging power, in W. |
| Battery discharge power | Measured battery discharging power, in W. |

Grid export, PV input, fault/online status, and charge/discharge power controls
are optional. Native minimum/maximum SOC controls are required before
commissioning and enabling automatic control; the optimizer verifies their
bounds and synchronizes them with its economic and emergency policy.

Full field semantics and the commissioning checklist are in
[configuration](docs/configuration.md).

## How the optimizer decides

On each one-minute refresh it:

1. Rejects stale, unavailable, faulted, offline, or unknown on-grid telemetry.
2. Normalizes the known price forecast into contiguous 15-minute intervals.
3. Predicts load for each interval from the learned 7-day time-of-week profile,
   recent observations, and manually scheduled loads.
4. Solves a deterministic dynamic-programming plan subject to SOC, capacity,
   charge/discharge power, minimum mode duration, and daily transition limits.
5. Includes round-trip losses, switching cost, degradation cost, and the
   configured required profit before using battery energy.
6. Exposes the plan and reasons, or stays in shadow mode until explicitly
   commissioned and enabled.

The normal economic floor is **Arbitrage reserve SOC**. The separate
**Absolute emergency SOC** is a lower native limit for preserving battery
energy in an outage. The invariant is always:

```text
0 ≤ absolute emergency SOC ≤ arbitrage reserve SOC
  < maximum charge SOC ≤ extra-storage charge SOC ≤ 100
```

### Extra storage for unusually large price spreads

Set an **Extra-storage charge SOC** above the normal **Maximum charge SOC** and
choose an **Extra-storage price spread**. The optimizer raises its effective
charge ceiling only when all of the following are true:

```text
highest known price − lowest known price ≥ extra-storage price spread

highest known price − (lowest known price / round-trip efficiency)
  − degradation cost ≥ minimum required profit
```

This merely permits extra stored energy; the planner still decides whether a
specific charge interval is useful. The `Extra storage policy`, `Known price
spread`, `Best effective price margin`, and `Effective charge target SOC`
entities make the result visible.

See [architecture and economics](docs/architecture.md) for the exact planning
model.

## Important entities

Entity IDs are generated from the entry name, so use Home Assistant's entity
picker rather than copying assumed IDs. The integration creates:

- State and action: **Optimizer state**, **Current decision**, **Battery mode**,
  **Operation plan**, **Extra storage policy**, and **Decision history**.
- Safety: **Optimizer problem** and **Grid available**.
- Live telemetry: SOC, connected-load power, grid import/export, battery
  charge/discharge power, PV power, and current price.
- Economy: expected savings, realized savings today/month/total, known spread,
  and effective margin.
- Battery health: charge/discharge energy today/month/total, equivalent full
  cycles, estimated degradation, estimated remaining capacity, learned usable
  capacity, and learned round-trip efficiency.
- Learning: load-learning coverage, forecast error, and the **Battery
  learning** status entity.
- Controls: **Automatic control**, **Force safe mode**, and all policy numbers.

The **Operation plan** has compact contiguous `blocks` attributes containing
start/end, action, expected savings, energy, SOC range, and explanation. The
underlying per-slot plan is included in diagnostics and the data export.

## Configuration numbers

The integration creates persistent number entities, initially set to these
conservative defaults:

| Setting | Default | Meaning |
| --- | ---: | --- |
| Nominal battery capacity | 1.958 kWh | Fallback until a usable capacity is learned. |
| Absolute emergency SOC | 10% | Native lower floor used by automated local commands. |
| Arbitrage reserve SOC | 20% | Economic floor; the planner will not discharge below it. |
| Maximum charge SOC | 90% | Normal charge ceiling. |
| Extra-storage charge SOC | 100% | Ceiling allowed only under the extra-storage policy. |
| Fallback round-trip efficiency | 85% | Used until a measured estimate is ready. |
| Battery degradation cost | 0.35 DKK/kWh | Wear charged to battery discharge. |
| Minimum required profit | 0.75 DKK/kWh | Margin required before discharge. |
| Extra-storage price spread | 2.00 DKK/kWh | Raw spread required before raising the ceiling. |
| Minimum mode duration | 30 min | Limits mode chatter. |
| Maximum daily mode transitions | 4 | Limits wear and control churn. |

Choose values that match your battery, tariff, and risk tolerance. A number
change is rejected if it violates the SOC ordering above.

## Examples

Examples for a dashboard, physical grid-status template, scheduled dishwasher
load, data export, and tuning are in [docs/examples.md](docs/examples.md).

## Evidence and troubleshooting

Use *Settings → Devices & services → House Battery → Download
diagnostics* for a redacted snapshot. The
`house_battery.export_data` service returns a structured JSON evidence
bundle with settings, current status, load/battery models, interval ledger,
decisions, and scheduled loads for offline or LLM analysis.

`Optimizer problem` includes the exact stale/unavailable/fault/offline reason.
Do not bypass it by inventing a state or switching on automatic control; repair
the entity mapping or local battery connection first.

## Connect the battery locally

1. Give the battery a DHCP reservation/static IP and confirm its local-control
   provider can read SOC, charge/discharge power, and Operating Mode.
2. Verify the provider's local connection without the vendor app holding the
   only device session. For AECC-compatible batteries, local TCP commonly uses
   port `8080`; use the provider's documented connection test.
3. Add House Battery and bind that provider's SOC, power, Operating Mode, and
   native min/max-SOC entities. Bind a separate physical on-grid entity and
   your price forecast/load entities.
4. Keep House Battery in `SHADOW` while you test `Charge`, `Idle`, and
   self-consumption locally. Commission it only after every read-back is
   correct.

## Evaluate degradation, ROI, and configuration quality

Use observed evidence, not a single “savings” number. Review these after at
least two representative tariff weeks:

- **Battery charge/discharge total** and **Equivalent full cycles** measure
  throughput. If throughput rises while realized savings do not, increase the
  minimum-profit margin, degradation cost, or switching penalty.
- **Estimated degradation**, **Learned usable capacity**, and **Learned
  round-trip efficiency** indicate whether the fallback model is still honest.
  Treat these as estimates; compare them with BMS/manufacturer diagnostics.
- **Estimated realized savings today/month/total** should be compared with a
  no-battery baseline over matching days. A practical ROI estimate is
  `lifetime realized savings ÷ installed battery cost`; use month-level values
  rather than a single volatile day.
- **Expected plan savings** versus realized savings and **Load forecast mean
  absolute error** show model quality. Large, persistent forecast error means
  inspect the load sensor scope, add scheduled loads, or wait for more history.
- **Extra storage policy** should be active only on large, clearly profitable
  spreads. If it frequently fills to 100% without later discharge, raise its
  spread threshold or lower its target.

Change one setting at a time, leave it for several comparable price horizons,
then export evidence with `house_battery.export_data`. Keep a small change log
of tariff conditions, settings, cycles, realized savings, and observed device
behavior so later tuning is evidence-based.

## Development

```bash
uvx ruff check custom_components/house_battery tests/test_house_battery_*.py
uv run --with pytest --with 'homeassistant>=2025.1' \
  pytest -q tests/test_house_battery_*.py
```

The core planner, policy, learning, and accounting behavior is covered by unit
tests. The adapter deliberately issues Home Assistant service calls only; use a
real commissioned local battery setup to validate device-specific modes and
limits.
