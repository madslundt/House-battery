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
   must disable automatic execution on an uncertain write, stale telemetry,
   unknown grid availability, or any read-back mismatch.

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
- On transport loss, malformed/stale frames, fault, or outage, stop optimizer
  writes, preserve the last observed physical state as stale (not current),
  and surface the failure. A safe fallback may be attempted once only if a
  fresh transport is available; do not endlessly retry battery commands.
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
