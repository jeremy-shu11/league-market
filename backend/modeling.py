from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Iterable


MODEL_VERSION = "league-strength-v1"
DEFAULT_SIMULATIONS = 20_000
FLEX_POSITIONS = {"RB", "WR", "TE"}
SUPER_FLEX_POSITIONS = {"QB", "RB", "WR", "TE"}
ZERO_EXPECTATION_CATEGORIES = {"fum_rec", "fum_rec_td", "st_fum_rec", "def_st_fum_rec"}


@dataclass(frozen=True)
class TeamStrength:
    roster_id: int
    mean: float
    stdev: float
    projected_starters: int
    starter_slots: int


def projection_points(stats: dict, scoring_settings: dict) -> float:
    """Score projected stat components with league settings, with a PPR fallback."""
    total = 0.0
    matched = 0
    for category, multiplier in scoring_settings.items():
        value = stats.get(category)
        if isinstance(value, (int, float)) and isinstance(multiplier, (int, float)):
            total += float(value) * float(multiplier)
            matched += 1
    if matched and total > 0:
        return total
    return float(stats.get("pts_ppr") or stats.get("pts_half_ppr") or stats.get("pts_std") or 0.0)


def position_eligible(position: str, slot: str) -> bool:
    position = (position or "").upper()
    slot = slot.upper()
    if slot == "FLEX":
        return position in FLEX_POSITIONS
    if slot == "SUPER_FLEX":
        return position in SUPER_FLEX_POSITIONS
    return position == slot


def optimize_lineup(players: list[dict], slots: list[str]) -> list[dict]:
    """Maximum projected lineup via a player-by-slot bitmask assignment."""
    active_slots = [slot for slot in slots if slot not in {"BN", "IR", "TAXI"}]
    if not active_slots:
        return []
    states: dict[int, tuple[float, tuple[int, ...]]] = {0: (0.0, ())}
    for player_index, player in enumerate(players):
        points = float(player.get("projected_points") or 0.0)
        if points <= 0:
            continue
        next_states = dict(states)
        for mask, (score, chosen) in states.items():
            for slot_index, slot in enumerate(active_slots):
                bit = 1 << slot_index
                if mask & bit or not position_eligible(str(player.get("position") or ""), slot):
                    continue
                candidate = (score + points, chosen + (player_index,))
                next_mask = mask | bit
                if next_mask not in next_states or candidate[0] > next_states[next_mask][0]:
                    next_states[next_mask] = candidate
        states = next_states
    full_mask = (1 << len(active_slots)) - 1
    _, chosen = states.get(full_mask, max(states.values(), key=lambda item: (len(item[1]), item[0])))
    return [players[index] for index in chosen]


def build_team_strengths(snapshot: dict, projections: dict[str, dict]) -> tuple[dict[int, TeamStrength], dict]:
    league = snapshot.get("league") or {}
    scoring = league.get("scoring_settings") or {}
    slots = league.get("roster_positions") or []
    players = snapshot.get("players") or {}
    strengths: dict[int, TeamStrength] = {}
    rostered_count = projected_count = starter_slots = projected_starters = 0
    active_slots = {slot for slot in slots if slot not in {"BN", "IR", "TAXI"}}
    relevant_prefixes = {"fum"}
    if active_slots & {"QB", "SUPER_FLEX"}:
        relevant_prefixes.update({"pass", "rush"})
    if active_slots & {"RB", "WR", "TE", "FLEX", "SUPER_FLEX"}:
        relevant_prefixes.update({"rush", "rec"})
    if "K" in active_slots:
        relevant_prefixes.update({"fg", "xp"})
    if active_slots & {"DEF", "DST"}:
        relevant_prefixes.update({"def", "pts_allow", "sack", "int", "ff", "safe", "blk"})
    available_projection_keys = {key for stats in projections.values() for key in stats}
    relevant_scoring = [
        key
        for key, multiplier in scoring.items()
        if float(multiplier or 0) != 0 and key not in ZERO_EXPECTATION_CATEGORIES
        and any(key == prefix or key.startswith(f"{prefix}_") for prefix in relevant_prefixes)
    ]
    unsupported_scoring = sorted(key for key in relevant_scoring if key not in available_projection_keys)

    for roster in snapshot.get("rosters") or []:
        roster_players = []
        for player_id in roster.get("players") or []:
            player_id = str(player_id)
            rostered_count += 1
            stats = projections.get(player_id) or {}
            points = projection_points(stats, scoring)
            if points > 0:
                projected_count += 1
            metadata = players.get(player_id) or {}
            roster_players.append(
                {
                    "player_id": player_id,
                    "position": metadata.get("position") or "",
                    "projected_points": points,
                }
            )
        lineup = optimize_lineup(roster_players, slots)
        active_slot_count = len([slot for slot in slots if slot not in {"BN", "IR", "TAXI"}])
        covered = sum(1 for player in lineup if float(player["projected_points"]) > 0)
        lineup_mean = sum(float(player["projected_points"]) for player in lineup)
        roster_id = int(roster.get("roster_id") or 0)
        strengths[roster_id] = TeamStrength(
            roster_id=roster_id,
            mean=round(lineup_mean, 4),
            stdev=round(max(14.0, lineup_mean * 0.16), 4),
            projected_starters=covered,
            starter_slots=active_slot_count,
        )
        starter_slots += active_slot_count
        projected_starters += covered

    diagnostics = {
        "rostered_players": rostered_count,
        "projected_rostered_players": projected_count,
        "roster_coverage": projected_count / rostered_count if rostered_count else 0.0,
        "starter_slots": starter_slots,
        "projected_starters": projected_starters,
        "starter_coverage": projected_starters / starter_slots if starter_slots else 0.0,
        "unsupported_scoring_categories": unsupported_scoring,
        "zero_expectation_scoring_categories": sorted(
            key for key in scoring if key in ZERO_EXPECTATION_CATEGORIES and float(scoring[key] or 0) != 0
        ),
    }
    return strengths, diagnostics


