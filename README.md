# House Battery for Home Assistant

House Battery is a local, deterministic optimizer for a compatible home
battery. It learns the connected load, plans against every
known electricity-price interval, and chooses `charge`, `grid`, or `battery`
only when losses, battery wear, and the configured profit requirement are met.

It is intentionally conservative: it is not a transfer switch, UPS safety
controller, or battery-management system. Battery firmware and the installed
local device provider remain responsible for electrical protection and hard
limits.

The integration connects directly to a compatible battery through its local
TCP interface. Setup starts with the battery IP address, port (normally 8080),
and a device name; it then creates the battery telemetry, native SOC-limit,
and operating-mode entities itself. Home load, physical on-grid state, and
electricity-price entities are still selected separately because they are not
reported reliably by the battery protocol. There is no cloud account or
external optimizer.

## Start here

- [Setup and commissioning](docs/setup.md) — installation, local battery
  connection, required bindings, and safe first use. Installation is through
  HACS from this GitHub repository; no manual component-file copy is needed.
- [Configuration reference](docs/configuration.md) — every config-flow field
  and safety rule.
- [Optimizer and forecast guide](docs/optimization.md) — planning economics,
  external price forecasts, uncertainty, accuracy scoring, ROI, and tuning.
- [Entity, control, and action reference](docs/entity-reference.md) — plain
  language descriptions and realistic examples for every entity.
- [Examples](docs/examples.md) — dashboards, templates, scheduled loads,
  exports, and a weekly tariff scenario.
- [Architecture](docs/architecture.md) — deterministic model, safety gates,
  and data flow.

## What it provides

- A plan across contiguous source price intervals (hourly, quarter-hourly, or
  another valid cadence), optionally extended with an external forecast.
- Continuous load learning plus conservative usable-capacity and efficiency
  estimates.
- Expected and realized savings, throughput, cycles, estimated degradation,
  decision history, diagnostics, and JSON evidence export.
- Independent **Grid available** status, which means on-grid supply exists —
  never merely that the load is currently importing power.
- A commissioning gate and separate **Automatic control** switch. Every new
  entry starts in safe shadow mode.

## Quick safety rules

Keep automatic control off until each local mode, SOC limit, read-back, load
path, and electrical/export setting has been tested. Bind **Grid available**
to a physical on-grid/device status, never a power sensor. Stale telemetry,
faults, an unknown on-grid status, or an outage fail safe and latch automatic
control off.

The invariant is always:

```text
0 ≤ absolute emergency SOC ≤ arbitrage reserve SOC
  < maximum charge SOC ≤ extra-storage charge SOC ≤ 100
```

## Development

```bash
uvx ruff check custom_components/house_battery tests/test_house_battery_*.py
uv run --with pytest --with 'homeassistant>=2025.1' \
  pytest -q tests/test_house_battery_*.py
```

Source, releases, and issues: [github.com/madslundt/House-battery](https://github.com/madslundt/House-battery).
