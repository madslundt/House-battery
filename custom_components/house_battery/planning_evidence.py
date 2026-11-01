"""Connect issued forecasts and offline replay evidence without affecting dispatch."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from .models import Action, PlannedSlot, PlannerSettings, PriceSlot
from .runtime import RuntimeState

_LOGGER = logging.getLogger(__name__)


def capture_evidence(operation: Callable[[], bool]) -> tuple[bool, str | None]:
    """Optional analytics failures must never interrupt the live control loop."""
    try:
        return operation(), None
    except Exception as exc:  # Analytics is an independent, fail-open observer.
        _LOGGER.exception("Optional battery planning evidence capture failed")
        return False, f"{type(exc).__name__}: {exc}"[:240]


def evidence_scope(config: dict[str, Any], *, direct: bool, time_zone: str) -> str:
    """Version source semantics without archiving device addresses in replay."""
    return sha256(repr((
        "issued_load_v1", direct, time_zone,
        config.get("host") if direct else config.get("load_power_entity"),
        config.get("port") if direct else None,
        config.get("grid_isolation_switch_entity"),
        tuple(config.get("price_entities", [])),
        tuple(config.get("price_forecast_entities", [])),
        config.get("price_forecast_entity"), "all_in_DKK_per_kWh",
    )).encode()).hexdigest()


def observe_actual_evidence(
    runtime: RuntimeState, now: datetime, *, load_w: float | None,
    price: float | None, scope: str,
) -> bool:
    """Observe actual total demand even while incremental-load learning pauses."""
    changed = runtime.load_forecast_evidence.configure(scope)
    for row in runtime.load_forecast_evidence.observe(now, load_w, price):
        runtime.replay_history.record_actual({**row, "scope": scope})
        changed = True
    return changed


def record_plan_evidence(
    runtime: RuntimeState, now: datetime, *, slots: Sequence[PriceSlot],
    settings: PlannerSettings, soc: float, current_action: Action, scope: str,
    forecast_load: Callable[[datetime, datetime], tuple[float, float] | None],
    committed_slot: PlannedSlot | None = None,
    terminal_price_floor_dkk_per_kwh: float = 0.0,
    current_discharge_enabled: bool = True,
) -> bool:
    """Keep issued inputs and canonical load predictions, with bounded horizons."""
    if not slots:
        return False
    changed = runtime.replay_history.record(
        now, slots=slots, settings=settings, soc=soc, current_action=current_action,
        scope=scope, committed_slot=committed_slot,
        terminal_price_floor_dkk_per_kwh=terminal_price_floor_dkk_per_kwh,
        current_discharge_enabled=current_discharge_enabled,
    )
    now = now.astimezone(UTC)
    cursor = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    if cursor < now:
        cursor += timedelta(minutes=15)
    horizon = min(now + timedelta(hours=72), max(slot.end for slot in slots))
    predictions = []
    while cursor + timedelta(minutes=15) <= horizon:
        end = cursor + timedelta(minutes=15)
        load = forecast_load(cursor, end)
        if load is not None:
            baseline, scheduled = load
            predictions.append({"start": cursor.isoformat(), "end": end.isoformat(),
                                "baseline_wh": baseline, "scheduled_wh": scheduled})
        cursor = end
    return runtime.load_forecast_evidence.record_forecasts(now, predictions, scope) or changed
