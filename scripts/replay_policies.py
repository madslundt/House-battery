"""Compare captured battery policies locally without contacting Home Assistant."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.replay import ReplayHistory, timestamp
from house_battery.replay_model import evaluate_policies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Saved house_battery.export_data JSON response, compact or expanded")
    parser.add_argument("--days", type=int, default=28, metavar="1–28",
                        help="Days ending at the last measured interval (default: 28)")
    parser.add_argument("--settlement-price", type=float, required=True,
                        help="Common all-in replacement price in DKK/kWh for final inventory")
    args = parser.parse_args()
    if not 1 <= args.days <= 28:
        parser.error("--days must be between 1 and 28")
    try:
        payload = json.loads(args.evidence.read_text())
        if not isinstance(payload, dict):
            raise TypeError("Evidence must be a JSON object")
        if isinstance(payload.get("data"), dict) and "replay_history" in payload["data"]:
            payload = payload["data"]
        history = ReplayHistory.from_dict(payload.get("replay", payload.get("replay_history", payload)))
        summary = history.summary()
        if summary["snapshot_count"] == 0:
            raise ValueError("No valid issued replay inputs in this export; collect new evidence first")
        last_actual = summary["last_actual_end"]
        result = evaluate_policies(
            history, settlement_price_dkk_per_kwh=args.settlement_price,
            since=timestamp(last_actual) - timedelta(days=args.days) if last_actual else None,
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
