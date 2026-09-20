# Examples

Replace every example entity ID with the entity ID created for your own entry.
The integration derives entity IDs from the entry name, so use Home Assistant's
entity picker rather than assuming these example names are exact.

## Physical grid-availability template

Only use this when `sensor.fbp1200_ac_input_state` is a device-provided physical
AC-input/on-grid status. It must not be grid-import watts.

```yaml
template:
  - binary_sensor:
      - name: FBP1200 grid available
        unique_id: fbp1200_grid_available
        device_class: power
        state: >-
          {{ states('sensor.fbp1200_ac_input_state') | lower
             in ['on', 'on_grid', 'connected', 'available'] }}
```

Bind `binary_sensor.fbp1200_grid_available` to **Grid available / on-grid
state** in the config flow. If the device exposes a native binary sensor,
prefer that directly.

## Conservative starting policy

For a 1.958 kWh FBP1200 with a 10% emergency buffer and a 20% economic reserve,
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
| Minimum mode duration | `30 min` |
| Maximum daily mode transitions | `4` |

These are examples, not manufacturer recommendations. Increase the emergency
and arbitrage reserves if backup availability matters more than price savings.

## Dashboard cards

An entities card is a good first dashboard because it makes safety state and
decision reason visible together:

```yaml
type: entities
title: FBP1200 optimizer
entities:
  - entity: sensor.fbp1200_optimizer_state
  - entity: binary_sensor.fbp1200_optimizer_problem
  - entity: binary_sensor.fbp1200_grid_available
  - entity: sensor.fbp1200_current_decision
  - entity: sensor.fbp1200_battery_mode
  - entity: sensor.fbp1200_battery_state_of_charge
  - entity: sensor.fbp1200_effective_charge_target_soc
  - entity: sensor.fbp1200_expected_plan_savings
  - entity: sensor.fbp1200_estimated_realized_savings_today
  - entity: sensor.fbp1200_extra_storage_policy
  - entity: switch.fbp1200_automatic_control
  - entity: button.fbp1200_force_safe_mode
```

Inspect the **Operation plan** entity attributes in Developer Tools → States.
Its `blocks` attribute is ready for a custom chart/card: each block contains
`start`, `end`, `action`, `expected_savings_dkk`, `energy_kwh`, `soc_start`,
`soc_end`, and `reason`.

## Add a known dishwasher or EV load

When a planned load is known, add it to the learned forecast. This service
replans immediately:

```yaml
service: fossibot_fbp1200.schedule_load
data:
  start: "2026-09-21T01:00:00+02:00"
  end: "2026-09-21T03:00:00+02:00"
  additional_w: 800
  label: Dishwasher
```

With more than one optimizer entry, include its config-entry ID:

```yaml
service: fossibot_fbp1200.schedule_load
data:
  config_entry_id: 01JABCDEF0123456789
  start: "2026-09-21T22:00:00+02:00"
  end: "2026-09-22T06:00:00+02:00"
  additional_w: 450
  label: EV standby load
```

Clear outstanding scheduled loads when they are no longer relevant:

```yaml
service: fossibot_fbp1200.clear_scheduled_loads
data: {}
```

## Export evidence for analysis

Run this from Developer Tools → Actions. Home Assistant displays the returned
JSON response, which can be saved for offline analysis:

```yaml
service: fossibot_fbp1200.export_data
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
