import csv
import io
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


class SleeperAdapter:
    BASE_URL = "https://api.sleeper.app/v1"

    @staticmethod
    def request_json(url: str) -> Any:
        with urllib.request.urlopen(url, timeout=40) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def get_league(cls, league_id: str) -> dict:
        return cls.request_json(f"{cls.BASE_URL}/league/{league_id}")

    @classmethod
    def get_rosters(cls, league_id: str) -> list[dict]:
        return cls.request_json(f"{cls.BASE_URL}/league/{league_id}/rosters")

    @classmethod
    def get_users(cls, league_id: str) -> list[dict]:
        return cls.request_json(f"{cls.BASE_URL}/league/{league_id}/users")

    @classmethod
    def get_players(cls) -> dict:
        return cls.request_json(f"{cls.BASE_URL}/players/nfl")

    @classmethod
    def get_state(cls) -> dict:
        return cls.request_json(f"{cls.BASE_URL}/state/nfl")

    @classmethod
    def get_projections(cls, season: str, week: int) -> dict:
        payload = cls.request_json(f"{cls.BASE_URL}/projections/nfl/regular/{season}/{week}")
        if not isinstance(payload, dict):
            raise ValueError("Sleeper projections returned an unexpected schema")
        return payload

    @classmethod
    def get_matchups(cls, league_id: str, week: int) -> list[dict]:
        return cls.request_json(f"{cls.BASE_URL}/league/{league_id}/matchups/{week}")

    @classmethod
    def get_transactions(cls, league_id: str, round_number: int) -> list[dict]:
        return cls.request_json(f"{cls.BASE_URL}/league/{league_id}/transactions/{round_number}")

    @classmethod
    def get_winners_bracket(cls, league_id: str) -> list[dict]:
        return cls.request_json(f"{cls.BASE_URL}/league/{league_id}/winners_bracket")

    @classmethod
    def build_snapshot(cls, league_id: str, through_week: int = 17) -> dict:
        league = cls.get_league(league_id)
        rosters = cls.get_rosters(league_id)
        users = cls.get_users(league_id)
        players = cls.get_players()
        weeks = {}
        for week in range(1, through_week + 1):
            try:
                weeks[str(week)] = cls.get_matchups(league_id, week)
            except Exception:
                weeks[str(week)] = []
        try:
            bracket = cls.get_winners_bracket(league_id)
        except Exception:
            bracket = []
        return {
            "source": "sleeper",
            "provenance": "live",
            "league": league,
            "rosters": rosters,
            "users": users,
            "players": players,
            "matchups": weeks,
            "winners_bracket": bracket,
        }


class NflverseRankingsAdapter:
    """Defensive FantasyPros rankings fallback distributed by dynastyprocess/nflverse tooling."""

    RANKINGS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/fp_latest_weekly.csv"
    PLAYER_IDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"

    @staticmethod
    def request_text(url: str) -> str:
        with urllib.request.urlopen(url, timeout=40) as response:
            return response.read().decode("utf-8")

    @classmethod
    def parse_projections(cls, rankings_csv: str, player_ids_csv: str) -> tuple[dict[str, dict], str]:
        id_map = {
            row["fantasypros_id"]: row["sleeper_id"]
            for row in csv.DictReader(io.StringIO(player_ids_csv))
            if row.get("fantasypros_id") not in {None, "", "NA"}
            and row.get("sleeper_id") not in {None, "", "NA"}
        }
        projections: dict[str, dict] = {}
        updated_dates = []
        for row in csv.DictReader(io.StringIO(rankings_csv)):
            sleeper_id = id_map.get(str(row.get("fantasypros_id") or ""))
            try:
                points = float(row.get("r2p_pts") or 0)
            except (TypeError, ValueError):
                points = 0.0
            scrape_date = str(row.get("scrape_date") or "")
            if scrape_date:
                updated_dates.append(scrape_date)
            if not sleeper_id or points <= 0:
                continue
            projections[str(sleeper_id)] = {
                "pts_ppr": points,
                "fallback_rank": float(row["rank"]) if str(row.get("rank") or "").replace(".", "", 1).isdigit() else 0,
            }
        if not projections or not updated_dates:
            raise ValueError("nflverse rankings did not contain mapped point projections")
        source_updated = max(updated_dates)
        updated_at = datetime.fromisoformat(source_updated).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated_at > timedelta(hours=24):
            raise ValueError(f"nflverse rankings are stale (updated {source_updated})")
        return projections, updated_at.isoformat()

    @classmethod
    def get_projections(cls) -> tuple[dict[str, dict], str]:
        return cls.parse_projections(
            cls.request_text(cls.RANKINGS_URL),
            cls.request_text(cls.PLAYER_IDS_URL),
        )
