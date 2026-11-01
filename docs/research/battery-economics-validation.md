# Battery-arbitrage economics validation

## Current comparison with other integrations — 2026-11-01

Implementation follow-up: quarter-hour load integration, scheduled-background
separation, shared topology-aware discharge feasibility, strict known-price
attribution, issued forecast scoring, bounded replay capture/evaluation and
efficiency-window context are now implemented. See [optimization](../optimization.md)
and [offline evaluation](../llm-evaluation.md#offline-policy-replay). Physical
charging semantics and richer standby/efficiency/taper models remain explicitly
measurement-gated; no guessed parameters were enabled. The priorities below
retain the rationale from the comparison.

This review compares the working-tree implementation with current first-party
project documentation and selected source. It is a capability review, not a
hardware trial or evidence that another controller would save more money here.
Upstream links follow current documentation/main branches; installed releases
may differ. Recommendations below are our assessment of the evidence.

The September review retained below is historical. In particular, its universal
cheapest-price discharge restriction has been removed; exports are now measured
when a suitable meter is bound and are **not credited at the import price**;
terminal credit and bill-savings components are exposed separately. Those older
observations must not be interpreted as outstanding defects.

### Comparable projects

| Project | Approach and useful comparison | Gap relative to House Battery |
| --- | --- | --- |
| [Predbat](https://springfall2008.github.io/batpred/) | Simulation and charge/export-window optimization; PV, EV and hot-water coordination; several inverter integrations. | More detailed device power curves, temporary weather-related reserves and alternative PV/load scenarios. See [device curves and weather alerts](https://springfall2008.github.io/batpred/apps-yaml/) and [scenario settings](https://springfall2008.github.io/batpred/customisation/). |
| [EMHASS](https://emhass.readthedocs.io/en/latest/) | Linear-programming energy management with rolling-horizon control; jointly schedules battery, PV and deferrable loads. | Grid limits, demand-charge costs, optional inverter transfer curves and thermal-load models. [Configuration](https://emhass.readthedocs.io/en/latest/config.html), [EV example](https://emhass.readthedocs.io/en/latest/study_cases/ev.html). |
| [ChargeIQ / BESS Manager](https://github.com/johanzander/bess-manager) | Particularly close comparison: dynamic programming, solar and consumption forecasts, wear costs and continuous replanning. | Documents fuse-aware charging, timed grid-import caps, EV discharge inhibition, entity discovery and integrated forecast/scenario dashboards. [Feature list](https://github.com/johanzander/bess-manager#features), [user guide](https://github.com/johanzander/bess-manager/blob/main/docs/USER_GUIDE.md). |
| [batcontrol](https://github.com/MaStr/batcontrol) | Forecast-based battery control with PV-aware charging, Fronius support and an MQTT bridge. | Preserves solar headroom using charge-rate limits and coordinates with evcc. Its [PV peak shaving](https://mastr.github.io/batcontrol/features/peak-shaving/) is distinct from shaving grid-import demand peaks; [evcc coordination](https://mastr.github.io/batcontrol/integrations/evcc-connection/). |
| [Emaldo Battery Optimizer](https://github.com/wertigpar/ha-battery-optimizer) | Device-specific greedy self-consumption scheduling with separate PV-sale decisions. | Explicit idle-drain modelling, recurring rules and SOC-divergence triggers. [Design](https://github.com/wertigpar/ha-battery-optimizer/blob/main/OPTIMIZER_DESIGN.md), [triggers](https://github.com/wertigpar/ha-battery-optimizer#automatic-triggers). Its device-specific idle draw is not an FBP1200 estimate. |
| [AECC Battery Local](https://github.com/StekkerDeal/aecc-battery-local) | Closest hardware/protocol comparison rather than an economic optimizer; currently lists Fossibot FBP1200 as community-confirmed. | Optional Modbus temperature/fault/lifetime telemetry and richer per-unit entities. These require compatibility verification; they are not evidence that higher-power settings on other AECC devices are valid for ours. |

### What we already cover

Our [planner](../../custom_components/house_battery/planner.py) uses deterministic
dynamic programming with SOC and power limits, conversion losses, degradation,
switching costs, a profit hurdle, variable price intervals, reserve recovery,
partial SOC targets and terminal energy value. Minute-level replanning already
provides a rolling-horizon control pattern. Calling another project MPC, LP or
AI does not by itself establish an improvement over that policy.

We also have commissioning/shadow operation, command verification, invalid-input
gates, forecast-price provenance and scoring, qualified meter-based efficiency
learning, realized accounting, evidence export and historical capacity-sizing
replay. These should remain foundations. See [architecture](../architecture.md),
[optimizer guide](../optimization.md), and
[exhaustive planner checks](../../tests/test_house_battery_planner_oracle.py).

### Recommended priorities for our current battery

1. **Reconcile planning assumptions with executable power.** The
   [planner transition](../../custom_components/house_battery/planner.py)
   subtracts predicted connected load from the configured charging limit.
   The [direct actuator](../../custom_components/house_battery/actuator.py)
   sends the full configured charge setpoint. Discharge planning caps delivery
   at predicted load, while the actuator subtracts a 50 W margin from observed
   load. These are confirmed code differences; their physical effect needs a
   measured check of AC-input, battery-side and backup-output power semantics.
   Publish device capabilities once and use the same feasible-flow model for
   planning and execution. Retain the execution safety margin. Validate predicted
   SOC against actual SOC at several stable loads before changing the model.

2. **Measure and model standby and power-dependent losses.**
   [PlannerSettings](../../custom_components/house_battery/models.py) has one
   round-trip efficiency; the planner splits it symmetrically with a square
   root. It has no explicit idle-energy transition or SOC/temperature power
   curves. [Efficiency learning](../../custom_components/house_battery/efficiency.py)
   subtracts an idle baseline, which does not model future standby consumption.
   First measure grid and battery overhead separately in Grid, Charge and
   Battery modes, then fit simple load bands and taper limits. Avoid counting
   overhead both in measured efficiency and in an added standby term.
   [Predbat curves](https://springfall2008.github.io/batpred/apps-yaml/#battery-chargedischarge-curves),
   [EMHASS inverter curves](https://emhass.readthedocs.io/en/latest/config.html#hybrid-inverter-power-curves)
   and [Emaldo idle modelling](https://github.com/wertigpar/ha-battery-optimizer/blob/main/OPTIMIZER_DESIGN.md)
   provide useful designs, not transferable parameter values.

3. **Score issued load forecasts and incorporate their uncertainty.**
   [LoadLearner](../../custom_components/house_battery/learning.py) blends recent
   load with weekly quarter-hour profiles. Its MAE compares a prediction made
   at observation time with that observation; it does not preserve issued
   6-, 12- or 24-hour load predictions for later scoring. Persist forecast
   vintages, score errors by lead time, and compare normal/high/low load cases
   before adding a risk policy. Bucket variance exists but is not a planner
   uncertainty input. Our price-forecast buffer already solves a different
   uncertainty problem. [Predbat scenario settings](https://springfall2008.github.io/batpred/customisation/)
   are a useful reference. EMHASS's
   [chance-constrained MPC study](https://emhass.readthedocs.io/en/latest/study_cases/chance_constrained_mpc.html)
   is explicitly a standalone research prototype, not a released controller
   capability to list as production parity.

4. **Current tariff completeness confirmed; check forecast compatibility.**
   The user confirmed on 2026-11-01 that the current prices already include VAT,
   tariffs, spot energy and the other applicable components. Treat these as the
   all-in marginal import cost: tariff composition is not missing for this
   installation, and no additional fees should be added. The
   [price parser](../../custom_components/house_battery/price.py) can consume
   those supplied prices directly. The remaining investigation is whether
   external forecast entities use the same price basis, currency and units;
   their compatibility has not yet been established by this clarification.

5. **Add temporary reserve targets and device-health context.** A request such
   as retain 70% until tomorrow morning would support anticipated outages while
   preserving existing hard SOC bounds. Predbat has a
   [weather-alert reserve mechanism](https://springfall2008.github.io/batpred/apps-yaml/#weather-alert-system).
   Our confirmed-outage emergency floor is already present, but anticipating
   the event is different. Optionally probe read-only identity, temperature and
   fault capabilities using verified firmware fixtures; AECC's
   [optional Modbus telemetry](https://github.com/StekkerDeal/aecc-battery-local#features)
   is a reference. Do not make unverified Modbus support a prerequisite.

6. **Extend replay from capacity sizing to policy evaluation.** We already
   replay capacity alternatives; add comparisons of today's policy, no battery,
   a simple tariff rule and candidate loss/uncertainty policies. Use identical
   starting SOC and equal ending SOC or explicit inventory valuation. Report
   bill cost, wear, throughput, reserve shortfalls, model error and net benefit.
   Preserve the forecasts actually available at each decision to avoid hindsight
   leakage. This would tell us which additions improve this installation.

### Conditional expansion features

- **PV and export economics:** add separate PV forecasts and net export rates,
  account for solar headroom and curtailment, and use a solar-without-battery
  counterfactual when attributing battery savings. We currently expose export
  but deliberately assign no export revenue in
  [accounting](../../custom_components/house_battery/accounting.py). This is a
  scope extension for solar/export installations, not a reason to enable export
  on the present controller. Predbat, EMHASS and batcontrol provide references.
- **Flexible loads and EVs:** our
  [scheduled-load service](../../custom_components/house_battery/services.yaml)
  adds demand at fixed start/end times. It does not select appliance start times
  or jointly optimize a required kWh before a deadline. EMHASS demonstrates that
  model; its EV guide explicitly notes limitations. A smaller first step is an
  EV-active discharge inhibit, if an EV is in the battery-served load scope.
- **Site/phase import limits and demand charges:** model the installation limit
  separately from inverter limits and use live grid/phase feedback. EMHASS
  documents bounds and peak-cost models; ChargeIQ documents fuse and timed
  import limits. A monthly peak charge needs billing-period state, not merely
  a per-kWh surcharge. Relevance depends on the actual installation and tariff.
- **Battery aging beyond throughput:** our manual wear-per-kWh cost does not
  model calendar aging or time spent at high SOC. Consider a warranty-based wear
  calculator first, then validate whether an extra high-SOC holding cost is
  warranted. This is our modelling recommendation, not a claim of parity with
  a specific comparator.

For the existing FBP1200 installation, prioritize feasible-power reconciliation,
loss measurements, forecast evidence and replay before expanding into a general
whole-home controller. No runtime or hardware settings were changed by this
review.

## Historical economics review — 2026-09-21

Research date: 2026-09-21. This is a source and code review of the current
strategy; it is not a claim about a particular household's tariff or battery
warranty.

## Verdict

The dynamic-programming strategy is a sound, deliberately conservative
*energy-arbitrage* policy if every configured price row is the all-in marginal
cost of importing one kWh at that time, the battery is used for on-site load,
and exports and demand charges are immaterial. It is not yet a full
electricity-bill optimizer.

The decisive break-even rule for grid-charged energy is:

```text
discharge price > charge price / round-trip efficiency + wear + required margin
```

This is the right marginal comparison. A historical purchase price is sunk
and should be kept for P&L/reporting rather than automatically used as the
next dispatch threshold. However, the current implementation's use of the
single cheapest price anywhere in the horizon as a hard discharge floor can
be too conservative for initial battery energy or PV-filled energy. It may
hold energy through a profitable moderate-price interval when that later cheap
slot is not a feasible or relevant replacement opportunity.

## What the implementation includes

- The planner charges grid energy at the price of its own interval, applies the
  square root of configured round-trip efficiency on each leg, and adds wear
  and a policy profit margin for discharge
  ([planner](../../custom_components/house_battery/planner.py)). The default
  85% efficiency is close to NREL's representative 86% figure for Li-ion
  systems ([NREL ATB](https://atb.nrel.gov/electricity/2021/utility-scale_battery_storage)).
- It accounts for SOC bounds, power caps, a fixed internal switching penalty,
  and forecast uncertainty. These all prevent
  uneconomic chattering.
- The wear setting is applied per delivered kWh in the planner. The ledger
  applies it per measured discharge kWh, so users should calibrate it
  conservatively and treat its unit/basis as an explicit operating assumption.
- Fixed subscriptions and other constant fees should not affect an interval
  dispatch decision: they are sunk for that decision, though they belong in
  investment ROI.

## Important gaps and conditions

1. **Price input semantics are critical.** `PriceSlot` has one price field and
   the planner multiplies grid import by it. The configured source must include
   spot energy, supplier markup, time-varying DSO/transmission tariffs,
   electricity tax and VAT where applicable. A spot-only feed can make a
   seemingly profitable cycle increase the bill. In Denmark these components
   can be material and time dependent; see Energinet's
   [2026 tariff catalogue](https://energinet.dk/media/ikylsqle/energinets_tarifkatalog_2026.pdf)
   and the applicable local DSO's price sheet. Do not add a component twice if
   the selected price entity already includes it.
2. **Export/PV economics are not modeled.** The planner currently uses no PV
   forecast, and the evidence collector supplies zero export. The ledger also
   values export at the import price. Therefore its savings figure is not
   bill-grade where exports occur or where export credit differs from the
   import rate.
3. **Peak/demand charges are absent.** They cannot be represented by a single
   DKK/kWh price. If the tariff has a monthly peak component, grid charging
   needs a demand-charge state model. Fixed fees remain outside dispatch.
4. **Wear is manual.** `cycle_life` supports health/degradation reporting, not
   the dispatch cost. Derive a conservative output-kWh wear allowance from
   incremental replacement/service cost divided by warranted equivalent full
   cycles times usable battery capacity at the warranty depth of discharge.
   Keep the installed battery's actual warranty, temperature, power and DoD
   limits as the authority. DOE's LCOS framework includes replacement,
   augmentation, O&M and charging energy
   ([DOE 2022 report](https://www.energy.gov/cmei/2022-grid-energy-storage-technology-cost-and-performance-assessment));
   its storage characterization report notes cycle life/temperature dependence
   ([DOE report](https://www.energy.gov/sites/default/files/2019/07/f65/Storage%20Cost%20and%20Performance%20Characterization%20Report_Final.pdf)).
5. **Reported plan savings omit terminal stored-energy value** even though the
   planner uses a terminal value to choose the final state. Show the terminal
   asset value separately or report the optimized objective as well, so plan
   reporting and decision logic can be reconciled.

## Recommended priority order

1. Require and clearly label an **all-in marginal import price** input, or
   compose it from dated tariff/tax components. Apply equivalent components to
   forecasts.
2. Add separate net export value and real export-meter inputs; use them for PV
   self-consumption and ledger accounting. State explicitly if zero export is
   the intended operating scope.
3. Make initial stored-energy valuation explicit: preserve the present
   replacement-value policy as a safe default, and test/optionally support a
   persisted cost-basis or SOC-shadow-value policy. Do not blindly substitute
   historical price for marginal opportunity cost.
4. Add a recommended degradation-cost calculator/range based on warranty EFC,
   usable capacity, replacement cost and depth of discharge; retain a user
   override.
5. Add optional demand-charge modelling for tariffs that have it, then validate
   one representative billing month against the retailer/DSO invoice: interval
   imports at the all-in rate, exports at their net credit, measured efficiency,
   throughput and wear.

## Boundary for current use

Use the current optimizer in shadow mode first and compare matching
control-on/control-off tariff periods. It is appropriate for conservative,
zero-export, known-load grid arbitrage when the configured price is genuinely
all-in. Do not treat its realized-savings total as a complete bill or ROI
measure until export and tariff components are represented.
