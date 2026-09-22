# Root Cause Analysis: Battery Optimizer Instability

## Problem
The battery optimization plan changes frequently, lacks stability, and sometimes suggests incorrect actions.

## Root Causes Found

### 1. CRITICAL BUG: Date Parsing Error (FIXED)
The price data parser incorrectly assigned `day=23` to the end of the first interval ("00:00-00:15") because the logic checked `eh==0 and em>0` to trigger midnight rollover. This caused ALL slots to end on Sep 23 instead of Sep 22.

**Symptom**: Only 1 slot passed through `_future_slots()` (contiguity check broke immediately). The optimizer saw only 1 price interval and computed terminal_price=2.11.

**Result**: Zero savings reported, grid-only plans.

**Fix**: Change date rollover logic to only add a day when end time ≤ start time:
```python
e = dt.replace(day=base_day, hour=eh, minute=em)
if e <= s: e += timedelta(days=1)
```

### 2. DISCHARGE FLOOR INSTABILITY (KEY ISSUE)
The discharge price floor is computed from the CURRENT (first) slot's price:
```
floor = price[0] / η + degradation + min_profit
floor = price[0] / 0.85 + 0.35 + 0.75
```

As the day progresses, `price[0]` shifts → the floor shifts → the set of "profitable discharge intervals" changes.

| Time | price[0] | Floor | Intervals Qualify |
|------|----------|-------|-------------------|
| 00:00 | 2.11 | 3.58 | 11/96 |
| 05:00 | 1.85 | 3.28 | 13/76 |
| 10:00 | 2.10 | 3.57 | 11/71 |
| 12:00 | 1.58 | 2.96 | 16/48 |
| 14:00 | 1.49 | 2.85 | 17/40 |
| 18:00 | 3.06 | 4.70 | 7/24 |
| 19:00 | 5.99 | 8.14 | **0/20** |

**Result**: The optimizer's view of "what's profitable" changes wildly every replan.

**Fix**: Use the cheapest available charging price over the entire horizon:
```
floor = min(future_prices) / η + degradation + min_profit
```
This gives a stable floor of 2.81 DKK/kWh (based on 1.45 at 13:45).

### 3. REPORTED SAVINGS MISLEADING
The optimizer minimizes `optimization_cost - terminal_value`, but the reported savings only include `baseline_cost - optimization_cost` (without terminal value).

For SOC=80%:
- Reported savings: **-0.63 DKK**
- Terminal value of stored energy: **+6.09 DKK**
- True economic value: **+5.46 DKK**

**Result**: The optimizer makes profitable decisions but reports negative savings.

**Fix**: Include terminal value in reported savings:
```
savings = baseline_cost - (optimization_cost - terminal_value)
```

### 4. TERMINAL PRICE OVERSTATED
The terminal price (6.38 DKK/kWh) is computed from today's tail prices (last 48 intervals). Since today's tail includes evening peak prices (up to 6.77), the terminal price overstates the value of stored energy.

The terminal price should represent tomorrow's expected prices, not today's remaining prices.

**Fix**: Use tomorrow's forecast prices. If unavailable, use a conservative value (e.g., today's median price ~2.30).

### 5. CHARGING AT EXPENSIVE HOURS
The optimizer charges at 23:15-23:45 (prices 2.30-2.37) instead of 13:45-14:00 (price 1.45). This is because the terminal value doesn't differentiate between charging at cheap vs. expensive prices — it only cares about the final SOC.

**Fix**: The DP should evaluate the full round-trip arbitrage: charge cost vs. discharge revenue, not just terminal SOC value.

## Summary

The optimizer IS working (it does find profitable discharge windows at peak hours), but:
1. A parsing bug made it see only 1 price interval
2. The discharge floor shifts with every replan, causing instability
3. Reported savings don't include terminal value, making plans look worse than they are
4. The terminal price overstates stored energy value

## Recommended Fixes (Priority Order)

1. **Fix date parsing** (date rollover for midnight)
2. **Stabilize discharge floor** using min(future_prices) instead of price[0]
3. **Include terminal value in reported savings**
4. **Improve terminal price** using tomorrow's forecast instead of today's tail
5. **Allow daytime charging** for round-trip arbitrage
