# AECC upstream comparison: AFERIY PS240 local support

Research date: 2026-09-20. Sources below are pinned to the heads inspected on
this date: [`aecc-battery-local` at `099e3bb`](https://github.com/StekkerDeal/aecc-battery-local/tree/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8)
and [`Lunergy-Local-TCP` at `a5d9a66`](https://github.com/slaapyhoofd/Lunergy-Local-TCP/tree/a5d9a66ad8c0c599641bbdb972efbe32521cf90f).
They are useful implementation evidence, not a manufacturer protocol
specification. The former lists AFERIY PS240 as community-confirmed, which is
not the same thing as a firmware compatibility guarantee
([compatibility table](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/README.md#L42-L58)).

## Bottom line

House Battery already has the important protocol base: a serialised persistent
JSON-line client, `EnergyParameter` telemetry, the proven control registers,
bounded response size, and strict SOC-limit read-back
([current adapter](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/local_tcp.py#L44-L188)).
The best additions are safety/diagnostic hardening, rather than copying either
upstream's broad provider-integration surface.

| Priority | Recommended gap | Evidence and reason | Safe implementation boundary |
| --- | --- | --- | --- |
| P0 | Verify a mode write and read the complete initial control state | House Battery only reads `3023`/`3024`; `async_set_mode` and `async_set_self_consumption` accept the SET acknowledgement without read-back, then report a locally commanded mode ([local adapter](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/local_tcp.py#L68-L128)). Both upstreams read the mode/slot controls at startup; Lunergy reads `3000`, `3003`, `3021`, `3022`, `3023`, `3024`, and `3030` and parses the slot/mode ([implementation](https://github.com/slaapyhoofd/Lunergy-Local-TCP/blob/a5d9a66ad8c0c599641bbdb972efbe32521cf90f/custom_components/lunergy_local/coordinator.py#L315-L384)). AECC keeps a write lock through verification because later writes can otherwise race the read-back ([implementation](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/coordinator.py#L627-L798)). | Expand the *read-only* allowlist to those seven controls; after each mode write, read them under the existing lock and fail/latch automatic control off on a mismatch. Parse `3003` semantically (enabled and signed power), not as byte-for-byte text, because firmware may normalise CSV fields. Include the result in diagnostics/evidence. |
| P0 | Validate and quarantine suspect AECC telemetry before it reaches the planner/ledger | The direct decoder currently accepts any finite `0..100` SOC, even `SOC=0` while `TotalBatteryOutputPower` shows active discharge ([decoder](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/local_tcp.py#L191-L215)). AECC documents BMS/gateway loss-of-sync that returns false zeros and implements zero-during-active-flow, warm-up, and rate-of-change checks ([cleaner rationale and checks](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/cleaners.py#L1-L121)); it also holds partial `Storage_list` frames briefly before accepting them ([frame guard](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/coordinator.py#L183-L232)). | Keep the last *accepted* snapshot with timestamp. Reject implausible SOC/partial-stack frames; mark direct telemetry stale/unavailable after a short bounded hold. Crucially, do **not** plan or account from held values: a rejected snapshot must cause the existing health gate to stop automatic control. Add captured, redacted PS240 fixtures before tuning thresholds. |
| P1 | Test the self-consumption register sequence against PS240 hardware before treating it as settled | House Battery writes `3021=0, 3022=1` for self-consumption ([local adapter](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/local_tcp.py#L109-L128)). The current AECC upstream explicitly says `3020=3` is confirmed on AFERIY PS240 and restores self-consumption with **both** AI flags enabled (`3021=1`, `3022=1`), custom off, and a cleared slot ([register map and mode sequence](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/const.py#L182-L265)). | This is a high-value compatibility test, **not** authorisation to change the command. Capture PS240 vendor-app transitions and assert the exact pre/post control state in an integration test. Change the local restore sequence only when a PS240 fixture and physical commissioning test agree. |
| P1 | Add a short, optional identity/firmware probe and expose it in redacted diagnostics | Current setup checks only `EnergyParameter` ([config flow](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/config_flow.py#L138-L164)). AECC's `DeviceManagement` probe deliberately requests a small identity/RSSI allowlist and excludes credential registers; it tolerates a three-second timeout because not all firmware answers ([client](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/tcp_client.py#L96-L141)). Lunergy implements the same best-effort serial/firmware probe ([client](https://github.com/slaapyhoofd/Lunergy-Local-TCP/blob/a5d9a66ad8c0c599641bbdb972efbe32521cf90f/custom_components/lunergy_local/tcp_client.py#L48-L92)). | Probe only after the required telemetry handshake, never make setup fail on it, and redact serial/IP. This will make field reports and firmware-specific protocol fixtures materially more useful. |
| P2 | Detect multi-unit frames and avoid silently treating unit 0 as the whole battery | `_first_mapping(Storage_list)` makes the direct adapter use the first unit for fallback SOC/power ([local decoder](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/local_tcp.py#L191-L206)). AECC treats stacks as a first-class case and notes that local manual control reaches the master only ([upstream documentation](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/README.md#L188-L221)). | Initially, detect `len(Storage_list) > 1`, prefer verified system summaries, surface a diagnostic warning, and disable automatic control if aggregate values cannot be proven. Per-unit entities are optional future provider functionality, not required for the optimizer. |
| P3 | Adopt only read-only Modbus telemetry after an isolated proof of compatibility | AECC can probe/read holding registers on the same socket for faults, temperature, lifetime counters and ratings; it explicitly supports read-only function code 03 and closes the socket on malformed replies ([transport](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/tcp_client.py#L143-L182)). A confirmed fault source could feed House Battery's optional fault safety gate. | Make this a separately probed, read-only diagnostic capability. Do not add Modbus writes or make Modbus availability a setup prerequisite. |

## Confirmed scope differences (do not copy for parity)

- **Provider integration vs optimizer:** the upstreams publish broad telemetry,
  Energy Dashboard counters, manual controls, brands, translations, and
  multi-unit device trees. House Battery deliberately needs only
  control-critical direct telemetry and still requires independent household,
  grid-availability, and price inputs
  ([architecture](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/docs/architecture.md#L3-L22)).
  Full sensor/entity parity would enlarge the safety-critical surface without
  improving the optimizer.
- **Do not infer grid availability from AECC meter power.** The upstreams
  expose meter/grid power (Lunergy maps `MeterTotalActivePower` to it
  [here](https://github.com/slaapyhoofd/Lunergy-Local-TCP/blob/a5d9a66ad8c0c599641bbdb972efbe32521cf90f/custom_components/lunergy_local/coordinator.py#L53-L56)),
  but neither source establishes an on-grid boolean. House Battery correctly
  keeps that as a required physical input and fails safe when unknown
  ([health gate](https://github.com/madslundt/House-battery/blob/63d1cae8d0735b6a00916c11dec7bc9ac796fdd1/custom_components/house_battery/health.py#L47-L68)).
- **Do not copy automatic write retries.** AECC retries idempotent writes
  ([implementation](https://github.com/StekkerDeal/aecc-battery-local/blob/099e3bb078eea8ddfa9cf145b57e7e8da6cf73a8/custom_components/aecc_battery/coordinator.py#L610-L625)); House Battery's documented no-retry policy for a mutating call is the more suitable ambiguity rule for an autonomous optimizer. Improve observability/read-back, not retry aggressiveness.
- **Do not blindly adopt the 2400 W / register `3039` path.** Lunergy uses it
  to unlock its model-specific extended range
  ([implementation](https://github.com/slaapyhoofd/Lunergy-Local-TCP/blob/a5d9a66ad8c0c599641bbdb972efbe32521cf90f/custom_components/lunergy_local/coordinator.py#L250-L260)),
  whereas this adapter has a conservative 1200 W cap. Add a model/firmware
  capability table only after PS240 evidence; never expose a generic higher
  power setting.

## Suggested order

1. Add regression fixtures/tests for false-zero SOC, fragmented/partial frames,
   complete control-state decoding, and mode read-back ordering.
2. Implement P0 mode verification and telemetry quarantine; keep auto-control
   fail-closed on uncertainty.
3. Collect PS240 app-write captures to resolve the self-consumption flags and
   `3003` slot variant before changing command payloads.
4. Add the optional identity probe, then evaluate multi-unit and read-only
   Modbus support from real PS240 data.
