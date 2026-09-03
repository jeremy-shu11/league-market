from __future__ import annotations

import os
import sys
import json
from copy import deepcopy
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path("/private/tmp/league-market-e2e.sqlite")

for suffix in ("", "-shm", "-wal"):
    DB_PATH.with_name(f"{DB_PATH.name}{suffix}").unlink(missing_ok=True)

os.environ.update(
    {
        "LEAGUE_MARKET_DB": str(DB_PATH),
        "LEAGUE_MARKET_HOST": "127.0.0.1",
        "LEAGUE_MARKET_PORT": "5066",
        "LEAGUE_MARKET_ENV": "test",
        "LEAGUE_MARKET_INVITE_CODE": "theleague",
        "LEAGUE_MARKET_ADMIN_CODE": "commissioner",
    }
)
sys.path.insert(0, str(PROJECT_DIR / "backend"))

import app as market_app  # noqa: E402
import uvicorn  # noqa: E402


FIXTURE = json.loads((PROJECT_DIR / "backend" / "fixtures" / "sleeper_sample.json").read_text(encoding="utf-8"))
FIXTURE["provenance"] = "e2e_fixture"
FIXTURE["league"]["league_id"] = os.environ["LEAGUE_MARKET_LEAGUE_ID"] if "LEAGUE_MARKET_LEAGUE_ID" in os.environ else market_app.DEFAULT_LEAGUE_ID
FIXTURE["league"]["season"] = "2026"
FIXTURE["league"]["roster_positions"] = ["QB", "RB", "RB", "WR", "WR", "TE"]
FIXTURE["league"]["scoring_settings"] = {"rec": 1.0, "rush_yd": 0.1, "rec_yd": 0.1, "pass_yd": 0.04}
FIXTURE["league"].setdefault("settings", {}).update({"leg": 1, "playoff_week_start": 15, "playoff_teams": 6})
PROJECTIONS = {
    str(player_id): {
        "pts_ppr": 8.0 + (index % 17) * 0.7,
        "rec": 2.0 + (index % 5),
        "rush_yd": 25.0 + (index % 11) * 3,
        "rec_yd": 30.0 + (index % 13) * 2,
        "pass_yd": 180.0 + (index % 9) * 8,
    }
    for index, player_id in enumerate(FIXTURE["players"])
}
market_app.MODEL_SIMULATIONS = 300
market_app.RAW_DATA_DIR = Path("/private/tmp/league-market-e2e-raw")
market_app.SleeperAdapter.build_snapshot = staticmethod(lambda league_id: deepcopy(FIXTURE))
market_app.SleeperAdapter.get_projections = staticmethod(lambda season, week: deepcopy(PROJECTIONS))


if __name__ == "__main__":
    market_app.configure_database()
    market_app.execute_schema()
    uvicorn.run(market_app.app, host="127.0.0.1", port=5066)
