# Battery-arbitrage economics validation

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
- It accounts for SOC bounds, power caps, minimum mode duration, transition
  budget, switching penalty, and forecast uncertainty. These all prevent
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
