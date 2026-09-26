# Evaluating optimizer performance with an LLM

An LLM can be useful as an independent reviewer of House Battery evidence. It
should not control the battery, decide live actions, or receive an unbounded
database connection. The durable pattern is: keep complete history in your
time-series database, extract a small reproducible analysis window, then give
that evidence and the review prompt to the LLM.

The result is an advisory review: it can identify likely missed savings,
unnecessary cycling, faulty forecasts, or execution mismatches. It cannot
replace the deterministic planner or prove utility-bill savings from a single
period.

## Export the current evidence bundle

Run this action in **Developer Tools → Actions** before each review:

```yaml
service: house_battery.export_data
data: {}
```

The response contains the effective settings, current status and plan, learned
load and battery models, interval ledger, decision history, scheduled loads,
and price-forecast accuracy. Review the data before sending it outside Home
Assistant. It is designed to omit common secrets, but it is still your
operational data.

Use a 14 to 28 day window when the battery has been operating normally. A
shorter window is useful for diagnosing an incident; it is not enough evidence
for a policy change. Exclude or label outages, `DEGRADED`/`RECOVERING` periods,
commissioning, manual overrides, and days with missing price or power data.

## Entities to retain in long-term history

Keep the usual House Battery telemetry at a 1 to 15 minute cadence:

| Purpose | Entities |
| --- | --- |
| Safety and control state | **Optimizer state**, **Optimizer problem**, **Grid available**, **Automatic control**, **Current plan slot**, **Battery activity**, **Operating mode** |
| Plan and its inputs | **Operation plan**, **Current plan slot**, **Planned load power**, **Current electricity price**, **Extra storage policy**, **Effective charge target SOC** |
| Physical outcome | **Battery state of charge**, **Connected load power**, **Grid import power**, **Battery charge power**, **Battery discharge power** |
| Aggregate economics | **Expected plan savings**, all **Estimated realized savings** periods, battery charge/discharge counters, **Equivalent full cycles**, and **Estimated battery degradation** |
| Model quality | **Load learning coverage**, **Load forecast mean absolute error**, **Battery learning**, **Learned usable capacity**, **Learned round-trip efficiency**, and **External price forecast accuracy** |

Retain the attributes of **Current plan slot** and **Plan execution**. The
former is the current planner action and identifies the information used for
the active price interval; the latter distinguishes planned movement from
physical battery telemetry. The slot start and end are attributes, so samples
can be indexed by plan interval without a separate current-decision entity.

Also retain the raw price-source intervals. A current-price sensor alone does
not show the price horizon the planner could see. For external forecasts,
retain source name, publication/update time, each interval's start/end/price,
and whether it was known or forecast data.

## Prepare a bounded VictoriaMetrics extract

VictoriaMetrics is a good long-term evidence store. It is better than adding a
direct LLM connection because it preserves the source-of-truth history and
makes each review repeatable. Query it first, then send the returned extract
to the model.

For each review window, create two artifacts:

1. **Quarter-hour evidence table.** Resample to the planner's price cadence
   (often 15 minutes). Include timestamp, price, price source, planned action,
   planned load, actual mean load, SOC start/end, mean grid import, charged
   energy, discharged energy, plan-execution state, and ledger savings/quality.
2. **Configuration and events bundle.** Include `export_data`, plan/decision
   changes, scheduled loads, forecast accuracy, configuration changes, and all
   non-healthy safety events in the same window.

Use sums for energy, a time-weighted mean for power and price, first/last for
SOC, and the latest value for action or diagnostic state. Preserve timestamps,
units, timezone, and missing values. Do not turn unavailable telemetry into
zero. Limit the query to the chosen time range and a defined set of entity
series; a 28-day, 15-minute table is only 2,688 rows and is usually practical
to attach as CSV or JSON.

Give an LLM a read-only, query-limited VictoriaMetrics account only if you need
interactive investigation. Restrict it to these series, a fixed maximum time
range/point count, and logged queries. Do not grant write, delete, alerting,
or Home Assistant service permissions. In most cases, a generated extract is
safer, cheaper, and produces a review another person can reproduce.

## Generate a 15 day VictoriaMetrics bundle

