# Commissioning: verifying export and SOC limits

Automatic control requires physical verification of zero export and both SOC
limits. Command read-back and automated tests verify the requested settings;
they do not establish the physical behavior of the separate off-grid AC output.

Direct-local discharge is locked until independent backup-cutoff verification
has been recorded. Existing installations also default to unverified. The lock
survives restarts, commissioning changes and automatic-control switch cycles;
held plans and forced Battery mode cannot bypass it. Charge and Grid controls
remain available, but planning excludes discharge and its assumed savings.
Read-only `backup_cutoff_verified`, `battery_discharge_blocked` and
`battery_discharge_blocked_reason` attributes explain this state. There is no
UI or service that unlocks discharge in this release. Native SOC read-back
does not unlock it.

## FBP1200 physical results, 2026-10-24

These results apply to the tested unit and firmware, not every FBP1200.

| Test | Observed result |
| --- | --- |
| Normal reserve raised from 18% to 22% at reported SOC 20% | Mains input restored; reported battery output fell from 215 W to 0 W; downstream UPS remained online. Passed. |
| Emergency floor 22% at reported SOC 20%, with the sole mains input disconnected | Native minimum and active-slot minimum both read back 22%, with all other slots disabled. Battery output continued at 243 W and the UPS received 230 V. Failed. |
| Autonomous crossing from SOC 20% to emergency floor 19% | Output continued at 235–278 W for over one minute after SOC reached 19%. Failed. |

Mains and original SOC settings were restored after every test. Automatic
control remains off on the tested installation. These failures do not establish
a supported emergency cutoff command; the software lock prevents deliberate
discharge but cannot stop independent backup output during a real outage.

## The three protection layers

1. **Discharge is bounded at the actuator.** Native integrations use their
   zero-export mode. A direct-local FBP1200 has no CT input, so its native
   Self-Gen mode remains idle; that path writes a manual Discharge slot capped
   at the fresh connected-load reading minus 50 W. Missing or invalid load
   telemetry produces an Idle/0 W command.
2. **A configured grid meter independently verifies the result.** The coordinator
   decomposes the signed grid-meter reading into import and export. Any export
   above a small noise floor lights an `Export detected` sensor; export above a
   slightly higher safety floor while automatic control is enabled raises an
   `Export safety fault` that latches and pauses inverter writes. This is
   fail-closed: an export can never continue through the next planning cycle.
   Automatic control remains armed. After export stops, reset the fault with an
   explicit off/on switch cycle.
3. **Accounting + telemetry report export as a first-class quantity.** Any grid
   export shows up on the **Grid export power** sensor and in the ledger, so a
   violation is visible rather than clamped away silently.

The planner also caps modeled discharge at expected load. The direct-local
actuator's second, real-time cap is deliberately independent of that forecast.

## Before energizing automatic control

- [ ] **Grid is confirmed available** and the `Grid available` entity reports
      true. The planner refuses to plan and requests safe mode when grid is
      unavailable. Automatic control remains armed and resumes when the grid
      returns.
- [ ] **The grid meter is configured** (`Grid import power` / `Grid export
      power`, or a single signed grid entity) for native integrations. A
      direct-local FBP1200 has no trustworthy CT value, so verify it with an
      external meter and understand that HA cannot latch on detected export.
- [ ] **Native SOC controls read back** (minimum / maximum) with valid, sane
      bounds. Automatic control stays off until they are commissioned.
- [ ] **`Absolute emergency SOC` ≤ `Reserve SOC` < `Maximum charge SOC` ≤ 100%**
      and all sit inside the native bounds. Normal commands preserve the reserve;
      only confirmed outages select the emergency floor. Verify physical cutoff
      at elevated thresholds using the procedure below.
- [ ] **Connected load power** is available. Direct-local entries use the
      complete per-storage off-grid telemetry; legacy entries bind a `Load
      power` sensor. The flow model and `Power source` sensor depend on it.
- [ ] **Commission** is toggled on *after* the above are satisfied.

## Safely verifying both SOC limits

Keep Home Assistant, the control network, and recovery controls on an independent
power source. Use a small noncritical test load on the battery output, with
someone at the physical AC-output button. The FBP1200 output may latch off and
require that button to restore it. A downstream UPS gives time to recover but
does not prove automatic AC-output recovery.

1. Save the original SOC settings, automatic-control/override state, native
   controls, and all schedule slots. Record actual SOC, battery charge/output
   power, output voltage or test-load power, and UPS state. Pause economic
   control so it cannot compete with the test.
2. Use thresholds near the current SOC, well above the real low-charge range.
   Preserve emergency SOC ≤ reserve SOC < maximum SOC. Do not discharge the
   battery down to its ordinary emergency minimum for this test.
3. For the normal reserve, raise it above the observed SOC and verify that a
   requested battery override is refused in favor of mains/Grid/Idle. Confirm
   actual battery discharge stops and the test load remains powered; a mode
   label or register read-back alone is insufficient.
4. For the emergency floor, use a supervised test isolated from critical loads.
   Keep the tested native and slot floor above observed SOC and apply the
   confirmed-outage command path. Observe whether actual off-grid battery
   discharge stops. Do not spoof a shared grid-status entity or disconnect the
   home's supply. If discharge continues below the test floor, abort and restore
   mains; treat emergency protection as unverified and leave economic discharge
   disabled.
5. Restore mains and the original settings in every outcome. Confirm load power,
   native/slot limits, and UPS online state. If AC output latched off, restore it
   physically before considering the test complete. Resume automatic control
   only after both physical behaviors have passed.

Record software command tests separately from physical results. An unavailable
device, a failed read-back, or a skipped physical test is not a passing cutoff
test. Software running on Home Assistant cannot enforce a limit while its host
or connection is unavailable; autonomous cutoff must be verified at the device.

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
  reversed CT makes this backwards. Fix the CT before resetting the fault.
- **Grid meter configured and fresh.** If the optimizer cannot see grid power,
  it cannot derive the flow or verify zero export, so automatic control stays
  off. A missing load entity has the same effect on the derived flow.
- **The safety gate stops a repeat.** `Export safety fault` requests safe mode
  and blocks economic writes while export persists, so even a firmware bug
  cannot push energy to the grid on the next refresh.

## Periodic self-check

The dashboard exposes everything needed for a recurring verification:
**Grid export power**, **Export detected**, **Export safety fault**,
**Power source**, and the decision history. Export power should read 0 (or less)
in normal operation; any sustained positive reading is an incident to
investigate using the checklist above. After export stops, reset `Export safety
fault` by turning automatic control off then on; the switch stays armed during
the fault.
