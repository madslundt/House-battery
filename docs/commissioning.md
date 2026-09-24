# Commissioning: verifying the never-export invariant

The single hard requirement for this integration is that the battery must never
export to the grid. This is enforced in every layer (see `docs/architecture.md`)
and must be verified physically after install, not only trusted from the code.

## The three layers that guarantee zero export

1. **Planner caps at the forecast load.** The optimizer never commands a
   discharge that the predicted house load can absorb.
2. **Execution caps at the *measured* load, on every refresh.** When the local
   adapter writes the fixed-power discharge slot, it clamps the setpoint to
   `measured_load − safety_margin` (never negative), so a load dip below the
   setpoint cannot push energy back out. This is the layer that actually stops
   export, because the planner's forecast is not the instantaneous load.
3. **Accounting + telemetry report export as a first-class quantity.** Any grid
   export shows up on the **Grid export power** sensor and in the ledger, so a
   violation is visible rather than clamped away silently.

Discharge economics are a secondary concern: stored energy is valued by its
future opportunity cost, and discharge only happens when it is genuinely better
than holding the energy. None of that can create export — that is guaranteed by
layer 2.

## Before energizing automatic control

- [ ] **Grid is confirmed available** and the `Grid available` entity reports
      true. The planner refuses to plan, and automatic control latches to Safe,
      when grid is unavailable.
- [ ] **Native SOC controls read back** (minimum / maximum) with valid, sane
      bounds. Automatic control stays off until they are commissioned.
- [ ] **`Absolute emergency SOC` ≤ `Reserve SOC` ≤ `Opportunistic target SOC`**
      and all sit inside the native bounds. The reserve is raised to the
      inverter's native minimum in Self-Gen, so a failed mode command cannot
      expose stored reserve.
- [ ] **Discharge power** is set to the value the adapter is expected to command
      (clamped to 1200 W at the command boundary).
- [ ] **Commission** is toggled on *after* the above are satisfied.

## Physically verifying zero export

Do this while someone can watch the inverter's grid meter or a plug meter.

1. Set **Operating Mode = Self-Gen / Zero Export** (the battery action).
2. Put a *small* constant load on the circuit the battery serves (a lamp or a
   space heater on a metered socket) that is **below** the configured discharge
   power. This is the exact condition that used to export.
3. Watch the grid meter: it must not run backwards. Simultaneously confirm:
   - **Grid export power** sensor reads `0` (or non-positive), never a positive
     number;
   - **Battery discharge power** sensor reads **at or below** `measured_load −
     safety_margin`, never the full configured discharge power when the load is
     small;
   - **Current action** sensor reports Self-Gen and the plan reason mentions
     battery discharge.
4. Now **raise the load above the discharge power**. The battery discharges at
   its full setpoint and the grid import rises by the discharge power — still
   no export.
5. **Reduce the load again to near zero.** The discharge setpoint must fall with
   it (down toward zero), not hold at the configured power. This confirms the
   execution clamp is tracking the *measured* load, not a fixed slot.

If step 3 shows any positive grid export power, stop: the local load
measurement is wrong or missing, and the clamp cannot act without it.

## What to check if export ever appears

- **Load measurement.** `Load power` / the local-load diagnostics must report a
  sensible positive number while the load is on. If the adapter cannot read
  battery-served load, the clamp is inert and export is possible.
- **Meter wiring / sign.** The grid meter must count import as positive. The
  integration normalizes the signed flow so import > 0 and export < 0; a
  reversed CT makes this backwards.
- **SOC at/above reserve.** Discharge is bounded by energy above the reserve; a
  misconfigured reserve changes how much the battery is allowed to give.
- **Mode lock / transitions.** A rapid mode change should not leave a fixed
  discharge slot written while the load has since dropped. The execution clamp
  re-clamps on the next refresh regardless.

## Periodic self-check

The dashboard exposes everything needed for a recurring verification:
**Grid export power**, **Battery discharge power**, **Current action**, and the
decision history. Export power should read 0 (or less) in normal operation; any
sustained positive reading is an incident to investigate using the checklist
above.
