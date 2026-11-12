# Setup and commissioning

## Install

House Battery is distributed as a **custom HACS integration**. Install through
HACS; do not copy component files into Home Assistant.

1. Open **HACS → Integrations** in Home Assistant.
2. Open the overflow menu, choose **Custom repositories**, and add
   `https://github.com/madslundt/House-battery` with category **Integration**.
3. Search HACS integrations for **House Battery**, select it, and choose
   **Download**.
4. Restart Home Assistant when HACS requests it.
5. Add **House Battery** from **Settings → Devices & services → Add
   integration**.

Alternatively, after the restart, use the **Add integration** button in the
repository README. It opens the same config flow in the selected Home Assistant
instance.

The GitHub repository is the installation source. Adding it as a HACS custom
repository lets HACS install and update the integration without a manual file
copy or a local repository checkout on Home Assistant.

Home Assistant 2025.1 or later is required. The local `brand/` icon and logo
are shown by Home Assistant 2026.3 and later; they do not affect the
integration's operation on earlier supported versions.

Choose the device type in the first setup screen. For FOSSiBOT, use the direct
local connection described below. For EcoFlow, install and configure the
[EF BLE integration](https://github.com/rabits/ha-ef-ble) first, then select its
telemetry, SOC controls, AC Port switch, and optional writable AC Charging
Speed number. The EcoFlow setup asks for installed capacity and rated AC output
power. Its charge-power setting follows the selected number's unit and native
bounds; if no writable charging control is available, the setting is disabled
and automatic charging is unavailable. A grid-isolation switch is required for
EcoFlow control.

## Connect a FOSSiBOT directly

1. Give the battery a DHCP reservation/static IP.
2. Add **House Battery**, enter its IP address, TCP port, and name, then let
   the read-only connection check complete.
3. Verify **Battery state of charge**, battery power telemetry, and the
   read-only **Native minimum SOC** and **Native maximum SOC** diagnostics.
4. During commissioning, verify the local adapter's mode readback and physical
   power response for each intended action. Use **Operation mode** to select
   `auto`, `charge`, `battery`, or `grid` after automatic control is enabled.

## Connect EcoFlow through EF BLE

1. Install EF BLE and pair the EcoFlow power station with Home Assistant.
2. Confirm the battery level, AC input/output power, AC Port switch, and SOC
   limit controls are available with the units shown in their entity details.
3. Add **House Battery**, choose **EcoFlow (EF BLE entities)**, and bind the
   matching entities. The AC Charging Speed control is optional; if selected,
   it must be writable and expose valid `W` or `kW` limits. The two SOC limit
   controls must be separate percent number entities.
4. Enter installed usable capacity in kWh and rated AC output power in W. Set
   up a separate grid-isolation switch and verify its physical behavior before
   commissioning.

## Bind the required entities

| Binding | What it must mean |
| --- | --- |
| Battery-served local load | Direct-local entries automatically use the complete per-storage off-grid total. Other local readings are diagnostic only. |
| Grid import power | Imported power, in W; required for legacy provider entries only. Direct-local entries use battery telemetry. |
| Grid available / on-grid state | Physical/device-reported supply availability, not grid use. |
| Known electricity-price entities | Actual published intervals in a supported list attribute. |

Fault and online status are optional. The direct adapter supplies battery
telemetry and read-only native SOC readbacks; those bounds are validated again
when you enable automatic control. See
[configuration.md](configuration.md) for accepted values and the full field
reference.

## Commission safely

New entries run in **SHADOW** mode: they build a plan but never write to the
battery. Leave it there through at least one representative price horizon and
compare **Current plan slot**, **Battery activity**, **Operation plan**, and the
power sensors.

Before setting *I verified…* in Options:

- Review the local mode mapping and the effects of each action.
- Confirm the native SOC limits are writable and the device honours them.
- Confirm an outage changes **Grid available** to unavailable; do not test an
  electrical outage unless it is safe and planned.
- Check that local electrical protection and operating requirements are already correct.

After commissioning, enable **Automatic control** during supervised
operation and verify each forced action's local readback and physical power
response. Forced **Operation mode** selections are unavailable while automatic
control is off.

If grid state becomes unknown/unavailable, data are stale, or the device is
faulted/offline, the optimizer stops economic control. An outage clears the
plan and asks for safe mode if needed, while leaving automatic control armed.
It retries on the next coordinator refresh and resumes automatically after the
health issue clears. Three consecutive local TCP or command failures schedule
an integration reload, limited to one attempt per 15 minutes. This recovery
does not bypass commissioning, telemetry, SOC, load, grid, or export safety
checks.
