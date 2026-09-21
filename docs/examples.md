# Examples

Replace every example entity ID with the entity ID created for your own entry.
The integration derives entity IDs from the entry name, so use Home Assistant's
entity picker rather than assuming these example names are exact.

## Physical grid-availability template

Only use this when `sensor.house_battery_ac_input_state` is a device-provided physical
AC-input/on-grid status. It must not be grid-import watts.

```yaml
template:
  - binary_sensor:
      - name: House Battery grid available
        unique_id: house_battery_grid_available
        device_class: power
        state: >-
          {{ states('sensor.house_battery_ac_input_state') | lower
             in ['on', 'on_grid', 'connected', 'available'] }}
```

Bind `binary_sensor.house_battery_grid_available` to **Grid available / on-grid
state** in the config flow. If the device exposes a native binary sensor,
prefer that directly.

## Conservative starting policy

For a 1.958 kWh home battery with a 10% emergency buffer and a 20% economic reserve,
set the number entities as follows before commissioning:

| Number entity | Suggested starting value |
| --- | ---: |
| Nominal battery capacity | `1.958 kWh` |
| Absolute emergency SOC | `10%` |
| Arbitrage reserve SOC | `20%` |
| Maximum charge SOC | `90%` |
| Extra-storage charge SOC | `100%` |
| Fallback round-trip efficiency | `85%` |
| Battery degradation cost | `0.35 DKK/kWh` |
| Minimum required profit | `0.75 DKK/kWh` |
| Extra-storage price spread | `2.00 DKK/kWh` |
| Extra-storage cheap-window maximum duration | `30 min` |
| Minimum mode duration | `30 min` |
| Maximum daily mode transitions | `4` |

These are examples, not manufacturer recommendations. Increase the emergency
and arbitrage reserves if backup availability matters more than price savings.

## Dashboard cards

An entities card is a good first dashboard because it makes safety state and
decision reason visible together:

```yaml
type: entities
title: House Battery optimizer
entities:
  - entity: sensor.house_battery_optimizer_state
  - entity: binary_sensor.house_battery_optimizer_problem
  - entity: binary_sensor.house_battery_grid_available
  - entity: sensor.house_battery_current_decision
  - entity: sensor.house_battery_battery_mode
  - entity: sensor.house_battery_battery_state_of_charge
  - entity: sensor.house_battery_effective_charge_target_soc
  - entity: sensor.house_battery_expected_plan_savings
  - entity: sensor.house_battery_estimated_realized_savings_today
  - entity: sensor.house_battery_extra_storage_policy
  - entity: switch.house_battery_automatic_control
  - entity: button.house_battery_force_safe_mode
```

Inspect the **Operation plan** entity attributes in Developer Tools → States.
Its `blocks` attribute is ready for a custom chart/card: each block contains
`start`, `end`, `action`, `expected_savings_dkk`, `energy_kwh`, `soc_start`,
`soc_end`, and `reason`.

## Add a known dishwasher or EV load

When a planned load is known, add it to the learned forecast. This service
replans immediately:

```yaml
service: house_battery.schedule_load
data:
  start: "2026-09-21T01:00:00+02:00"
  end: "2026-09-21T03:00:00+02:00"
  additional_w: 800
  label: Dishwasher
```

With more than one optimizer entry, include its config-entry ID:

```yaml
service: house_battery.schedule_load
data:
  config_entry_id: 01JABCDEF0123456789
  start: "2026-09-21T22:00:00+02:00"
  end: "2026-09-22T06:00:00+02:00"
  additional_w: 450
  label: EV standby load
```

Clear outstanding scheduled loads when they are no longer relevant:

```yaml
service: house_battery.clear_scheduled_loads
data: {}
```

## Export evidence for analysis

Run this from Developer Tools → Actions. Home Assistant displays the returned
JSON response, which can be saved for offline analysis:

```yaml
service: house_battery.export_data
data: {}
```

The response includes the decision history, ledger, plan, settings, learned
load profile, battery capacity/efficiency estimates, and scheduled loads. Do
not export sensitive Home Assistant configuration details to an external model
without reviewing the data first.

## A safe enablement sequence

1. Configure the bindings and leave the entry uncommissioned.
2. Wait for price data and valid telemetry; confirm `Optimizer state` becomes
   `SHADOW` rather than `DEGRADED`.
3. Compare the Operation plan against your tariff and expected loads for
   several complete horizons.
4. In Options, mark the entry commissioned only after locally checking all
   three Operating Mode actions and native SOC limits.
5. Turn on **Automatic control** while observing the initial decision. The
   first fresh refresh intentionally sends no write.
6. Keep the Force safe mode button accessible. Use it immediately if telemetry,
   device behavior, or electrical behavior differs from the plan.

## One realistic fluctuating-price week

Assume a 1.958 kWh battery, 20% arbitrage reserve, 90% normal target, 100%
extra-storage target, 85% round-trip efficiency, 0.35 DKK/kWh degradation
cost, and 0.75 DKK/kWh minimum profit. The connected load is typically
250–450 W overnight and 500–900 W in the evening. These examples show the
decision shape; the actual plan still uses the learned load profile and every
contiguous known interval at the cadence supplied by the price provider.

| Day | Known price pattern, DKK/kWh | Expected optimizer outcome | Why |
| --- | --- | --- | --- |
| Monday | 1.92 overnight, 2.08 midday, 2.31 evening | `grid` throughout | The 0.39 spread is below losses, degradation, and required profit. Cycling would cost more than it saves. |
| Tuesday | 0.42 at 02:00–05:00, 2.75 at 17:00–20:00 | Charge only toward 90%, then discharge to the 20% reserve in the evening | The effective margin is large enough to pay for a cycle, but the extra-storage threshold is not necessarily needed once the planned evening load is covered. |
| Wednesday | 0.68 overnight, 1.05 evening | Mostly `grid`; perhaps retain energy acquired earlier | The 0.37 spread does not justify a new charge/discharge cycle. |
| Thursday | 0.18 from 01:00–01:30, 4.35 from 17:00–21:00 | Enable extra storage up to 100%, then discharge only against forecast load | The 30-minute lowest known-price window satisfies the default duration limit; its raw spread also exceeds the configured 2.00 DKK/kWh threshold and remains profitable after efficiency and wear. The extra target permits, but does not force, more charging. |
| Friday | 1.45 most of the day, 2.10 evening | `grid` or hold reserve | Avoids chattering for a marginal 0.65 spread. |
| Saturday | -0.05 for two hours, 1.80 later | Charge if SOC headroom and known future load justify it; otherwise hold | Negative/very low input price can be attractive, but the planner still respects the target, minimum mode duration, and transition budget. |
| Sunday | 0.55 overnight, 3.20 evening, large scheduled dishwasher load at 19:00 | Charge ahead of the high-price window and reserve energy for the scheduled load | `house_battery.schedule_load` adds the dishwasher demand to the forecast, making the decision explainable rather than accidental. |

For Thursday, the visible **Extra storage policy** attributes should show
`active`, the effective target as `100`, the known spread near `4.17`, and a
positive effective margin. For Monday and Friday it should remain `normal`.
The **Operation plan** blocks show when the plan chose `charge`, `grid`, or
`battery`, with the SOC range and expected savings for each block.