This procedure exports numeric telemetry as 15-minute samples. It deliberately
does not assume a metric name: Home Assistant's Prometheus exporter normally
uses the entity's unit or its configured metric override as the metric name,
and every installation may also apply a namespace. The exported series carry
an `entity` label. Confirm the exact metric and label names in VMUI before
copying them into the commands below.

### 1. Confirm the series in VMUI

Open VMUI and use Metrics explorer or Query. If your Home Assistant
Prometheus exporter has no namespace, a useful starting query is:

```promql
entity_info{entity=~"sensor\\..*house_battery.*"}
```

If a namespace is configured, prefix `entity_info` with it. Inspect a numeric
House Battery entity such as **Battery state of charge** or **Connected load
power**, then copy the exact metric name and `entity` label from the result.
Do the same for your raw tariff sensor. The Home Assistant Prometheus exporter
does not reliably turn arbitrary text state or state attributes into useful
numeric series, so keep plan reasons, state transitions, and `Current plan
slot` attributes in the separate `export_data` evidence bundle.

### 2. Set a completed 15 day window

Use an end time at a completed 15-minute boundary; this avoids mixing a
partially observed current interval into the comparison. Set these values in a
terminal on the machine that can reach VictoriaMetrics. Do not put a password
or token in the generated evidence files.

```sh
export VM_QUERY_URL='https://victoriametrics.example/api/v1/query_range'
export VM_START='2026-09-07T00:00:00Z'
export VM_END='2026-09-22T00:00:00Z'
export VM_STEP='15m'
export VM_EVIDENCE_TOKEN='replace-with-a-read-only-token'
```

For a VictoriaMetrics cluster, use the query URL in this form instead:

```text
https://vmselect.example/select/TENANT_ID/prometheus/api/v1/query_range
```

Create a tiny helper so every export has the same range and step:

```sh
vm_range() {
  curl --fail --silent --show-error --get "$VM_QUERY_URL" \
    -H "Authorization: Bearer $VM_EVIDENCE_TOKEN" \
    --data-urlencode "query=$1" \
    --data-urlencode "start=$VM_START" \
    --data-urlencode "end=$VM_END" \
    --data-urlencode "step=$VM_STEP"
}
```

If your proxy uses a different authentication mechanism, replace only the
`Authorization` header. Do not give this token to the LLM.

### 3. Export each numeric series

Replace `YOUR_METRIC` with the metric discovered in VMUI and replace each
entity ID with the one generated by your own House Battery entry. Different
units often use different metric names, so `YOUR_METRIC` may differ on each
line. With a regular scrape interval, `avg_over_time` produces a useful
15-minute mean for power; do not use a simple sample sum for power.

```sh
mkdir -p llm-evidence/vm

vm_range 'avg_over_time(YOUR_METRIC{entity="sensor.house_battery_connected_load_power"}[15m])' \
  > llm-evidence/vm/actual_load_w.json
vm_range 'avg_over_time(YOUR_METRIC{entity="sensor.house_battery_grid_import_power"}[15m])' \
  > llm-evidence/vm/grid_import_w.json
vm_range 'avg_over_time(YOUR_METRIC{entity="sensor.house_battery_battery_charge_power"}[15m])' \
  > llm-evidence/vm/battery_charge_w.json
vm_range 'avg_over_time(YOUR_METRIC{entity="sensor.house_battery_battery_discharge_power"}[15m])' \
  > llm-evidence/vm/battery_discharge_w.json
vm_range 'last_over_time(YOUR_METRIC{entity="sensor.house_battery_battery_state_of_charge"}[15m])' \
  > llm-evidence/vm/soc_end_pct.json
vm_range 'last_over_time(YOUR_METRIC{entity="sensor.house_battery_planned_load_power"}[15m])' \
  > llm-evidence/vm/planned_load_w.json
vm_range 'last_over_time(YOUR_METRIC{entity="sensor.house_battery_current_electricity_price"}[15m])' \
  > llm-evidence/vm/price_dkk_per_kwh.json
```

Add equivalent files for the raw tariff source and numeric policy/model values
that changed during the period. Retain **Battery charge/discharge total**
counters if available, but use the House Battery interval ledger as the
accounting authority rather than attempting to infer savings from Prometheus
counter changes alone.

