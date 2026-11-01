"""Compare captured battery policies locally without contacting Home Assistant."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.replay import ReplayHistory
from house_battery.replay_model import evaluate_policies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Saved house_battery.export_data JSON response")
    parser.add_argument("--settlement-price", type=float, required=True,
                        help="Common all-in replacement price in DKK/kWh for final inventory")
    args = parser.parse_args()
    try:
        payload = json.loads(args.evidence.read_text())
        if not isinstance(payload, dict):
            raise TypeError("Evidence must be a JSON object")
        history = ReplayHistory.from_dict(payload.get("replay", payload.get("replay_history", payload)))
        if history.summary()["snapshot_count"] == 0:
            raise ValueError("No valid issued replay inputs in this export; collect new evidence first")
        result = evaluate_policies(history, settlement_price_dkk_per_kwh=args.settlement_price)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
