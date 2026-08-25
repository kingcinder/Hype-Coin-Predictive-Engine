from __future__ import annotations

import argparse
import json
from datetime import datetime

from forecast.engine import ForecastEngine
from storage.database import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare full forecast training with velocity features masked"
    )
    parser.add_argument(
        "--decision-ts",
        help="ISO decision cutoff (default: current UTC time)",
    )
    args = parser.parse_args()
    decision_ts = (
        datetime.fromisoformat(args.decision_ts.replace("Z", "+00:00"))
        if args.decision_ts
        else None
    )
    with SessionLocal() as session:
        result = ForecastEngine().run_velocity_ab_experiment(session, decision_ts=decision_ts)
        session.commit()
    print(json.dumps(result, default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
