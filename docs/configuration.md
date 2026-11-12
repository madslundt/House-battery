# Configuration and commissioning

## Prerequisites

Choose FOSSiBOT for a direct local TCP connection, or EcoFlow for EF BLE
entities. FOSSiBOT needs a stable local IP address, normally on port `8080`;
the setup flow checks telemetry before creating the entry. EcoFlow needs the
[EF BLE integration](https://github.com/rabits/ha-ef-ble) configured first.
The optimizer project is maintained at
[github.com/madslundt/House-battery](https://github.com/madslundt/House-battery).

You also need an electricity-price integration that exposes dated **known**
rows. A current-price-only entity is insufficient because the planner compares
future intervals. External forecast entities are optional and never replace
those known rows.

## EcoFlow entity bindings

Use EF BLE's percent Battery Level sensor, W based AC input/output power
sensors, Battery Discharge Limit and Battery Charge Limit number entities, and
the AC Port switch. The two SOC limit numbers and the grid-isolation switch and
AC Port switch must be distinct entities. Select the optional AC Charging Speed
number when it is writable. Its W or kW native minimum, maximum, and step bound
the editable **Maximum AC charge power** setting; if it is unavailable or does
not expose valid native limits, that setting becomes unavailable and charging
is disabled. Enter installed usable capacity in kWh and the rated AC output in
W during setup. EF BLE does not provide a writable AC output wattage limit, so
the output rating constrains planning only.

Entries created by earlier versions from generic Home Assistant entities can
be migrated in **Configure**: choose EcoFlow, then bind its EF BLE AC port,
charge and discharge limit controls, and the separate grid isolation switch.
The commissioning check remains off until wiring and control behavior have
been verified.

## Household and tariff inputs

| Config-flow field | Expected entity | Notes |
| --- | --- | --- |
| Battery-served local load | Local telemetry | Direct-local entries automatically use the complete per-storage off-grid total. Smart-load and backup-load readings remain diagnostics. An incomplete stack total fails closed. Legacy entries bind a load sensor. |
| Grid import power | Legacy entries: required `sensor`; direct-local entries: optional `sensor` (single signed reading: import positive, export negative, or separate import/export entities) | Numeric watts. A direct-local CT meter enables export detection. Pair it with the whole-house load sensor below for balanced-flow accounting and realized-savings estimates. Battery throughput and tariff valuation use battery telemetry without either binding. |
| Whole-house load power for accounting | Direct-local entries: optional `sensor` | Numeric watts. Bind this together with the optional CT meter to enable a balanced household savings estimate. It is separate from the battery-served local load used for planning and control. |
| Efficiency measurement grid power | Optional `sensor` in W | True active power, preferably the Watts Live phase supplying the battery. Used with connected load, SOC, and observed activity for matched-SOC efficiency learning. This is independent of the grid-import sensor used for export safety. |
| Grid available / on-grid state | `binary_sensor` or `sensor` | Required physical availability signal; see below. |
| Known electricity-price entities | one or more `sensor` entities | Must contain dated published/known price rows. |
| External price forecast entities | optional ordered `sensor` list | Same row format; each is scored independently; the first usable source that can safely extend the horizon is used for planning. |
| Grid input smart plug | FOSSiBOT: optional `switch`; EcoFlow: required `switch` | It isolates battery output from the household before EcoFlow Battery mode and is restored in Grid, Charge, and Safe modes. |

Fault state and online state are optional.
Battery SOC, local operating-mode telemetry, and native minimum/maximum SOC
readbacks come from the direct connection. The native SOC values are read-only
diagnostics; House Battery still writes validated limits through its local
adapter. Direct-local battery charge/output power comes from battery telemetry.
Legacy entries with external battery providers derive battery flow from load,
grid, and SOC balance. Direct-local entries use measured battery charge/output
telemetry for throughput and tariff valuation; an optional whole-house CT meter
and whole-house load sensor together add the balance needed for realized-savings
accounting.

## Measured efficiency for planning

In the integration's **Configure** flow, select **Efficiency measurement grid
power**. Prefer the Watts Live phase supplying the battery over the whole-house
total: appliances on other phases then cannot contaminate charging measurements.
The source must measure true active watts. Do not select a plug whose apparent
power is reported as W, and do not apply a fixed watt offset to it.

House Battery subtracts connected load and a bracketed idle baseline from that
meter to estimate charging input. It integrates connected load during verified
discharge and compares the energies over substantial, non-overlapping periods
that return to the same SOC. Direct-local discharge must be corroborated by the
grid-input relay being off, or by an independent balanced flow proving the full
load is battery supplied. Reported charge power only corroborates a session;
reported battery output never supplies the delivered-energy numerator.

Three consistent qualified periods are required before the planner automatically
uses the measured round-trip efficiency. Until then it uses **Fallback round-trip
efficiency**. **Measured round-trip efficiency** exposes the candidate estimate,
quality reason, accepted periods, selected source and effective planning value.
Missing optional measurement data pauses learning, not otherwise healthy control.
Gaps, outages, exports, unstable baselines and inconsistent periods are rejected.

Recent accepted evidence survives restarts; an unfinished measurement does not
integrate across the offline gap. Changing the meter, battery/load bindings or
nominal capacity clears incompatible evidence. Without this optional binding,
the original recent battery-telemetry ratio remains diagnostic only, and existing
legacy balanced-flow learning is retained.

## Grid availability is not grid use

The **Grid available** input answers “is an on-grid supply physically
available?” It is not a measure of import power and it is not the optimizer's
selected mode. Acceptable normalized states are:

| Meaning | Accepted raw state values |
| --- | --- |
| Available | `1`, `on`, `available`, `grid`, `on_grid`, `connected`, `true` |
| Unavailable | `0`, `off`, `no_grid`, `disconnected`, `false` |

Any other state is unsafe and makes the optimizer degraded. Use a direct device
or inverter on-grid/AC-input status when available. If you need a template,
derive it from such a status—not from `grid_import_power > 0`.
Home Assistant's literal `unknown` and `unavailable` states mean grid status
cannot be determined; neither permits selecting the lower emergency floor.

## Price input and external forecasts

The integration reads common list attributes named `prices`, `raw_today`,
`raw_tomorrow`, `today`, or `tomorrow`. Each row needs:

- a timestamp field named `start`, `hour`, `time`, or `Time`;
- an optional `end`; and
- a numeric `price`, `value`, or `Price`.

Rows must use timezone-aware timestamps. Explicit `end` times are retained.
When `end` is omitted, the source cadence is inferred from adjacent `start`
times (using the median gap for a final row); a single isolated row defaults to
one hour. This lets hourly, quarter-hourly, and other regular sources keep
their own duration. The optimizer uses only a contiguous future horizon and
never invents missing price data.

The optional external forecast list uses exactly this format. It is disabled by
default through **Use price forecasts**. Sources may overlap: each
source retains its first prediction for an interval and is compared later to
the same known actual price. When enabled, the first configured usable source
that provides a contiguous extension is used for planning; forecasts are never
blended. A forecast can add only a contiguous sequence starting at the end of
the known horizon; it cannot overwrite a known price or fill a gap. It is
optional and isolated: an unavailable, malformed, empty, expired, or stale
forecast is discarded without degrading known-price planning.

Forecast prices can extend the optimizer's horizon by at most **72 hours from
the current planning time**. Longer forecast rows are ignored after that limit;
a row that crosses the boundary is clipped. Confirmed known prices are not
limited by this forecast cap. Forecast intervals retain forecast source labels
and the configured uncertainty allowance during planning.

Each entity must have reported an update within the fixed three-hour forecast
age limit. The per-source status exposes `used`, `disabled`,
`unavailable`, `stale`, `invalid`, `empty`, `expired`, or
`no_contiguous_extension`; only `used` contributes forecast intervals to a
plan.

The same switch also permits learned weekday/hour price patterns after the
known and selected external forecast horizon. No external forecast entity is
required for this fallback. History is collected while the switch is off;
four independent matching dates, sufficient coverage, recent actual prices
and genuinely issued forecast skill are required before it can contribute.
The optimizer also requires battery movement in the learned period; otherwise
the extension stays in shadow. Read `price_patterns` on **Optimizer
state** for readiness and use. The configured uncertainty is a floor; learned
prices have an additional statistical allowance of at least 0.25 DKK/kWh.
See [learned price patterns](optimization.md#learned-price-patterns).

**Adaptive planning** additionally permits qualified source-specific residual
correction of external forecasts. It remains neutral without independent dated
evidence. Calibration preserves the original uncertainty interval, leaves known
prices untouched and reports its validation state on each forecast source.

Set **Price forecast uncertainty** (DKK/kWh) to a conservative
absolute error allowance. The planner treats a forecast charge price as
forecast plus that amount and a forecast discharge price as forecast minus it.
For example, a 0.20 DKK/kWh forecast with a 0.25 allowance is evaluated as
0.45 when charging; a 2.00 forecast is evaluated as 1.75 when discharging.

## Internal SOC-dependent price adjustments

The optimizer uses fixed policy values rather than exposing four rarely used
number entities:

| Setting | Default | Effect |
| --- | ---: | --- |
| Low-SOC charging breakpoint | 20% | Below this SOC, charging receives a price premium. |
| Low-SOC charging price premium | 0.25 DKK/kWh | Maximum adjustment at 0% SOC, fading to zero at the breakpoint. |
| High-SOC discharging breakpoint | 90% | Above this SOC, discharging receives a margin discount. |
| High-SOC discharge margin discount | 0.25 DKK/kWh | Maximum adjustment at 100% SOC, fading to zero at the breakpoint. |

For a charge or discharge step that crosses a breakpoint, the planner averages
the adjustment over the SOC range traversed, so energy closer to the breakpoint
gets a smaller adjustment. These are economic preferences, not physical limits:
**Arbitrage reserve SOC** remains the hard discharge floor, and **Maximum charge
SOC** (or the fixed 100% opportunistic target) remains the hard charge ceiling.
The two breakpoints satisfy low-SOC breakpoint < high-SOC breakpoint. Plan cost
and savings continue to report tariff and wear costs; these adjustments affect
only which plan the optimizer selects.

## Mode mapping

The adapter uses exactly these proven local options:

| Optimizer action | Local Operating Mode option |
| --- | --- |
| `charge` | `Charge` |
| `grid` | `Idle` |
| `battery` | Native `Self-Gen/Zero Export`, or a load-capped `Discharge` slot on direct-local FBP1200 entries |
| `safe` | Native safe mode, or `Idle` on direct-local FBP1200 entries |

**Operation mode** is House Battery's temporary override selector (`auto`,
`charge`, `battery`, or `grid`), not the battery's reported native mode. The
local adapter's mode and SOC readbacks are diagnostics; the vendor app can
change the device separately. Direct-local
discharge fails closed to 0 W without a complete connected-load reading and is
capped below the current load with a 50 W margin. The physical
confirmation of what actually happened is the **grid meter** (zero export) and
the derived **Battery output power** / **Power source**, which are reconciled
against the load and grid rather than taken from the inverter's raw registers.

When a grid input smart plug is configured, automatic and manual Battery mode
first command the FBP1200 into battery mode, then turn the plug off and wait for
`off` state confirmation. If it is unavailable or does not confirm `off`, the
integration immediately commands Grid/Idle and keeps operating. When leaving
Battery mode, it retries turning the plug on up to three times and checks for
`on` after each attempt before issuing Charge or Grid/Idle. If `on` is not
confirmed, it commands Grid/Idle to stop battery output and reports the failed
handoff. Automatic control stays armed and retries after the plug state is
confirmed. Without a configured plug, behavior is unchanged.

## Commissioning checklist

New entries remain in `SHADOW` and cannot turn on **Automatic control** until
you mark the integration commissioned in its Options. Commission only after:

1. Confirming the direct battery entities and the selected household inputs are
   current and represent the actual battery/load path.
2. Verifying that the grid-available entity changes accurately during an
   on-grid/off-grid test or approved simulation.
3. Reviewing the mode mapping below. After commissioning, verify each action's
   local readback and measured power response during supervised operation;
   forced **Operation mode** selections require automatic control to be on.
4. Checking the native minimum/maximum SOC controls and the resulting device
   behavior. In particular, understand what the battery does at its minimum
   SOC when the grid is absent.
5. Confirming charging, electrical installation, and tariff behavior
   comply with your local requirements.
6. Setting capacity, power, reserve, profit, and degradation values to
   conservative values for your battery.

After commissioning, first leave **Automatic control** off and inspect the
shadow plan for a few complete price horizons. When ready, enable it during a
supervised check of the mode mapping and SOC bounds.

## SOC settings

The SOC number entities must satisfy:

```text
absolute emergency SOC ≤ arbitrage reserve SOC
  < maximum charge SOC ≤ 100%
```

- **Absolute emergency SOC**: lower SOC value selected only for a confirmed
  physical grid outage. Safe/outage commands write it to the native minimum
  and the custom slot. Register read-back does not prove that the separate
  off-grid AC output obeys this floor; verify that behavior during commissioning.
- **Arbitrage reserve SOC**: normal planning and commanded discharge floor.
  Measured SOC at or below this value overrides held battery slots and manual
  battery overrides, requests mains reconnection, and commands Grid/Idle.
  Faults, missing prices, and unknown grid availability preserve this reserve;
  they do not release the emergency buffer.
- Direct-local mode commands disable all fifteen unused schedule slots and
  require read-back of the complete sixteen-slot bank. An incomplete read-back
  is a failure, not permission to replay an ambiguous write. This prevents a
  leftover vendor schedule from silently retaining a different SOC floor.
- **Maximum charge SOC**: normal planning and hardware ceiling.
- **Opportunistic full charge**: the opt-in policy always targets 100%. It is
  used only when **Allow opportunistic full charge** is enabled and a complete
  additional known-price cycle improves plan savings.

Changing a number updates the persistent plan setting and triggers replanning.
It does not make a previously unsafe device configuration safe; that must be
verified at the physical device.

## System states

| State | Meaning | Control behavior |
| --- | --- | --- |
| `BOOTSTRAP` | Awaiting usable telemetry/price horizon. | No economic command. |
| `SHADOW` | Plan is valid but commissioning or automatic control is off. | Plan only; no writes. |
| `ACTIVE` | Commissioned and automatic control is on. | Writes local limits/mode with read-back. |
| `RECOVERING` | The direct local TCP connection has been unavailable for less than two minutes. | Pauses inverter writes, restores the configured mains-input plug, and retains the automatic-control switch. A fresh, valid local frame resumes `ACTIVE`; two minutes of loss becomes `DEGRADED`. |
| `DEGRADED` | A required input is stale, invalid, faulted, offline, or prices cannot yield a plan. | Requests safe mode and retries on each refresh. Automatic control stays armed and resumes after health recovers. |
| `OUTAGE` | Physical grid signal explicitly reports no mains. | Clears the plan and requests safe mode with the absolute emergency floor. Unknown grid availability is a fault and preserves the arbitrage reserve. |

`Force safe mode` immediately turns off automatic control and requests the
adapter's safe local mode. System errors pause writes without changing the
switch; three consecutive local TCP or battery-command failures request an
integration reload, with a 15-minute cooldown. Most health gates resume
automatically when inputs recover. The export safety latch requires an
operator off/on reset after export stops. The **Optimizer problem** binary
sensor shows active health blockers.
