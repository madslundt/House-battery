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

House Battery connects to the battery itself. In the first setup screen, enter
the battery's local IP address, TCP port (normally `8080`), and a display name.
The flow performs a read-only telemetry handshake before it creates the entry.
It then creates the battery SOC, charging/discharging-power, native SOC-limit,
and operating-mode entities under the new device. No separate battery-provider
integration is needed.

## Connect the battery locally

1. Give the battery a DHCP reservation/static IP.
2. Add **House Battery**, enter its IP address, TCP port, and name, then let
   the read-only connection check complete.
3. Verify the created **Battery state of charge**, **Battery charge power**,
   **Battery discharge power**, **Native minimum SOC**, **Native maximum SOC**,
   and **Operating mode** entities.
4. Verify each intended mode manually: `Charge`, `Idle`, and
   `Self-Gen/Zero Export`. The operating-mode entity is a local
   commanded state; check the physical power sensors after every command.

## Bind the required entities

| Binding | What it must mean |
| --- | --- |
| Battery-served local load | Direct-local entries automatically use the complete per-storage off-grid total. Other local readings are diagnostic only. |
| Grid import power | Imported power, in W. |
| Grid available / on-grid state | Physical/device-reported supply availability, not grid use. |
| Known electricity-price entities | Actual published intervals in a supported list attribute. |

Fault and online status are optional. The direct
adapter supplies the battery telemetry and native SOC controls; the latter are
validated again when you enable automatic control. See
[configuration.md](configuration.md) for accepted values and the full field
reference.

## Commission safely

New entries run in **SHADOW** mode: they build a plan but never write to the
battery. Leave it there through at least one representative price horizon and
compare **Current decision**, **Battery activity**, **Operation plan**, and the
power sensors.

Before setting *I verified…* in Options and enabling **Automatic control**:

- Test all three modes and their read-back with normal household loads.
- Confirm the native SOC limits are writable and the device honours them.
- Confirm an outage changes **Grid available** to unavailable; do not test an
  electrical outage unless it is safe and planned.
- Check that local electrical protection and operating requirements are already correct.

If grid state becomes unknown/unavailable, data are stale, or the device is
faulted/offline, the optimizer stops economic control. An outage clears the
plan, asks for safe mode if needed, and latches automatic control off.
