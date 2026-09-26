# Commissioning: verifying the never-export invariant

The single hard requirement for this integration is that the battery must never
export to the grid. This is enforced in every layer (see `docs/architecture.md`)
and must be verified physically after install, not only trusted from the code.

## The three protection layers

1. **Discharge is bounded at the actuator.** Native integrations use their
   zero-export mode. A direct-local FBP1200 has no CT input, so its native
   Self-Gen mode remains idle; that path writes a manual Discharge slot capped
   at the fresh connected-load reading minus 50 W. Missing or invalid load
   telemetry produces an Idle/0 W command.
2. **A configured grid meter independently verifies the result.** The coordinator
   decomposes the signed grid-meter reading into import and export. Any export
   above a small noise floor lights an `Export detected` sensor; export above a
   slightly higher safety floor while automatic control is enabled **latches an
   `Export safety fault` that disables all inverter writes** until you reset
   execution. This is fail-closed: an export can never continue through the next
   planning cycle.
3. **Accounting + telemetry report export as a first-class quantity.** Any grid
   export shows up on the **Grid export power** sensor and in the ledger, so a
   violation is visible rather than clamped away silently.

The planner also caps modeled discharge at expected load. The direct-local
actuator's second, real-time cap is deliberately independent of that forecast.

## Before energizing automatic control

- [ ] **Grid is confirmed available** and the `Grid available` entity reports
      true. The planner refuses to plan, and automatic control latches to Safe,
      when grid is unavailable.
- [ ] **The grid meter is configured** (`Grid import power` / `Grid export
      power`, or a single signed grid entity) for native integrations. A
      direct-local FBP1200 has no trustworthy CT value, so verify it with an
      external meter and understand that HA cannot latch on detected export.
- [ ] **Native SOC controls read back** (minimum / maximum) with valid, sane
      bounds. Automatic control stays off until they are commissioned.
- [ ] **`Absolute emergency SOC` ≤ `Reserve SOC` ≤ `Opportunistic target SOC`**
      and all sit inside the native bounds. The reserve is raised to the
      inverter's native minimum during discharge, so a failed mode command cannot
      expose stored reserve.
- [ ] **Connected load power** is configured (the `Load power` entity). The
      derived battery flow and the `Power source` sensor depend on it.
- [ ] **Commission** is toggled on *after* the above are satisfied.

## Physically verifying zero export

Do this while someone can watch the inverter's grid meter or a plug meter.

1. Confirm the battery action is **Self-Gen / Zero Export** for a native
   integration or **Discharge** for a direct-local FBP1200.
2. Put a *small* constant load on the circuit the battery serves (a lamp or a
   space heater on a metered socket). This is the condition that used to export
   under the old load-clamped approach.
3. Watch the grid meter: it must not run backwards. Simultaneously confirm:
   - **Grid export power** sensor reads `0` (or non-positive), never a positive
     number;
   - **Export detected** binary sensor stays `off`;
   - **Power source** reads `battery` (the battery is serving the load); and
   - **Battery output power** is close to the load (within converter loss),
     confirming the derived flow agrees with the meter.
4. Now **raise the load above the battery's discharge power**. Grid import rises
   by the shortfall.
5. **Reduce the load again to near zero.** The grid meter must not run
   backwards. `Export detected` stays `off` throughout. Direct-local entries
   without a grid meter must be checked with an external meter during initial
   commissioning because the integration cannot observe export itself.

If any step shows positive grid export power, stop: the grid meter is mis-wired
or mis-signed, or the load measurement is wrong, and the derived flow cannot be
trusted.

## What to check if export ever appears

- **Meter wiring / sign.** The grid meter must count import as positive. The
  integration decomposes the signed flow so import > 0 and export < 0; a
  reversed CT makes this backwards. Fix the CT and clear the latch.
- **Grid meter configured and fresh.** If the optimizer cannot see grid power,
  it cannot derive the flow or verify zero export, so automatic control stays
  off. A missing load entity has the same effect on the derived flow.
- **The latch is what stops a repeat.** `Export safety fault` disables all
  inverter writes until you reset execution, so even a firmware bug cannot push
  energy to the grid on the next refresh.

## Periodic self-check

The dashboard exposes everything needed for a recurring verification:
**Grid export power**, **Export detected**, **Export safety fault**,
**Power source**, and the decision history. Export power should read 0 (or less)
in normal operation; any sustained positive reading is an incident to
investigate using the checklist above. Clear `Export safety fault` by disabling
then re-enabling automatic control once the export has stopped.