def calibrate_team_volatility(strengths: dict[int, TeamStrength], historical_scores: Iterable[float]) -> dict[int, TeamStrength]:
    scores = [float(value) for value in historical_scores if float(value) > 0]
    if len(scores) < 12:
        return strengths
    empirical = max(12.0, pstdev(scores))
    return {
        roster_id: TeamStrength(
            roster_id=value.roster_id,
            mean=value.mean,
            stdev=round(0.6 * value.stdev + 0.4 * empirical, 4),
            projected_starters=value.projected_starters,
            starter_slots=value.starter_slots,
        )
        for roster_id, value in strengths.items()
    }


def matchup_pairs(snapshot: dict, week: int, roster_ids: list[int]) -> list[tuple[int, int]]:
    entries = (snapshot.get("matchups") or {}).get(str(week)) or []
    grouped: dict[int, list[int]] = {}
    for entry in entries:
        matchup_id = entry.get("matchup_id")
        roster_id = entry.get("roster_id")
        if matchup_id is not None and roster_id is not None:
            grouped.setdefault(int(matchup_id), []).append(int(roster_id))
    pairs = [tuple(sorted(ids[:2])) for ids in grouped.values() if len(ids) >= 2]
    if pairs:
        return sorted(set(pairs))
    ordered = sorted(roster_ids)
    return [(ordered[index], ordered[index + 1]) for index in range(0, len(ordered) - 1, 2)]


def smoothed_probabilities(counts: dict[int, int], simulations: int) -> dict[int, float]:
    outcome_count = len(counts)
    denominator = simulations + 0.5 * outcome_count
    return {key: (count + 0.5) / denominator for key, count in counts.items()}


def stable_seed(league_id: str, season: str, week: int, projection_hash: str = "") -> int:
    material = f"{league_id}:{season}:{week}:{MODEL_VERSION}:{projection_hash}"
    return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:15], 16)


def _score(rng: random.Random, strength: TeamStrength) -> float:
    return max(0.0, rng.gauss(strength.mean, strength.stdev))


def _playoff_winner(rng: random.Random, teams: list[int], strengths: dict[int, TeamStrength]) -> int:
    if len(teams) < 2:
        return teams[0]

    def winner(left: int, right: int) -> int:
        left_score = _score(rng, strengths[left])
        right_score = _score(rng, strengths[right])
        return left if (left_score, -left) >= (right_score, -right) else right

    if len(teams) >= 6:
        quarter_a = winner(teams[2], teams[5])
        quarter_b = winner(teams[3], teams[4])
        semifinalists = sorted([quarter_a, quarter_b])
        semi_a = winner(teams[0], semifinalists[-1])
        semi_b = winner(teams[1], semifinalists[0])
        return winner(semi_a, semi_b)
    remaining = list(teams)
    while len(remaining) > 1:
        next_round = []
        for index in range(0, len(remaining) - 1, 2):
            next_round.append(winner(remaining[index], remaining[index + 1]))
        if len(remaining) % 2:
            next_round.append(remaining[-1])
        remaining = next_round
    return remaining[0]


