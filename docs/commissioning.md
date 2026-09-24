# Commissioning: verifying the never-export invariant

The single hard requirement for this integration is that the battery must never
export to the grid. This is enforced in every layer (see `docs/architecture.md`)
and must be verified physically after install, not only trusted from the code.

## The three layers that guarantee zero export

1. **Self-Gen / Zero Export is the only battery action.** When automatic control
   is on and the plan wants to move the battery, the actuator writes the
   inverter's native `Self-Gen/Zero Export` mode. That mode is a hardware
   guarantee that no energy reaches the grid; it is not a load-dependent
   setpoint that can drift above the load and export. This is the layer that
   actually stops export.
2. **The grid meter independently verifies the guarantee.** The coordinator
   decomposes the signed grid-meter reading into import and export. Any export
   above a small noise floor lights an `Export detected` sensor; export above a
   slightly higher safety floor while automatic control is enabled **latches an
   `Export safety fault` that disables all inverter writes** until you reset
   execution. This is fail-closed: an export can never continue through the next
   planning cycle.
3. **Accounting + telemetry report export as a first-class quantity.** Any grid
   export shows up on the **Grid export power** sensor and in the ledger, so a
   violation is visible rather than clamped away silently.

Because zero export is a firmware guarantee, the planner no longer has to reserve
a load-dip margin to avoid exporting and can value stored energy on its genuine
opportunity cost. Discharge economics are a secondary concern; none of it can
create export on its own — that is guaranteed by layers 1 and 2.

## Before energizing automatic control

- [ ] **Grid is confirmed available** and the `Grid available` entity reports
      true. The planner refuses to plan, and automatic control latches to Safe,
      when grid is unavailable.
- [ ] **The grid meter is configured** (`Grid import power` / `Grid export
      power`, or a single signed grid entity). Without a grid meter the derived
      power-flow model is incomplete and automatic control cannot safely run.
- [ ] **Native SOC controls read back** (minimum / maximum) with valid, sane
      bounds. Automatic control stays off until they are commissioned.
- [ ] **`Absolute emergency SOC` ≤ `Reserve SOC` ≤ `Opportunistic target SOC`**
      and all sit inside the native bounds. The reserve is raised to the
      inverter's native minimum in Self-Gen, so a failed mode command cannot
      expose stored reserve.
- [ ] **Connected load power** is configured (the `Load power` entity). The
      derived battery flow and the `Power source` sensor depend on it.
- [ ] **Commission** is toggled on *after* the above are satisfied.

## Physically verifying zero export

Do this while someone can watch the inverter's grid meter or a plug meter.

1. Confirm the battery action is **Self-Gen / Zero Export**.
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
   by the shortfall and there is still no export — the Self-Gen mode simply lets
   the grid make up the difference.
5. **Reduce the load again to near zero.** The grid meter must not run
   backwards: the firmware holds zero export regardless of how small the load
   becomes. `Export detected` stays `off` throughout.

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