An LLM needs a wide, aligned table rather than separate query responses. Merge
the returned `data.result[].values` arrays by their Unix timestamp into
`quarter_hour_evidence.csv` or `quarter_hour_evidence.json`. Each row should
contain at least:

```text
timestamp_utc,price_dkk_per_kwh,planned_load_w,actual_load_w,soc_end_pct,
grid_import_w,battery_charge_w,battery_discharge_w
```

Derive `charged_kwh` and `discharged_kwh` only after confirming the series are
power in watts: for a complete 15-minute row, `kWh = mean_W × 0.25 ÷ 1000`.
Leave incomplete rows blank and label them incomplete. Do not silently use
zero for a series absent from a query result.

### 4. Add non-numeric evidence and a manifest

Run `house_battery.export_data` in Home Assistant and save the returned JSON
as `llm-evidence/optimizer_export.json`. It supplies the decision history,
interval ledger, plan, settings, learned models, forecast accuracy, and
scheduled loads that the Prometheus-style numeric export cannot preserve.

Create `llm-evidence/manifest.json` alongside the files:

```json
{
  "start": "2026-09-07T00:00:00Z",
  "end": "2026-09-22T00:00:00Z",
  "step": "15m",
  "timezone_for_reporting": "Europe/Copenhagen",
  "source": "VictoriaMetrics",
  "excluded_periods": [],
  "notes": "Power values are 15-minute means; energy is derived only for complete rows."
}
```

Before attaching the bundle, remove hostnames, tokens, public IP addresses,
Wi-Fi names, and unrelated household entities. Keep the original local bundle
unchanged so a later reviewer can reproduce the analysis. VictoriaMetrics
supports the Prometheus-compatible range-query API and raw CSV/JSON export;
use raw export only to diagnose an unexpected sample, not as the default LLM
input because it is much larger. [VictoriaMetrics query and export examples](https://docs.victoriametrics.com/victoriametrics/url-examples/)

## Review prompt

Attach the current `export_data` JSON and the bounded historical extract after
this prompt.

```text
You are an independent reviewer of a Home Assistant battery optimizer. Assess
the supplied evidence; do not control equipment or infer data that is absent.

The optimizer chooses charge, grid, or battery for each price interval using
the price horizon available then, predicted connected load, SOC, capacity,
charge/discharge limits, round-trip efficiency, degradation cost, minimum
profit, reserve SOC, switching penalty, minimum mode duration, and transition
budget. Known prices are authoritative. Forecast prices, if used, have a
conservative uncertainty buffer.

Review requirements:
- Evaluate a past plan only against information available at its decision time.
  Do not use later prices or realised load as if they were known.
- Separate execution mismatch, load/price forecast error, telemetry quality,
  configuration choice, and planner/economic issue.
- Include efficiency, degradation, profit threshold, reserve, power/capacity,
  transition budget, and mode lock before calling an opportunity profitable.
- Treat realised savings as an estimate and ignore incomplete ledger rows for
  quantified conclusions.
- Do not propose changing emergency or arbitrage reserve without stating the
  resilience trade-off. Do not recommend a setting change from one unusual day.
- Recommendations are advisory only; never provide a Home Assistant service
  call or direct battery command.

Return:
1. Verdict: performing well, mixed, or materially improvable, with confidence.
2. A plan-adherence table: planned action/movement, observed movement, duration,
   evidence, and likely explanation for every repeated mismatch.
3. An economic scorecard: expected versus realised savings, charge/discharge
   energy, equivalent cycles, savings per discharged kWh, and any estimated
   avoidable cost with stated assumptions.
4. Forecast and load-model findings: coverage, MAE, bias, external-forecast
   uncertainty adequacy, and data-quality gaps.
5. Up to five ranked improvements. For each give evidence with timestamps,
   category, estimated impact, confidence, a reversible change if justified,
   and a validation period of comparable tariff days.
6. A no-change-recommended section for settings that the data supports.
7. Safety or data-quality conditions that invalidate economic conclusions.

Evidence follows.
```

## Validate a proposed improvement

Change one policy number at a time, preserve the before/after extracts, and
compare similar tariff and load days. Review expected and realised savings,
throughput, equivalent cycles, forecast error, plan execution, and any safety
events together. Revert the change if the expected benefit does not recur or
if it increases cycling without a proportionate, measured gain.