def run_league_model(
    snapshot: dict,
    projections: dict[str, dict],
    simulation_count: int = DEFAULT_SIMULATIONS,
    seed: int | None = None,
    historical_scores: Iterable[float] = (),
    projection_hash: str = "",
) -> dict:
    if simulation_count < 100:
        raise ValueError("At least 100 simulations are required")
    league = snapshot.get("league") or {}
    settings = league.get("settings") or {}
    league_id = str(league.get("league_id") or "")
    season = str(league.get("season") or "")
    current_week = max(1, int(settings.get("leg") or 1))
    playoff_week = int(settings.get("playoff_week_start") or 15)
    playoff_teams = int(settings.get("playoff_teams") or 6)
    strengths, diagnostics = build_team_strengths(snapshot, projections)
    strengths = calibrate_team_volatility(strengths, historical_scores)
    roster_ids = sorted(strengths)
    if len(roster_ids) < 2:
        raise ValueError("At least two projected rosters are required")
    if diagnostics["starter_coverage"] < 0.95:
        raise ValueError(f"Starter projection coverage is {diagnostics['starter_coverage']:.1%}; 95% required")
    if diagnostics["unsupported_scoring_categories"]:
        raise ValueError(
            "Unsupported scoring categories: " + ", ".join(diagnostics["unsupported_scoring_categories"])
        )

    resolved_seed = seed if seed is not None else stable_seed(league_id, season, current_week, projection_hash)
    rng = random.Random(resolved_seed)
    top_counts = {roster_id: 0 for roster_id in roster_ids}
    low_counts = {roster_id: 0 for roster_id in roster_ids}
    playoff_counts = {roster_id: 0 for roster_id in roster_ids}
    champion_counts = {roster_id: 0 for roster_id in roster_ids}
    roster_settings = {
        int(roster.get("roster_id") or 0): roster.get("settings") or {}
        for roster in snapshot.get("rosters") or []
    }

    for _ in range(simulation_count):
        current_scores = {roster_id: _score(rng, strengths[roster_id]) for roster_id in roster_ids}
        top_counts[max(roster_ids, key=lambda roster_id: (current_scores[roster_id], -roster_id))] += 1
        low_counts[min(roster_ids, key=lambda roster_id: (current_scores[roster_id], roster_id))] += 1

        wins = {roster_id: float(roster_settings.get(roster_id, {}).get("wins") or 0) for roster_id in roster_ids}
        points = {
            roster_id: float(roster_settings.get(roster_id, {}).get("fpts") or 0)
            + float(roster_settings.get(roster_id, {}).get("fpts_decimal") or 0) / 100
            for roster_id in roster_ids
        }
        for week in range(current_week, playoff_week):
            weekly_scores = current_scores if week == current_week else {
                roster_id: _score(rng, strengths[roster_id]) for roster_id in roster_ids
            }
            for roster_id, score in weekly_scores.items():
                points[roster_id] += score
            for left, right in matchup_pairs(snapshot, week, roster_ids):
                if weekly_scores[left] > weekly_scores[right]:
                    wins[left] += 1
                elif weekly_scores[right] > weekly_scores[left]:
                    wins[right] += 1
                else:
                    wins[left] += 0.5
                    wins[right] += 0.5
        seeded = sorted(roster_ids, key=lambda roster_id: (wins[roster_id], points[roster_id], -roster_id), reverse=True)
        qualifiers = seeded[:playoff_teams]
        for roster_id in qualifiers:
            playoff_counts[roster_id] += 1
        champion_counts[_playoff_winner(rng, qualifiers, strengths)] += 1

    probabilities = {
        "week_top": smoothed_probabilities(top_counts, simulation_count),
        "week_low": smoothed_probabilities(low_counts, simulation_count),
        "makes_playoffs": {
            key: (count + 0.5) / (simulation_count + 1.0) for key, count in playoff_counts.items()
        },
        "champion": smoothed_probabilities(champion_counts, simulation_count),
    }
    diagnostics.update(
        {
            "team_count": len(roster_ids),
            "current_week": current_week,
            "playoff_week": playoff_week,
            "playoff_teams": playoff_teams,
            "mean_projected_team_score": mean(value.mean for value in strengths.values()),
            "projection_hash": projection_hash,
        }
    )
    return {
        "model_version": MODEL_VERSION,
        "league_id": league_id,
        "season": season,
        "week": current_week,
        "seed": resolved_seed,
        "simulation_count": simulation_count,
        "team_strengths": {str(key): value.__dict__ for key, value in strengths.items()},
        "probabilities": {
            family: {str(key): value for key, value in estimates.items()}
            for family, estimates in probabilities.items()
        },
        "diagnostics": diagnostics,
        "assumptions": {
            "opening_odds_policy": "frozen_at_publish",
            "score_distribution": "nonnegative_normal",
            "simulation_smoothing": "jeffreys_0.5",
        },
    }


def model_fingerprint(result: dict) -> str:
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
