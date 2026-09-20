# Configuration and commissioning

## Prerequisites

Before adding this integration, expose the FBP1200 through a **local** Home
Assistant integration/control path that provides a verified Operating Mode
`select` entity and the relevant telemetry. The implementation is designed to
layer on that local adapter; it does not open a new TCP connection itself.

You also need an electricity-price integration that exposes dated forecast
rows. A current-price-only entity is insufficient for optimization because the
planner must compare future intervals.

## Required bindings

| Config-flow field | Expected entity | Notes |
| --- | --- | --- |
| Battery state of charge | `sensor` | Numeric percentage from 0 to 100. |
| Connected/house load power | `sensor` | Watts for load served by this battery. Do not use whole-house demand if the FBP1200 cannot serve all of it. |
| Grid import power | `sensor` | Numeric watts. Used for accounting/telemetry. |
| Grid available / on-grid state | `binary_sensor` or `sensor` | Required physical availability signal; see below. |
| AFERIY Operating Mode | `select` | Must offer the verified local options `Charge`, `Idle`, and `Self-Gen/Zero Export`. |
| Electricity price forecast entities | one or more `sensor` entities | Must contain dated price rows. |
| Battery charge power | `sensor` | Measured W; required for evidence and learning. |
| Battery discharge power | `sensor` | Measured W; required for evidence and learning. |

Optional bindings are grid export power, PV power, fault state, online state,
charge-power control, discharge-power control, native minimum-SOC control, and
native maximum-SOC control. Bind all available native controls so device limits
are synchronized whenever automatic control sends an action.

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

## Price input

The integration reads common list attributes named `prices`, `raw_today`,
`raw_tomorrow`, `today`, or `tomorrow`. Each row needs:

- a timestamp field named `start`, `hour`, `time`, or `Time`;
- an optional `end` (otherwise one hour is assumed); and
- a numeric `price`, `value`, or `Price`.

Rows must use timezone-aware timestamps. Valid rows are normalized into
15-minute slots. The optimizer uses only a contiguous future horizon and never
invent missing price data.

## Mode mapping

The adapter uses exactly these proven local options:

| Optimizer action | Local Operating Mode option |
| --- | --- |
| `charge` | `Charge` |
| `grid` | `Idle` |
| `battery` | `Self-Gen/Zero Export` |
| `safe` | `Self-Gen/Zero Export` |

The configured Operating Mode is read back immediately after a command. The
measured charge/discharge-power entities are the physical confirmation of what
the battery is actually doing.

## Commissioning checklist

New entries remain in `SHADOW` and cannot turn on **Automatic control** until
you mark the integration commissioned in its Options. Commission only after:

1. Confirming all required entities are current and represent the actual
   battery/load path.
2. Verifying that the grid-available entity changes accurately during an
   on-grid/off-grid test or approved simulation.
3. Manually checking each mode through the local FBP1200 control and confirming
   the Operating Mode read-back and measured power response.
4. Checking the native minimum/maximum SOC controls and the resulting device
   behavior. In particular, understand what the FBP1200 does at its minimum
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
  optional native minimum-SOC control during automatic commands. It is below
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
