# Local FBP1200 adapter research

Research date: 2026-09-20. This note records the implementation boundary for
adding direct local FOSSiBOT FBP1200 support; it is not a claim that an
uncommissioned battery is safe to control.

## What the reference proves

MortUK's AFERIY project is an `aecc_battery` local-TCP integration, rather
than an FBP1200-specific protocol specification. Its manifest declares local
polling, no Python requirements, TCP discovery, and a separate configuration
flow ([manifest](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/manifest.json#L1-L25)).
It sends newline-delimited JSON over a persistent asyncio TCP stream:

- `Get: EnergyParameter` polls telemetry and `Get/Set:
  Energycontrolparameters` reads/writes control registers
  ([client API](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/tcp_client.py#L54-L67),
  [JSON framing and serialisation](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/tcp_client.py#L122-L200)).
- The reference uses one shared socket per `(host, port)`, locks connection
  access, and reconnects after an empty/failed response
  ([connection manager](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/tcp_manager.py#L11-L80),
  [failed GET recovery](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/tcp_client.py#L122-L150)).
- Its coordinator polls every five seconds (never faster than two), retains
  prior good data during a bounded failure tolerance, validates the response
  contains storage/summary data, and then raises `UpdateFailed`
  ([poll configuration](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/const.py#L73-L85),
  [poll validation](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/coordinator.py#L405-L467)).
- The observable controls are built on registers `3023` (minimum discharge
  SOC), `3024` (maximum charge SOC), and a custom time-slot record at `3003`.
  A negative slot power charges, positive discharges, and a disabled slot is
  idle ([register definitions](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/const.py#L157-L210),
  [custom command construction](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/coordinator.py#L2922-L2978)).
- It records every write, waits for a reply, and reads registers back where
  possible before declaring success
  ([write and verification path](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/coordinator.py#L2864-L2908)).
  Its operating-mode selector labels the resulting commands `Charge`, `Idle`,
  `Discharge`, `Feed`, and `Self-Gen/Zero Export`
  ([selector](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/select.py#L85-L226)).
  That selector is a *locally commanded* state, however, and explicitly warns
  that cloud/app actions may not update it; observed power/status remains the
  evidence of actual behaviour
  ([selector attributes](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/custom_components/aecc_battery/select.py#L109-L141)).

The reference fork itself only mentions FOSSiBOT compatibility in a changelog
fix for an initial false-zero SOC report
([changelog](https://github.com/MortUK/Aferiy-PS240-Local-/blob/6dbe81e130fa9d5fcb2d385a9a982f9b8fb96781/CHANGELOG.md#L80-L89)).
It does **not** identify FBP1200 in its config flow, model constants, or
command map. Therefore it is useful architectural evidence, not sufficient
device validation by itself.

## Direct upstream evidence

The current upstream `StekkerDeal/aecc-battery-local` is materially more
relevant: it explicitly lists **Fossibot FBP 1200** as “Community confirmed”,
with the stated firmware limitation that per-unit charge and per-string PV
readings are zero ([compatibility table](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/README.md#L42-L58)).
This remains a community claim, not a manufacturer protocol contract.

That upstream has hardened the same design:

- A connection manager serialises access, closes stale sockets, and uses
  exponential reconnect cooldowns ([manager](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/tcp_manager.py#L11-L150)).
- JSON commands and reads are serialised on that socket; it also has a
  read-only Modbus diagnostic path which must not become a control path
  ([client](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/tcp_client.py#L79-L182)).
- It documents a single local session on some firmware and warns that the
  vendor app may block control; it also notes one output cap cannot be altered
  by local TCP ([connection limitation](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/README.md#L127-L139),
  [single-session behaviour](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/README.md#L348-L348)).

The upstream is MIT licensed ([license](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/LICENSE)),
but copying substantial code requires retaining its copyright and license
notice. Tracking an external custom integration as a runtime import is not a
stable Home Assistant packaging mechanism; the existing project only has an
`after_dependencies` ordering hint and currently expects selected entities
([current manifest](/Users/madslundt/Documents/house_battery/custom_components/house_battery/manifest.json),
[current config flow](/Users/madslundt/Documents/house_battery/custom_components/house_battery/config_flow.py#L56-L91)).

## Recommendation

Implement a **first-party, dedicated `fbp1200_local` adapter inside this
integration**, with the AECC wire protocol treated as a versioned, tested
compatibility layer. Do not make `aecc_battery` a required runtime dependency:
users cannot rely on a custom component being installed, importable, or at a
compatible revision, and two integrations must never open competing sessions
to a device that may only support one.

The direct adapter should be optional during migration: preserve the current
entity-binding adapter for existing installations, but make direct local setup
the supported path. It should create the physical device and the telemetry,
native SOC controls, and operating-mode entities itself; the optimizer must
consume an internal typed adapter interface rather than its own entities or
raw Home Assistant service calls.

Suggested seams:

1. `Fbp1200Transport`: JSON-line request/response with one I/O lock, injected
   clock/sleep, bounded connect/read/write timeouts, close-on-any-framing
   failure, reconnect backoff, and no automatic write retry.
2. `Fbp1200Protocol`: pure request construction and response decoding for
   `EnergyParameter` and the allowlisted control registers. Reject missing,
   malformed, impossible, or stale frames; scale fields only from verified
   fixture data.
3. `Fbp1200DeviceAdapter`: coordinator that publishes one atomic accepted
   telemetry snapshot and exposes `set_limits` plus a small, allowlisted
   `set_mode` API. Each mutating operation must read back the exact registers
   and report an unambiguous confirmation result.
4. `OptimizerControlPort`: the existing planner-facing control interface. It
   must pause physical writes on an uncertain write, stale telemetry, unknown
   grid availability, or any read-back mismatch while retaining the user's
   authorization. Retry on the next refresh using fresh telemetry; after three
   consecutive local transport or command failures, request a rate-limited
   integration reload. Never blindly replay an ambiguous protocol write.

## Non-negotiable safety gates

- Configuration must attempt a read-only identity/telemetry handshake before
  creating a controllable entry. Do not infer FBP1200 solely from a host/port
  or mDNS advert; retain model/firmware/serial diagnostics and require the
  user to confirm the physical device.
- Start in shadow mode. Require an explicit commissioning sequence that proves
  safe/idle, charge, discharge/self-consumption, minimum SOC, maximum SOC,
  grid-availability input, and post-write read-back against the actual unit.
  Keep grid availability as a separately configured physical signal: the
  investigated protocol sources do not establish a trustworthy on-grid
  boolean, and grid import must not be used as a substitute.
- Allow only the known control-register allowlist. No broad register write,
  no unbounded power setpoint, no secret/credential register reads, and no
  Modbus writes. A command acknowledgement without matching read-back is a
  failure.
- On transport loss, malformed/stale frames, fault, or outage, pause optimizer
  writes, preserve the last observed physical state as stale (not current),
  and surface the failure. Request safe mode when a fresh transport is
  available; retry recovery at the coordinator's bounded cadence without
  turning off the user's authorization.
- Integration tests need a scripted TCP server for fragmented JSON, empty
  replies, concurrent polling/control, socket resets, slow replies, malformed
  frames, and exact command/read-back ordering. Keep protocol fixtures captured
  from this FBP1200 with serials, IPs, and credentials redacted. Hardware
  acceptance tests must cover every supported firmware before declaring the
  direct adapter production-ready.

## Decision record

Use MortUK's project for the proven AECC control shape and upstream
`aecc-battery-local` for current FBP1200 compatibility evidence, but do not
copy its overnight-price strategy. The House-battery optimizer remains the
sole economic planner; the new adapter is only a deterministic local device
provider.

## Physical control verification (2026-09-26)

The physical FBP1200 was tested with the Home Assistant config entry
temporarily disabled to release its single TCP session. Baseline telemetry
showed 149 W connected load, 0 W reported battery output and Self-Gen controls
(`3020=3`, `3021=1`, `3022=1`, `3030=0`). Writing a 100 W custom Discharge slot
produced three consecutive non-zero output samples (316 W, 304 W and 333 W).
Restoring the complete baseline register set returned reported output to 0 W.
This verifies that custom Discharge starts physical output on this unit while
native Self-Gen remains idle without a CT signal. Raw output power remains
diagnostic-only and is not treated as a calibrated measure of delivered AC
power.

## Backup-output investigation (2026-10-24)

The separate off-grid output did not stop at an elevated native/slot minimum,
including an autonomous SOC crossing from 20% to 19%. See
[commissioning results](../commissioning.md). No FBP1200 backup-cutoff command
has been verified. Version 1.7.3 blocked direct-local economic discharge;
version 1.7.4 removed that block. The user has enabled automatic control.

Additional bounded, read-only observations on this unit:

- mDNS reports logger type 131. This identifies the logger, not the inverter
  firmware or its control register map.
- Modbus holding register 65030 returned the same SOC as JSON telemetry.
- Candidate shutdown register 41116 from the Humsienk register map returned
  Modbus exception 4. Its FBP1200 meaning and accessibility are not established.
- A holding-register read at candidate AC-switch address 4000 also returned
  exception 4; a coil read at 4000 timed out. Neither is a usable control.
- The initial JSON RTU pass-through probe parsed only one response line and
  produced an inconclusive result. A corrected complete-message reader later
  confirmed that pass-through works; see the TCP follow-up below.

The [vendor EMS interface](https://shuoxd.com/document/appEMS?language=en)
documents JSON, Modbus TCP and RTU pass-through as separate access methods.
Do not assume their parameter namespaces are interchangeable.

Static inspection of the [Fossibot vendor app distribution](https://www.pgyer.com/fossibot)
(version 1.260516.3; SHA-256
`e0d16f0aa1d452ac4f2bd7ecfe2204b18aa203b3e4954252e7bfc69c7a9ff8d4`)
found a shared cloud AC-switch handler at address `0xFA0` (4000). Its UI is
gated to Bienesol app ID 33; Fossibot uses app ID 17. Related BLE switch and
SOC controls belong to WS logger type 174. Their presence in a shared app
bundle is insufficient evidence that FBP1200/logger type 131 supports them.
No command from these unrelated paths was written to the battery.

## TCP 8080 follow-up (2026-10-24)

Read-only probes with exclusive access to the battery's existing TCP 8080
endpoint confirmed the following interfaces:

| Interface | Result |
| --- | --- |
| JSON `EnergyParameter` | Returned SOC 19%, 0 W battery output and connected-load telemetry. |
| JSON `Energycontrolparameters` | Returned SOC registers 3023 and 3024. |
| JSON `EnergycontrolparametersList` | Returned named fields including `EnergyManagement`, `ScheduledModeEnabled` and `SystemMaxPowerLimit`. Other documented fields did not all answer within the bounded probe. |
| JSON `DataTransmission`, inner Modbus function 03 | Returned a valid, CRC-checked native SOC response. |
| Direct Modbus function 43 / MEI 14 basic device identification | No response within 8 seconds. No register address or write operation was sent. |
| Individual named `InstantControlPeriod` and `PowerControlPeriod1` reads | No response within 8 seconds each. |
| Individual named `PeakShavingConfig` read | Returned an empty `ControlInfoField`; no cutoff capability established. |
| Narrow JSON `DeviceManagement` read, IDs 20 and 21 | Returned `ControlInfo` with model `GTSW0000` and firmware `1.4.9.9.1.0`. |

A fresh `EnergyParameter` read exposed only the published storage telemetry
fields, with `DevAddr=1` and SOC 19%; it supplied no model, firmware or DTC.
Timeouts establish only that these bounded requests did not answer, not that
the underlying feature is absent. None of these observations justifies a
backup-output write or another physical cutoff test.

The successful identity request used `RegDeviceManagementAddr: [20, 21]`,
following the upstream integration's identity field mappings. The previous
request for IDs 8 and 9 had timed out; an unanswered batch must not be treated
as proof that every field on that interface is unavailable. `GTSW0000` is a
logger identifier also reported on unrelated Growatt equipment, so it does
not identify the FBP1200 inverter protocol or justify using another model's
cutoff registers.

The native SOC read used the vendor-documented RTU pass-through envelope:

```json
{
  "Set": "DataTransmission",
  "SerialNumber": 301,
  "CommandSource": "HA",
  "SetCommand": {
    "FunctionCode": 3,
    "TransmittedData": "01 03 FE 06 00 01 55 E3"
  }
}
```

Despite the outer `Set` name, the inner function 03 is a read. The returned RTU
frame was `01 03 02 00 13 F9 89`, representing SOC 19. A separate read of
candidate shutdown address 41116 returned CRC-valid exception frame
`01 83 04 40 F3`. The outer `ControlState` still said `succeed`: successful
pass-through does not establish success of the enclosed Modbus operation.
That candidate is not a usable FBP1200 cutoff control.

These probes wrote no battery settings. Home Assistant's battery integration
was temporarily unloaded to release its single TCP session, then restored.
The user's newly enabled automatic-control setting was preserved. Installed
version 1.7.4 reports backup cutoff unverified and discharge unblocked; it
removed the 1.7.3 lock. Final checks showed mains on and battery discharge 0 W.

## Capturing this FBP1200 from the original iPhone app

No public FBP1200-specific backup-cutoff register specification was found in
the sources examined. Capture the installed app's actual device settings
instead of treating shared code for another brand as a supported command.
The battery is already visible in the user's original iPhone app.
The user reports that no minimum or maximum SOC setting is visible there.
This does not establish whether the firmware supports an autonomous backup
cutoff. Availability of an AC-output switch in this FBP1200's app remains
unconfirmed; even a verified switch would establish output control rather
than an autonomous minimum-SOC threshold.

### First capture: connection and settings reads

1. Get PacketLogger from Apple's
   [Additional Tools for Xcode](https://developer.apple.com/download/all/?q=additional).
   The download requires Apple Developer sign-in. PacketLogger was not found
   on the Mac during preparation.
2. On the **iPhone**, use Apple's
   [Bluetooth for iOS/iPadOS profile and current instructions](https://developer.apple.com/feedback-assistant/profiles-and-logs/?name=bluetooth).
   Follow its installation/restart instructions. This is the iPhone logging
   profile, not the Mac Bluetooth-controller profile.
3. Connect the unlocked iPhone to the Mac over USB and accept the trust prompts.
   In PacketLogger choose **File → New iOS Trace** and select the iPhone.
   Apple demonstrates this [capture route](https://developer.apple.com/videos/play/wwdc2019/901/?time=1752).
4. Keep mains connected and discharge disabled. Record the times when opening
   the battery page, opening its settings and refreshing those pages. Initially
   change no settings, switches, schedules, firmware or network bindings.
5. Stop and save the short trace locally. Inspect whether those actions produce
   a battery BLE connection with ATT characteristic writes and notifications.
   If not, this trace has not captured the app's control route; the app may be
   using its cloud service. Remove the logging profile after capture is complete.

### Decode and identify the control

Preparation includes a private local capture directory:
`~/Library/Logs/HouseBattery/2026-10-24/` (mode 0700). It contains an offline
decoder prototype and the inspected vendor APK. Neither capture data nor SDK
material should be committed to Git or uploaded as an entire phone diagnostic.

The prototype extracts AES material from the APK into memory and validates
framing and checksums before interpreting a message. It redacts logger
provisioning/authentication values. Synthetic tests passed; **real iPhone
compatibility is not yet verified**. It does not parse `.pklg` directly.
Export ATT value bytes from the trace, preserving ordering and keeping each
direction/connection/characteristic stream separate. Its hex input accepts
one value chunk per line, without HCI/L2CAP/ATT headers.

```sh
.venv/bin/python ~/Library/Logs/HouseBattery/2026-10-24/decode_ble_capture.py \
  --apk ~/Library/Logs/HouseBattery/2026-10-24/fossibot-1.260516.3.apk \
  --hex-file /absolute/path/to/sent-att-values.txt
```

After a readable baseline is established, inspect the exact setting names
offered for this FBP1200. A later supervised capture can compare one raised
backup-minimum setting with its original value, including the response and
read-back. Do not infer a SET command from a read, another brand's address, or
an unrelated charging/EMS limit. A captured AC switch would only establish an
output-control command; it would not establish autonomous low-SOC protection.
An elevated-threshold physical cutoff test must pass before claiming that the
emergency minimum protects the backup output. The user's later decision to
enable automatic control does not constitute verification of that protection.

### If the settings use HTTPS instead of Bluetooth

A router trace or iPhone IP trace alone cannot reveal encrypted request bodies.
The alternative is a short, local HTTPS inspection session for the app's actual
vendor host using a trusted debugging proxy. This would require configuring
the iPhone's Wi-Fi proxy and trusting that proxy's certificate; it is a separate
setup step and has **not** been performed. See
[mitmproxy's certificate and iOS instructions](https://docs.mitmproxy.org/stable/concepts/certificates/).
Capture the settings query/response first, then only the one supervised control
transaction. Keep authentication headers, serials and raw captures private.
If the app rejects the proxy certificate, stop and reassess rather than
assuming the interception succeeded. Remove proxy settings and certificate
trust afterward.

The shared app's generic settings path submits logical field names and a
`setTemplateId` to `deviceSetConfig/setCustomParams`. Translation into a native
device command can therefore happen on the server. An HTTPS capture may reveal
the FBP1200's DTC and supported setting schema without revealing the native
register/function. Such a capture alone is not sufficient evidence for a local
register write; it may require a device-side command capture or the vendor's
model-specific protocol/template specification.

The manufacturer's documentation request should specifically ask for the
**FBP1200 backup/off-grid AC output's autonomous minimum-SOC cutoff**, its local
protocol, address/function, units, read-back and restart behavior. State that
EMS register 3023 and the active schedule minimum were accepted but did not
stop backup output in elevated-threshold tests. No support message has been sent.
