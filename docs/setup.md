# Setup and commissioning

## Install

Install through HACS; do not copy component files into Home Assistant.

1. Open **HACS → Integrations** in Home Assistant.
2. Open the overflow menu, choose **Custom repositories**, and add
   `https://github.com/madslundt/House-battery` with category **Integration**.
3. Search HACS integrations for **House Battery**, select it, and choose
   **Download**.
4. Restart Home Assistant when HACS requests it.
5. Add **House Battery** from **Settings → Devices & services → Add
   integration**.

The GitHub repository is the installation source. Adding it as a HACS custom
repository lets HACS install and update the integration without a manual file
copy or a local repository checkout on Home Assistant.

House Battery does not yet discover or connect to a battery during its config
flow. First install and prove a local provider for the FBP1200; then bind the
provider's entities in House Battery. The included TCP research layer is
fail-closed and not a replacement for that proven connection.

## Connect the battery locally

1. Give the FBP1200 a DHCP reservation/static IP and prove local monitoring.
2. Confirm the provider reads SOC, charging/discharging power, on-grid status,
   and Operating Mode even when the vendor application is not holding the
   device session. AECC-compatible devices commonly use TCP port `8080`; use
   your provider's documented connection test rather than assuming a protocol.
3. Identify local, writable native minimum- and maximum-SOC controls.
4. Verify each intended mode manually: `Charge`, `Idle`/grid, and
   `Self-Gen/Zero Export`/battery. Check the provider's read-back and physical
   power sensors after each command.

## Bind the required entities

| Binding | What it must mean |
| --- | --- |
| Battery state of charge | Current battery SOC in percent. |
| Connected/house load power | The load the battery can genuinely serve, in W. |
| Grid import power | Imported power, in W. |
| Grid available / on-grid state | Physical/device-reported supply availability, not grid use. |
| Battery Operating Mode | Verified local `select` used for the three modes. |
| Known electricity-price entities | Actual published intervals in a supported list attribute. |
| Battery charge/discharge power | Measured physical battery power, in W. |

Grid export, PV input, fault/online status, and charge/discharge power controls
are optional. Native minimum/maximum SOC controls become mandatory when you
commission automatic control. See [configuration.md](configuration.md) for
accepted values and the full field reference.

## Commission safely

New entries run in **SHADOW** mode: they build a plan but never write to the
battery. Leave it there through at least one representative price horizon and
compare **Current decision**, **Battery mode**, **Operation plan**, and the
power sensors.

Before setting *I verified…* in Options and enabling **Automatic control**:

- Test all three modes and their read-back with normal household loads.
- Confirm the native SOC limits are writable and the device honours them.
- Confirm an outage changes **Grid available** to unavailable; do not test an
  electrical outage unless it is safe and planned.
- Check that local electrical protection and export rules are already correct.

If grid state becomes unknown/unavailable, data are stale, or the device is
faulted/offline, the optimizer stops economic control. An outage clears the
plan, asks for safe mode if needed, and latches automatic control off.
