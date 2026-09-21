# Configuration and commissioning

## Prerequisites

Before adding this integration, give the battery a stable local IP address.
House Battery connects to its local TCP interface directly: enter the IP,
port (normally `8080`), and device name, then complete the read-only telemetry
check. It creates battery telemetry, native SOC-limit, and operating-mode
entities itself. The optimizer project is maintained at
[github.com/madslundt/House-battery](https://github.com/madslundt/House-battery).

You also need an electricity-price integration that exposes dated **known**
rows. A current-price-only entity is insufficient because the planner compares
future intervals. External forecast entities are optional and never replace
those known rows.

## Household and tariff inputs

| Config-flow field | Expected entity | Notes |
| --- | --- | --- |
| Connected/house load power | `sensor` | Watts for load served by this battery. Do not use whole-house demand if the battery cannot serve all of it. Direct-local entries expose separate local load diagnostics before choosing a battery-served source. |
| Grid import power | `sensor` | Numeric watts. Used for accounting/telemetry. |
| Grid available / on-grid state | `binary_sensor` or `sensor` | Required physical availability signal; see below. |
| Known electricity-price entities | one or more `sensor` entities | Must contain dated published/known price rows. |
| External price forecast entities | optional ordered `sensor` list | Same row format; each is scored independently; the first usable source that can safely extend the horizon is used for planning. |

Grid export power, PV power, fault state, and online state are optional.
Battery SOC, charge/discharge power, the **Operating mode** selector, and the
native minimum/maximum SOC controls come from the direct connection. Native
SOC limits are read back after every automatic limit write and checked again
before control can be enabled.

## Grid availability is not grid use

The **Grid available** input answers “is an on-grid supply physically
available?” It is not a measure of import power and it is not the optimizer's
selected mode. Acceptable normalized states are:

| Meaning | Accepted raw state values |
| --- | --- |
| Available | `1`, `on`, `available`, `grid`, `on_grid`, `connected`, `true` |
| Unavailable | `0`, `off`, `unavailable`, `no_grid`, `disconnected`, `false` |

Any other state is unsafe and makes the optimizer degraded. Use a direct device
or inverter on-grid/AC-input status when available. If you need a template,
derive it from such a status—not from `grid_import_power > 0`.

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
default through **Use external price forecast**. Sources may overlap: each
source retains its first prediction for an interval and is compared later to
the same known actual price. When enabled, the first configured usable source
that provides a contiguous extension is used for planning; forecasts are never
blended. A forecast can add only a contiguous sequence starting at the end of
the known horizon; it cannot overwrite a known price or fill a gap. It is
optional and isolated: an unavailable, malformed, empty, expired, or stale
forecast is discarded without degrading known-price planning.

Each entity must have reported an update within **External price forecast maximum
age** (180 minutes by default). Use a value that matches the forecast source's
normal update cadence. The per-source status exposes `used`, `disabled`,
`unavailable`, `stale`, `invalid`, `empty`, `expired`, or
`no_contiguous_extension`; only `used` contributes forecast intervals to a
plan.

Set **External price forecast uncertainty** (DKK/kWh) to a conservative
absolute error allowance. The planner treats a forecast charge price as
forecast plus that amount and a forecast discharge price as forecast minus it.
For example, a 0.20 DKK/kWh forecast with a 0.25 allowance is evaluated as
0.45 when charging; a 2.00 forecast is evaluated as 1.75 when discharging.

## Mode mapping

The adapter uses exactly these proven local options:

| Optimizer action | Local Operating Mode option |
| --- | --- |
| `charge` | `Charge` |
| `grid` | `Idle` |
| `battery` | `Self-Gen/Zero Export` |
| `safe` | `Self-Gen/Zero Export` |

**Operating mode** shows the last command acknowledged by the battery's local
interface. The vendor app can change the mode separately, so measured
charge/discharge power remains the physical confirmation of actual behaviour.

## Commissioning checklist

New entries remain in `SHADOW` and cannot turn on **Automatic control** until
you mark the integration commissioned in its Options. Commission only after:

1. Confirming the direct battery entities and the selected household inputs are
   current and represent the actual battery/load path.
2. Verifying that the grid-available entity changes accurately during an
   on-grid/off-grid test or approved simulation.
3. Manually checking each mode through **Operating mode** and confirming the
   local command and measured power response.
4. Checking the native minimum/maximum SOC controls and the resulting device
   behavior. In particular, understand what the battery does at its minimum
   SOC when the grid is absent.
5. Confirming export, charging, electrical installation, and tariff behavior
   comply with your local requirements.
6. Setting capacity, power, reserve, profit, and degradation values to
   conservative values for your battery.

After commissioning, first leave **Automatic control** off and inspect the
shadow plan for a few complete price horizons. Turn it on only when the plan,
mode mapping, and SOC bounds are consistently correct.

## SOC settings

The following number entities must satisfy:

```text
absolute emergency SOC ≤ arbitrage reserve SOC
  < maximum charge SOC ≤ extra-storage charge SOC
```

- **Absolute emergency SOC**: device-level lower SOC value written through the
  required native minimum-SOC control during automatic commands. It is below
  the normal economic reserve, preserving a last-resort buffer.
- **Arbitrage reserve SOC**: planning floor. The optimizer never schedules a
  battery discharge below this value.
- **Maximum charge SOC**: normal planning and hardware ceiling.
- **Extra-storage charge SOC**: a temporary higher ceiling that may be used
  only when the extra-storage policy is economically justified.

Changing a number updates the persistent plan setting and triggers replanning.
It does not make a previously unsafe device configuration safe; that must be
verified at the physical device.

## System states

| State | Meaning | Control behavior |
| --- | --- | --- |
| `BOOTSTRAP` | Awaiting usable telemetry/price horizon. | No economic command. |
| `SHADOW` | Plan is valid but commissioning or automatic control is off. | Plan only; no writes. |
| `ACTIVE` | Commissioned and automatic control is on. | Writes local limits/mode with read-back. |
| `DEGRADED` | A required input is stale, invalid, faulted, offline, or prices cannot yield a plan. | Requests safe mode and latches automatic control off. |
| `OUTAGE` | Physical grid signal is unavailable. | Clears plan, requests safe mode and latches automatic control off. |

`Force safe mode` immediately turns off automatic control and requests the
adapter's safe local mode. The **Optimizer problem** binary sensor shows active
health blockers.
