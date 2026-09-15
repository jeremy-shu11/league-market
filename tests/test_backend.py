import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import app as market_app
from market_engine import lmsr_prices, shares_for_budget, trade_cost, worst_case_subsidy
from modeling import optimize_lineup, projection_points
from sleeper import NflverseRankingsAdapter


class EngineTests(unittest.TestCase):
    def test_binary_lmsr_prices_start_even(self):
        prices = lmsr_prices([0, 0], 35)
        self.assertAlmostEqual(prices[0], 50.0)
        self.assertAlmostEqual(prices[1], 50.0)

    def test_multi_outcome_lmsr_prices_sum_to_payout(self):
        prices = lmsr_prices([0] * 12, 30)
        self.assertAlmostEqual(sum(prices), 100.0)
        self.assertAlmostEqual(prices[0], 100.0 / 12)

    def test_buying_outcome_moves_price_up(self):
        quantities = [0, 0]
        before = lmsr_prices(quantities, 35)[0]
        cost = trade_cost(quantities, 0, 10, 35)
        after = lmsr_prices([10, 0], 35)[0]
        self.assertGreater(cost, 0)
        self.assertGreater(after, before)

    def test_lmsr_uses_model_prior_and_has_bounded_subsidy(self):
        priors = [0.7, 0.3]
        prices = lmsr_prices([0, 0], 40, priors=priors)
        self.assertAlmostEqual(prices[0], 70.0)
        self.assertAlmostEqual(prices[1], 30.0)
        self.assertAlmostEqual(sum(prices), 100.0)
        self.assertGreater(worst_case_subsidy(priors, 40), 0)

    def test_lmsr_buy_sell_round_trip_conserves_cost(self):
        priors = [0.65, 0.35]
        buy_cost = trade_cost([0, 0], 0, 7, 35, priors=priors)
        sell_cost = trade_cost([7, 0], 0, -7, 35, priors=priors)
        self.assertAlmostEqual(buy_cost + sell_cost, 0.0, places=9)

    def test_budget_quote_solves_for_contracts_without_exceeding_spend(self):
        priors = [0.68, 0.32]
        shares = shares_for_budget([0, 0], 0, 100, 35, priors=priors)
        cost = trade_cost([0, 0], 0, shares, 35, priors=priors)
        after = lmsr_prices([shares, 0], 35, priors=priors)[0]
        self.assertAlmostEqual(cost, 100, places=6)
        self.assertGreater(shares, 1)
        self.assertLess((after - 68) / 100, 0.05)

    def test_nflverse_rankings_map_to_sleeper_ids_and_reject_stale_data(self):
        today = datetime.now(timezone.utc).date().isoformat()
        rankings = f"fantasypros_id,scrape_date,rank,r2p_pts\n10,{today},1,19.5\n"
        player_ids = "fantasypros_id,sleeper_id\n10,sl-10\n"
        projections, source_updated_at = NflverseRankingsAdapter.parse_projections(rankings, player_ids)
        self.assertEqual(projections["sl-10"]["pts_ppr"], 19.5)
        self.assertTrue(source_updated_at.startswith(today))
        with self.assertRaisesRegex(ValueError, "stale"):
            NflverseRankingsAdapter.parse_projections(
                "fantasypros_id,scrape_date,rank,r2p_pts\n10,2020-01-01,1,19.5\n",
                player_ids,
            )

    def test_custom_scoring_and_superflex_lineup_are_deterministic(self):
        self.assertAlmostEqual(
            projection_points({"pass_yd": 250, "pass_td": 2, "pass_int": 1}, {"pass_yd": 0.04, "pass_td": 4, "pass_int": -2}),
            16,
        )
        players = [
            {"player_id": "qb1", "position": "QB", "projected_points": 24},
            {"player_id": "qb2", "position": "QB", "projected_points": 20},
            {"player_id": "rb1", "position": "RB", "projected_points": 18},
            {"player_id": "wr1", "position": "WR", "projected_points": 17},
        ]
        lineup = optimize_lineup(players, ["QB", "FLEX", "SUPER_FLEX"])
        self.assertEqual({player["player_id"] for player in lineup}, {"qb1", "qb2", "rb1"})

    def test_live_scoring_uses_unscored_starter_projections(self):
        live = market_app.live_week_score_probabilities(
            league_id="league",
            season="2026",
            week=1,
            model_run_id=7,
            estimates={1: (100.0, 1.0), 2: (50.0, 1.0)},
            player_projections={"a": 100.0, "b": 0.0, "c": 20.0, "d": 30.0},
            matchups=[
                {
                    "roster_id": 1,
                    "points": 30.0,
                    "starters": ["a", "b"],
                    "players_points": {"a": 30.0, "b": 0.0},
                },
                {
                    "roster_id": 2,
                    "points": 20.0,
                    "starters": ["c", "d"],
                    "players_points": {"c": 20.0, "d": 0.0},
                },
            ],
            simulation_count=300,
        )
        self.assertEqual(live["projected_remaining"][1], 0.0)
        self.assertEqual(live["projected_remaining"][2], 30.0)
        self.assertEqual(live["starter_context"][1]["projection_basis"], "starter_projection")
        self.assertGreater(live["probabilities"]["week_top"][2], 0.95)


class ConfigTests(unittest.TestCase):
    def test_production_rejects_default_or_short_codes(self):
        with self.assertRaises(RuntimeError):
            market_app.validate_runtime_config("production", "theleague", "commissioner")
        with self.assertRaises(RuntimeError):
            market_app.validate_runtime_config("production", "short", "still-too-short")

    def test_production_accepts_private_codes(self):
        market_app.validate_runtime_config(
            "production",
            "invite-code-with-entropy",
            "admin-code-with-more-entropy",
        )


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        market_app.DB_PATH = Path(self.tempdir.name) / "market.sqlite"
        market_app.RAW_DATA_DIR = Path(self.tempdir.name) / "raw"
        market_app.BACKUP_DIR = Path(self.tempdir.name) / "backups"
        market_app.MODEL_SIMULATIONS = 300
        market_app.execute_schema()
        self.client = TestClient(market_app.app)

    def tearDown(self):
        self.tempdir.cleanup()

    def join(self, name="Trader", league_id="1326428061876371456"):
        response = self.client.post(
            "/api/auth/join",
            json={
                "invite_code": "theleague",
                "display_name": name,
                "sleeper_username": "",
                "league_id": league_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def admin_headers(self):
        return {"X-Admin-Code": "commissioner"}

    def unlock_admin(self, token):
        response = self.client.post(
            "/api/admin/session",
            headers={"X-Participant-Token": token},
            json={"admin_code": "commissioner"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def trade(self, headers, market_id, outcome_id, shares, side):
        quote_response = self.client.post(
            f"/api/markets/{market_id}/quote",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": shares, "side": side},
        )
        if quote_response.status_code != 200:
            return quote_response
        quote = quote_response.json()["quote"]
        guard = {"max_cost": quote["estimated_cost"] + 0.01} if side == "buy" else {
            "min_proceeds": max(0, quote["estimated_cost"] - 0.01)
        }
        return self.client.post(
            f"/api/markets/{market_id}/{side}",
            headers=headers,
            json={
                "outcome_id": outcome_id,
                "shares": shares,
                "market_revision": quote["market_revision"],
                **guard,
            },
        )

    def test_health_and_security_headers(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200, index.text)
        self.assertEqual(index.headers["cache-control"], "no-store")
        self.assertNotIn("__ASSET_VERSION__", index.text)
        if "/_nuxt/" in index.text:
            self.assertIn("league-market-nuxt-bridge", index.text)
            self.assertRegex(index.text, r'name="league-market-asset-version" content="[0-9a-f]{12}"')
        else:
            self.assertRegex(index.text, r"/static/app\.js\?v=[0-9a-f]{12}")

    def seed(self):
        payload, projections = self.modeled_fixture()
        with market_app.db() as conn:
            synced = market_app.sync_snapshot(conn, payload, "1326428061876371456")
            projection_run = market_app.ingest_projections(conn, payload, projections)
            market_app.run_model_for_snapshot(
                conn,
                payload,
                projections,
                [
                    synced["ingestion"]["league_run_id"],
                    synced["ingestion"]["players_run_id"],
                    projection_run["id"],
                ],
                simulation_count=300,
            )
        response = self.client.post("/api/admin/seed-markets", headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["created"], 0)

    def modeled_fixture(self):
        fixture = Path(__file__).resolve().parent.parent / "backend" / "fixtures" / "sleeper_sample.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["provenance"] = "test_fixture"
        payload["league"]["league_id"] = "1326428061876371456"
        payload["league"]["season"] = "2026"
        payload["league"]["roster_positions"] = ["QB", "RB", "RB", "WR", "WR", "TE"]
        payload["league"]["scoring_settings"] = {"rec": 1.0, "rush_yd": 0.1, "rec_yd": 0.1, "pass_yd": 0.04}
        payload["league"].setdefault("settings", {}).update(
            {"leg": 1, "playoff_week_start": 15, "playoff_teams": 6}
        )
        projections = {
            str(player_id): {
                "pts_ppr": 8.0 + (index % 17) * 0.7,
                "rec": 2.0 + (index % 5),
                "rush_yd": 25.0 + (index % 11) * 3,
                "rec_yd": 30.0 + (index % 13) * 2,
                "pass_yd": 180.0 + (index % 9) * 8,
            }
            for index, player_id in enumerate(payload["players"])
        }
        return payload, projections

    def test_named_invite_opens_dashboard_and_reuses_participant(self):
        created = self.client.post(
            "/api/admin/invites",
            headers=self.admin_headers(),
            json={"display_name": "Jeremy Shu", "uses_remaining": 1},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["invite"]["code"], "jeremy-shu")

        first = self.client.post("/api/auth/join", json={"invite_code": "Jeremy Shu"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["participant"]["display_name"], "Jeremy Shu")
        self.assertFalse(first.json()["returning"])

        second = self.client.post("/api/auth/join", json={"invite_code": "jeremy-shu"})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["token"], first.json()["token"])
        self.assertTrue(second.json()["returning"])

    def test_join_accepts_existing_raw_invite_codes_with_symbols(self):
        raw_code = "Legacy/Admin+Code/2026"
        with market_app.db() as conn:
            conn.execute(
                """
                INSERT INTO invite_codes (code, league_id, role, uses_remaining, display_name, created_at)
                VALUES (?, ?, 'participant', NULL, 'Legacy Manager', ?)
                """,
                (raw_code, "1326428061876371456", market_app.now_iso()),
            )

        response = self.client.post("/api/auth/join", json={"invite_code": raw_code})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["participant"]["display_name"], "Legacy Manager")

    def test_admin_can_generate_personal_codes_from_sleeper_managers(self):
        with market_app.db() as conn:
            conn.execute(
                """
                INSERT INTO league_managers
                  (league_id, user_id, username, display_name, team_name, avatar, is_owner, roster_ids_json, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, '', 1, '[1]', '{}', ?)
                """,
                ("1326428061876371456", "sleeper-1", "jshu", "Jeremy Shu", "The Desk", market_app.now_iso()),
            )

        generated = self.client.post(
            "/api/admin/invites/manager-codes",
            headers=self.admin_headers(),
            json={"league_id": "1326428061876371456"},
        )
        self.assertEqual(generated.status_code, 200, generated.text)
        self.assertEqual(generated.json()["created"][0]["code"], "jeremy-shu")
        self.assertEqual(generated.json()["created"][0]["sleeper_user_id"], "sleeper-1")

        joined = self.client.post("/api/auth/join", json={"invite_code": "jeremy-shu"})
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertEqual(joined.json()["participant"]["display_name"], "Jeremy Shu")

    def test_participant_can_change_personal_league_code(self):
        created = self.client.post(
            "/api/admin/invites",
            headers=self.admin_headers(),
            json={"display_name": "Jeremy Shu", "uses_remaining": 1},
        )
        self.assertEqual(created.status_code, 200, created.text)
        joined = self.client.post("/api/auth/join", json={"invite_code": "jeremy-shu"})
        self.assertEqual(joined.status_code, 200, joined.text)
        token = joined.json()["token"]

        updated = self.client.post(
            "/api/account/code",
            headers={"X-Participant-Token": token},
            json={"code": "J Shu"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["participant"]["invite_code"], "j-shu")

        old_code = self.client.post("/api/auth/join", json={"invite_code": "jeremy-shu"})
        self.assertEqual(old_code.status_code, 403, old_code.text)
        new_code = self.client.post("/api/auth/join", json={"invite_code": "j-shu"})
        self.assertEqual(new_code.status_code, 200, new_code.text)
        self.assertEqual(new_code.json()["token"], token)
        self.assertTrue(new_code.json()["returning"])

    def test_participant_code_change_rejects_code_used_by_another_account(self):
        first = self.join("First Trader")
        second = self.join("Second Trader")
        first_update = self.client.post(
            "/api/account/code",
            headers={"X-Participant-Token": first},
            json={"code": "first"},
        )
        self.assertEqual(first_update.status_code, 200, first_update.text)

        second_update = self.client.post(
            "/api/account/code",
            headers={"X-Participant-Token": second},
            json={"code": "first"},
        )
        self.assertEqual(second_update.status_code, 409, second_update.text)

    def test_join_seed_buy_sell_portfolio_and_leaderboard(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        markets = self.client.get("/api/markets", headers=headers).json()["markets"]
        champion = next(market for market in markets if market["title"] == "2026 League Champion")
        outcome_id = champion["outcomes"][0]["id"]

        quote = self.client.post(
            f"/api/markets/{champion['id']}/quote",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": 10, "side": "buy"},
        )
        self.assertEqual(quote.status_code, 200, quote.text)
        quoted = quote.json()["quote"]
        self.assertEqual(quoted["side"], "buy")
        self.assertEqual(quoted["outcome_id"], outcome_id)
        self.assertGreater(quoted["estimated_cost"], 0)
        self.assertGreater(quoted["price_after"], quoted["price_before"])

        buy = self.trade(headers, champion["id"], outcome_id, 10, "buy")
        self.assertEqual(buy.status_code, 200, buy.text)
        self.assertLess(buy.json()["participant"]["cash"], 10_000)
        self.assertAlmostEqual(abs(buy.json()["cash_delta"]), round(quoted["estimated_cost"], 2), places=2)

        ticker = self.client.get("/api/ticker", headers=headers)
        self.assertEqual(ticker.status_code, 200, ticker.text)
        self.assertIn("Trader BUY", ticker.json()["items"][0]["headline"])

        sell = self.trade(headers, champion["id"], outcome_id, 2, "sell")
        self.assertEqual(sell.status_code, 200, sell.text)

        portfolio = self.client.get("/api/portfolio", headers=headers).json()
        self.assertEqual(len(portfolio["positions"]), 1)
        self.assertAlmostEqual(portfolio["positions"][0]["shares"], 8)

        leaderboard = self.client.get("/api/leaderboard", headers=headers).json()["leaderboard"]
        self.assertEqual(leaderboard[0]["display_name"], "Trader")
        self.assertTrue(leaderboard[0]["is_active"])
        self.assertEqual(leaderboard[0]["trade_count"], 2)

    def test_prevents_negative_cash_and_oversell(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]

        oversell = self.client.post(
            f"/api/markets/{market['id']}/sell",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": 1},
        )
        self.assertEqual(oversell.status_code, 409)

        oversell_quote = self.client.post(
            f"/api/markets/{market['id']}/quote",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": 1, "side": "sell"},
        )
        self.assertEqual(oversell_quote.status_code, 409)

        too_big = self.client.post(
            f"/api/markets/{market['id']}/buy",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": 500},
        )
        self.assertEqual(too_big.status_code, 409)

        too_big_quote = self.client.post(
            f"/api/markets/{market['id']}/quote",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": 500, "side": "buy"},
        )
        self.assertEqual(too_big_quote.status_code, 409)

    def test_buy_quote_accepts_credit_budget_and_returns_fill_terms(self):
        token = self.join("Budget Trader")
        self.seed()
        headers = {"X-Participant-Token": token}
        markets = self.client.get("/api/markets", headers=headers).json()["markets"]
        market = next(
            item
            for item in markets
            if item["title"].endswith("Makes Playoffs")
            and 0.25 <= item["outcomes"][0]["probability"] <= 0.75
        )
        outcome = market["outcomes"][0]
        quote_response = self.client.post(
            f"/api/markets/{market['id']}/quote",
            headers=headers,
            json={"outcome_id": outcome["id"], "budget": 100, "side": "buy"},
        )
        self.assertEqual(quote_response.status_code, 200, quote_response.text)
        quote = quote_response.json()["quote"]
        self.assertAlmostEqual(quote["estimated_cost"], 100, places=2)
        self.assertAlmostEqual(quote["average_fill_price"], quote["estimated_cost"] / quote["shares"], places=3)
        self.assertEqual(quote["requested_budget"], 100)
        self.assertGreater(quote["shares"], 0)
        self.assertLess(quote["price_impact"], 0.05)
        self.assertGreaterEqual(market["liquidity"], 35)

    def test_modeled_origination_is_idempotent_and_priors_are_not_uniform(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        first = self.client.get("/api/markets", headers=headers).json()["markets"]
        champion = next(market for market in first if market["title"] == "2026 League Champion")
        priors = [outcome["prior_probability"] for outcome in champion["outcomes"]]
        self.assertAlmostEqual(sum(priors), 1.0, places=3)
        self.assertGreater(max(priors) - min(priors), 0.001)

        second = self.client.post("/api/admin/seed-markets", headers=self.admin_headers())
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["created"], 0)
        self.assertEqual(second.json()["existing"], len(first))
        after = self.client.get("/api/markets", headers=headers).json()["markets"]
        self.assertEqual([market["id"] for market in first], [market["id"] for market in after])

    def test_stale_quote_is_rejected_and_ticks_cover_every_outcome(self):
        first_token = self.join("First")
        second_token = self.join("Second")
        self.seed()
        first_headers = {"X-Participant-Token": first_token}
        second_headers = {"X-Participant-Token": second_token}
        market = self.client.get("/api/markets", headers=first_headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        quote = self.client.post(
            f"/api/markets/{market['id']}/quote",
            headers=first_headers,
            json={"outcome_id": outcome_id, "shares": 2, "side": "buy"},
        ).json()["quote"]
        moved = self.trade(second_headers, market["id"], outcome_id, 1, "buy")
        self.assertEqual(moved.status_code, 200, moved.text)
        stale = self.client.post(
            f"/api/markets/{market['id']}/buy",
            headers=first_headers,
            json={
                "outcome_id": outcome_id,
                "shares": 2,
                "market_revision": quote["market_revision"],
                "max_cost": quote["estimated_cost"] + 0.01,
            },
        )
        self.assertEqual(stale.status_code, 409)
        detail = self.client.get(f"/api/markets/{market['id']}", headers=first_headers).json()
        marked_outcomes = {point["outcome_id"] for point in detail["price_history"]}
        self.assertEqual(marked_outcomes, {outcome["id"] for outcome in market["outcomes"]})

    def test_concurrent_same_revision_commits_once_and_rolls_back_loser(self):
        first_token = self.join("Concurrent First")
        second_token = self.join("Concurrent Second")
        self.seed()
        headers = {"X-Participant-Token": first_token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        quote = self.client.post(
            f"/api/markets/{market['id']}/quote",
            headers=headers,
            json={"outcome_id": outcome_id, "shares": 1, "side": "buy"},
        ).json()["quote"]

        def submit(token):
            with TestClient(market_app.app) as client:
                return client.post(
                    f"/api/markets/{market['id']}/buy",
                    headers={"X-Participant-Token": token},
                    json={
                        "outcome_id": outcome_id,
                        "shares": 1,
                        "market_revision": quote["market_revision"],
                        "max_cost": quote["estimated_cost"] + 1,
                    },
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(submit, [first_token, second_token]))
        self.assertEqual(statuses, [200, 409])
        with market_app.db() as conn:
            trade_count = conn.execute(
                "SELECT COUNT(*) AS count FROM trades WHERE market_id = ?", (market["id"],)
            ).fetchone()["count"]
            quantity = conn.execute("SELECT quantity FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()["quantity"]
            ledger_count = conn.execute(
                "SELECT COUNT(*) AS count FROM ledger_entries WHERE market_id = ?", (market["id"],)
            ).fetchone()["count"]
            committed_trade = conn.execute(
                "SELECT id, cash_delta FROM trades WHERE market_id = ?", (market["id"],)
            ).fetchone()
            trade_cash_delta = committed_trade["cash_delta"]
            trade_event = conn.execute(
                "SELECT payload_json FROM market_events WHERE market_id = ? AND event_type = 'trade'",
                (market["id"],),
            ).fetchone()
            combined_cash = conn.execute(
                "SELECT SUM(cash) AS total FROM participants WHERE display_name LIKE 'Concurrent %'"
            ).fetchone()["total"]
        self.assertEqual(trade_count, 1)
        self.assertEqual(ledger_count, 1)
        self.assertEqual(quantity, 1)
        self.assertAlmostEqual(combined_cash, 20_000 + trade_cash_delta, places=4)
        self.assertEqual(json.loads(trade_event["payload_json"])["trade_id"], committed_trade["id"])

    def test_archived_automated_test_market_is_hidden_but_preserved(self):
        token = self.join()
        with market_app.db() as conn:
            market_id = market_app.create_market(
                conn,
                "1326428061876371456",
                "Playwright Coin Toss historical",
                "binary",
                [("YES", "manual:yes"), ("NO", "manual:no")],
                "",
                "Test",
                "Test only",
                "medium",
                "manual:historical-test",
            )
            market_app.archive_playwright_contracts(conn)
            archived = dict(conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone())
        self.assertEqual(archived["origin"], "automated_test")
        self.assertEqual(archived["visibility"], "hidden")
        markets = self.client.get("/api/markets", headers={"X-Participant-Token": token}).json()["markets"]
        self.assertFalse(any("Playwright Coin Toss" in market["title"] for market in markets))

    def test_weekly_resolution_waits_until_tuesday_and_settles_from_one_sleeper_sync(self):
        self.seed()
        fixture, _ = self.modeled_fixture()
        fixture["league"]["settings"]["leg"] = 1
        fixture["matchups"]["1"] = [
            {"roster_id": roster_id, "matchup_id": (roster_id + 1) // 2, "points": 80 + roster_id}
            for roster_id in range(1, 13)
        ]
        fixture["matchups"]["1"][1]["points"] = fixture["matchups"]["1"][0]["points"]
        with market_app.db() as conn:
            weekly = conn.execute(
                "SELECT id, contract_key FROM markets WHERE contract_key LIKE '%:week:1:%' ORDER BY id"
            ).fetchall()
            top_id = next(row["id"] for row in weekly if row["contract_key"].endswith("week_top"))
            low_id = next(row["id"] for row in weekly if row["contract_key"].endswith("week_low"))
            conn.execute(
                "UPDATE markets SET status = 'closed', closed_at = ? WHERE id IN (?, ?)",
                (market_app.now_iso(), top_id, low_id),
            )
            market_app.sync_snapshot(conn, fixture, "1326428061876371456")
            before_deadline = market_app.observe_automatic_resolutions(
                conn,
                as_of=datetime(2026, 9, 15, 4, 59, tzinfo=timezone.utc),
            )
            at_deadline = market_app.observe_automatic_resolutions(
                conn,
                as_of=datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc),
            )
            statuses = {
                row["id"]: (row["status"], row["winning_outcome_id"])
                for row in conn.execute(
                    "SELECT id, status, winning_outcome_id FROM markets WHERE id IN (?, ?)",
                    (top_id, low_id),
                ).fetchall()
            }
            top_winner = conn.execute(
                "SELECT source_ref FROM outcomes WHERE id = ?", (statuses[top_id][1],)
            ).fetchone()["source_ref"]
        self.assertEqual(before_deadline["observed"], 0)
        self.assertFalse(before_deadline["resolved"])
        self.assertEqual(at_deadline["observed"], 2)
        self.assertEqual(at_deadline["resolved"], [top_id])
        self.assertEqual(at_deadline["voided"], [low_id])
        self.assertEqual(statuses[top_id][0], "resolved")
        self.assertEqual(statuses[low_id][0], "void")
        self.assertEqual(top_winner, "roster:12")

    def test_weekly_settlement_time_is_tuesday_at_one_am_eastern(self):
        settlement = market_app.weekly_settlement_time(2026, 1)
        self.assertEqual(settlement, datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc))

    def test_live_score_mark_updates_weekly_model_odds_and_closes_started_week(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        markets = self.client.get("/api/markets", headers=headers).json()["markets"]
        top = next(market for market in markets if market["title"] == "Week 1 Top Scoring Team")
        low = next(market for market in markets if market["title"] == "Week 1 Lowest Scoring Team")
        before_top = next(outcome for outcome in top["outcomes"] if outcome["source_ref"] == "roster:12")
        before_low = next(outcome for outcome in low["outcomes"] if outcome["source_ref"] == "roster:1")
        matchups = [
            {
                "roster_id": roster_id,
                "matchup_id": (roster_id + 1) // 2,
                "points": 150 if roster_id == 12 else roster_id,
                "starters_points": [25] * 6 if roster_id == 12 else [roster_id / 6] * 6,
            }
            for roster_id in range(1, 13)
        ]
        result = market_app.run_live_score_mark_operation(
            "1326428061876371456",
            matchups_override=matchups,
            as_of=datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc),
            simulation_count=300,
        )
        self.assertEqual(result["marked"], 2)
        self.assertEqual(result["closed"], 2)

        after = self.client.get("/api/markets", headers=headers).json()["markets"]
        top_after = next(market for market in after if market["id"] == top["id"])
        low_after = next(market for market in after if market["id"] == low["id"])
        after_top = next(outcome for outcome in top_after["outcomes"] if outcome["source_ref"] == "roster:12")
        after_low = next(outcome for outcome in low_after["outcomes"] if outcome["source_ref"] == "roster:1")
        self.assertEqual(top_after["status"], "closed")
        self.assertEqual(low_after["status"], "closed")
        self.assertAlmostEqual(after_top["probability"], before_top["probability"], places=4)
        self.assertGreater(after_top["model_probability"], 0.95)
        self.assertGreater(after_top["model_probability"], before_top["model_probability"])
        self.assertGreater(after_low["model_probability"], 0.95)
        self.assertGreater(after_low["model_probability"], before_low["model_probability"])
        self.assertEqual(top_after["model"]["mark_source"], "live_scores")
        with market_app.db() as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) AS count FROM market_events WHERE event_type = 'live_score_mark'"
            ).fetchone()["count"]
        self.assertEqual(event_count, 2)

    def test_weekly_lifecycle_rehearsal_closes_settles_pays_and_refunds(self):
        self.seed()
        token = self.join("Lifecycle Rehearsal")
        headers = {"X-Participant-Token": token}
        fixture, _ = self.modeled_fixture()
        fixture["matchups"]["1"] = [
            {"roster_id": roster_id, "matchup_id": (roster_id + 1) // 2, "points": 80 + roster_id}
            for roster_id in range(1, 13)
        ]
        fixture["matchups"]["1"][1]["points"] = fixture["matchups"]["1"][0]["points"]

        with market_app.db() as conn:
            weekly = conn.execute(
                "SELECT id, contract_key FROM markets WHERE contract_key LIKE '%:week:1:%' ORDER BY id"
            ).fetchall()
            top_id = next(int(row["id"]) for row in weekly if row["contract_key"].endswith("week_top"))
            low_id = next(int(row["id"]) for row in weekly if row["contract_key"].endswith("week_low"))
            top_winner_id = conn.execute(
                "SELECT id FROM outcomes WHERE market_id = ? AND source_ref = 'roster:12'",
                (top_id,),
            ).fetchone()["id"]
            low_position_id = conn.execute(
                "SELECT id FROM outcomes WHERE market_id = ? AND source_ref = 'roster:1'",
                (low_id,),
            ).fetchone()["id"]

        self.assertEqual(self.trade(headers, top_id, top_winner_id, 3, "buy").status_code, 200)
        self.assertEqual(self.trade(headers, low_id, low_position_id, 2, "buy").status_code, 200)
        with market_app.db() as conn:
            participant_id = conn.execute(
                "SELECT id FROM participants WHERE display_name = 'Lifecycle Rehearsal'"
            ).fetchone()["id"]
            top_cash_delta = float(conn.execute(
                "SELECT cash_delta FROM trades WHERE participant_id = ? AND market_id = ?",
                (participant_id, top_id),
            ).fetchone()["cash_delta"])

        calls = {"league": 0, "matchups": [], "bracket": 0}
        original_league = market_app.SleeperAdapter.get_league
        original_matchups = market_app.SleeperAdapter.get_matchups
        original_bracket = market_app.SleeperAdapter.get_winners_bracket

        def get_league(league_id):
            calls["league"] += 1
            return deepcopy(fixture["league"])

        def get_matchups(league_id, week):
            calls["matchups"].append(week)
            return deepcopy(fixture["matchups"][str(week)])

        def get_bracket(league_id):
            calls["bracket"] += 1
            return []

        market_app.SleeperAdapter.get_league = staticmethod(get_league)
        market_app.SleeperAdapter.get_matchups = staticmethod(get_matchups)
        market_app.SleeperAdapter.get_winners_bracket = staticmethod(get_bracket)
        kickoff = market_app.first_nfl_kickoff(2026)
        deadline = market_app.weekly_settlement_time(2026, 1)
        try:
            closed = market_app.run_lifecycle_operation(
                "scheduler", as_of=kickoff, refresh_sources=True
            )
            pending = market_app.run_lifecycle_operation(
                "scheduler", as_of=deadline - timedelta(minutes=1), refresh_sources=True
            )
            settled = market_app.run_tracked_job(
                "1326428061876371456",
                "lifecycle",
                "scheduler",
                lambda: market_app.run_lifecycle_operation(
                    "scheduler", as_of=deadline, refresh_sources=True
                ),
            )
        finally:
            market_app.SleeperAdapter.get_league = original_league
            market_app.SleeperAdapter.get_matchups = original_matchups
            market_app.SleeperAdapter.get_winners_bracket = original_bracket

        self.assertEqual(closed["closed"], 2)
        self.assertEqual(closed["sources_refreshed"], 0)
        self.assertEqual(pending["sources_refreshed"], 0)
        self.assertEqual(calls, {"league": 1, "matchups": [1], "bracket": 0})
        self.assertEqual(settled["resolved"], [top_id])
        self.assertEqual(settled["voided"], [low_id])
        self.assertEqual(settled["observed"], 2)

        with market_app.db() as conn:
            statuses = {
                int(row["id"]): row["status"]
                for row in conn.execute(
                    "SELECT id, status FROM markets WHERE id IN (?, ?)", (top_id, low_id)
                ).fetchall()
            }
            participant = conn.execute(
                "SELECT cash FROM participants WHERE id = ?", (participant_id,)
            ).fetchone()
            ledger_types = {
                row["entry_type"]
                for row in conn.execute(
                    "SELECT entry_type FROM ledger_entries WHERE participant_id = ?",
                    (participant_id,),
                ).fetchall()
            }
            job = conn.execute(
                "SELECT status, triggered_by FROM job_runs WHERE job_type = 'lifecycle' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            resolution_actions = [
                action for action in market_app.admin_dashboard_payload(
                    conn, "1326428061876371456"
                )["actions"]
                if action["type"] == "market_resolution"
            ]

        self.assertEqual(statuses, {top_id: "resolved", low_id: "void"})
        self.assertAlmostEqual(float(participant["cash"]), 10_000 + top_cash_delta + 300, places=5)
        self.assertIn("settlement", ledger_types)
        self.assertIn("void_refund", ledger_types)
        self.assertEqual(dict(job), {"status": "succeeded", "triggered_by": "scheduler"})
        self.assertFalse(resolution_actions)

    def test_playoff_and_champion_markets_resolve_from_stable_sleeper_bracket(self):
        self.seed()
        fixture, _ = self.modeled_fixture()
        fixture["league"]["settings"]["leg"] = 15
        fixture["winners_bracket"] = [
            {"r": 1, "m": 1, "t1": 1, "t2": 2},
            {"r": 1, "m": 2, "t1": 3, "t2": 4},
            {"r": 1, "m": 3, "t1": 5, "t2": 6},
        ]
        with market_app.db() as conn:
            playoff_id = conn.execute(
                "SELECT id FROM markets WHERE contract_key LIKE '%:makes-playoffs:1'"
            ).fetchone()["id"]
            champion_id = conn.execute(
                "SELECT id FROM markets WHERE contract_key LIKE '%:champion'"
            ).fetchone()["id"]
            conn.execute(
                "UPDATE markets SET status = 'closed', closed_at = ? WHERE id IN (?, ?)",
                (market_app.now_iso(), playoff_id, champion_id),
            )
            market_app.sync_snapshot(conn, fixture, "1326428061876371456")
            first = market_app.observe_automatic_resolutions(conn)
            market_app.sync_snapshot(conn, fixture, "1326428061876371456")
            second = market_app.observe_automatic_resolutions(conn)
            playoff = market_app.market_with_outcomes(conn, playoff_id)
            champion = market_app.market_with_outcomes(conn, champion_id)
        self.assertFalse(first["resolved"])
        self.assertEqual(second["resolved"], [playoff_id])
        self.assertEqual(playoff["status"], "resolved")
        self.assertEqual(
            next(outcome["source_ref"] for outcome in playoff["outcomes"] if outcome["id"] == playoff["winning_outcome_id"]),
            "roster:1:yes",
        )
        self.assertEqual(champion["status"], "closed")

        dashboard = self.client.get(
            "/api/admin/dashboard?league_id=1326428061876371456", headers=self.admin_headers()
        ).json()
        self.assertFalse(any(action["id"] == f"resolve:{champion_id}" for action in dashboard["actions"]))

        fixture["winners_bracket"].append({"r": 3, "m": 6, "p": 1, "t1": 1, "t2": 3, "w": 3, "l": 1})
        with market_app.db() as conn:
            market_app.sync_snapshot(conn, fixture, "1326428061876371456")
            market_app.observe_automatic_resolutions(conn)
            market_app.sync_snapshot(conn, fixture, "1326428061876371456")
            result = market_app.observe_automatic_resolutions(conn)
            champion = market_app.market_with_outcomes(conn, champion_id)
        self.assertEqual(result["resolved"], [champion_id])
        self.assertEqual(champion["status"], "resolved")
        self.assertEqual(
            next(outcome["source_ref"] for outcome in champion["outcomes"] if outcome["id"] == champion["winning_outcome_id"]),
            "roster:3",
        )

    def test_pipeline_uses_recent_last_known_good_projections(self):
        self.seed()
        fixture, _ = self.modeled_fixture()
        original_snapshot = market_app.SleeperAdapter.build_snapshot
        original_projections = market_app.SleeperAdapter.get_projections
        market_app.SleeperAdapter.build_snapshot = staticmethod(lambda league_id: deepcopy(fixture))
        market_app.SleeperAdapter.get_projections = staticmethod(
            lambda season, week: (_ for _ in ()).throw(RuntimeError("projection feed unavailable"))
        )
        try:
            result = market_app.execute_live_pipeline("1326428061876371456", simulation_count=300)
        finally:
            market_app.SleeperAdapter.build_snapshot = original_snapshot
            market_app.SleeperAdapter.get_projections = original_projections
        self.assertEqual(result["ingestion"]["projection_source"], "last_known_good")
        self.assertGreaterEqual(result["model"]["coverage"], 0.95)

        metrics = self.client.get(
            "/api/admin/metrics?league_id=1326428061876371456", headers=self.admin_headers()
        )
        self.assertEqual(metrics.status_code, 200, metrics.text)
        self.assertEqual(metrics.json()["model"]["simulation_count"], 300)
        self.assertIn("score_mae", metrics.json()["model"])
        with market_app.db() as conn:
            score_estimate_count = conn.execute(
                "SELECT COUNT(*) AS count FROM team_score_estimates WHERE model_run_id = ?",
                (result["model"]["id"],),
            ).fetchone()["count"]
        self.assertEqual(score_estimate_count, 12)

    def test_pipeline_rejects_concurrent_runs(self):
        self.assertTrue(market_app._PIPELINE_LOCK.acquire(blocking=False))
        try:
            response = self.client.post(
                "/api/admin/pipeline",
                headers=self.admin_headers(),
                json={"league_id": "1326428061876371456"},
            )
        finally:
            market_app._PIPELINE_LOCK.release()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("already running", response.json()["detail"])

    def test_admin_overview_reports_launch_health(self):
        self.seed()
        response = self.client.get(
            "/api/admin/overview?league_id=1326428061876371456",
            headers=self.admin_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        overview = response.json()
        self.assertFalse(overview["pipeline_running"])
        self.assertGreaterEqual(overview["model"]["coverage"], 0.95)
        self.assertGreater(overview["markets"]["counts"]["open"], 0)
        self.assertEqual(overview["data"]["projection_source"], "sleeper")
        self.assertTrue(any(warning["code"] == "backup_stale" for warning in overview["warnings"]))

    def test_feedback_submission_and_admin_triage(self):
        token = self.join("Beta Tester")
        self.seed()
        headers = {"X-Participant-Token": token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        response = self.client.post(
            "/api/feedback",
            headers=headers,
            json={
                "category": "pricing_odds",
                "message": "The implied odds changed and I need a clearer explanation.",
                "page": "markets",
                "market_id": market["id"],
                "context": {"selected_market_id": market["id"], "viewport": {"width": 1200, "height": 800}},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        feedback = response.json()["feedback"]
        self.assertEqual(feedback["status"], "new")
        self.assertEqual(feedback["display_name"], "Beta Tester")
        self.assertEqual(feedback["market_title"], market["title"])

        inbox = self.client.get(
            "/api/admin/feedback?league_id=1326428061876371456&status=open",
            headers=self.admin_headers(),
        )
        self.assertEqual(inbox.status_code, 200, inbox.text)
        self.assertEqual(inbox.json()["feedback"][0]["id"], feedback["id"])
        dashboard = self.client.get(
            "/api/admin/dashboard?league_id=1326428061876371456",
            headers=self.admin_headers(),
        ).json()
        self.assertTrue(any(action["type"] == "feedback_review" for action in dashboard["actions"]))

        updated = self.client.post(
            f"/api/admin/feedback/{feedback['id']}/status",
            headers=self.admin_headers(),
            json={"status": "resolved", "note": "Handled in beta notes"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["feedback"]["status"], "resolved")
        dashboard = self.client.get(
            "/api/admin/dashboard?league_id=1326428061876371456",
            headers=self.admin_headers(),
        ).json()
        self.assertFalse(any(action["type"] == "feedback_review" for action in dashboard["actions"]))

    def test_realtime_revision_changes_after_trade(self):
        token = self.join("Live Trader")
        self.seed()
        headers = {"X-Participant-Token": token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        with market_app.db() as conn:
            before = market_app.realtime_revision(conn, "1326428061876371456")

        trade = self.trade(headers, market["id"], outcome_id, 2, "buy")
        self.assertEqual(trade.status_code, 200, trade.text)

        with market_app.db() as conn:
            after = market_app.realtime_revision(conn, "1326428061876371456")
        self.assertNotEqual(before["signature"], after["signature"])
        self.assertGreater(after["trade_id"], before["trade_id"])
        self.assertGreater(after["market_event_id"], before["market_event_id"])

    def test_admin_session_cookie_expiry_logout_and_origin_protection(self):
        token = self.join("Commissioner Session")
        invalid = self.client.post(
            "/api/admin/session",
            headers={"X-Participant-Token": token},
            json={"admin_code": "wrong"},
        )
        self.assertEqual(invalid.status_code, 403, invalid.text)

        unlocked = self.unlock_admin(token)
        cookie = unlocked.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        session = self.client.get("/api/admin/session")
        self.assertTrue(session.json()["authenticated"])

        dashboard = self.client.get("/api/admin/dashboard?league_id=1326428061876371456")
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        rejected = self.client.post(
            "/api/admin/lifecycle/run",
            headers={"Origin": "https://attacker.example"},
            json={},
        )
        self.assertEqual(rejected.status_code, 403, rejected.text)

        logout = self.client.delete("/api/admin/session")
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 403)

    def test_admin_dashboard_tracks_jobs_audit_and_exception_resolution(self):
        token = self.join("Inbox Commissioner")
        self.seed()
        self.unlock_admin(token)

        initial = self.client.get("/api/admin/dashboard?league_id=1326428061876371456")
        self.assertEqual(initial.status_code, 200, initial.text)
        actions = initial.json()["actions"]
        self.assertTrue(any(action["type"] == "participant_link" for action in actions))
        self.assertFalse(any(action["type"] == "market_resolution" for action in actions))

        backup = self.client.post("/api/admin/maintenance/backup", json={})
        self.assertEqual(backup.status_code, 200, backup.text)
        with market_app.db() as conn:
            job = conn.execute("SELECT * FROM job_runs WHERE job_type = 'backup' ORDER BY id DESC LIMIT 1").fetchone()
            events = conn.execute("SELECT action FROM admin_events ORDER BY id").fetchall()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["triggered_by"], "commissioner")
        self.assertTrue(any("maintenance/backup" in row["action"] for row in events))

        market = self.client.post(
            "/api/admin/markets",
            json={
                "league_id": "1326428061876371456",
                "title": "Commissioner exception market",
                "market_type": "binary",
                "outcomes": ["YES", "NO"],
                "resolution_rule": "Commissioner records the official result.",
            },
        ).json()["market"]
        closed = self.client.post(f"/api/admin/markets/{market['id']}/close", json={})
        self.assertEqual(closed.status_code, 200, closed.text)
        queued = self.client.get("/api/admin/dashboard?league_id=1326428061876371456").json()
        resolution = next(action for action in queued["actions"] if action["type"] == "market_resolution")
        self.assertEqual(resolution["payload"]["market"]["status"], "closed")

        winner = market["outcomes"][0]["id"]
        resolved = self.client.post(
            f"/api/admin/markets/{market['id']}/resolve",
            json={"winning_outcome_id": winner},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        after = self.client.get("/api/admin/dashboard?league_id=1326428061876371456").json()
        self.assertFalse(any(action["id"] == resolution["id"] for action in after["actions"]))

    def test_scheduled_endpoint_runs_lifecycle_and_skips_fresh_daily_jobs(self):
        self.seed()
        with market_app.db() as conn:
            for job_type in ("live_scores", "pipeline", "backup", "prune"):
                conn.execute(
                    """
                    INSERT INTO job_runs
                      (league_id, job_type, status, triggered_by, started_at, completed_at, result_json)
                    VALUES (?, ?, 'succeeded', 'scheduler', ?, ?, '{}')
                    """,
                    ("1326428061876371456", job_type, market_app.now_iso(), market_app.now_iso()),
                )
        response = self.client.post("/api/admin/scheduled/run", headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["completed"], ["lifecycle"])
        self.assertEqual(set(result["skipped"]), {"live_scores", "pipeline", "backup", "prune"})
        with market_app.db() as conn:
            lifecycle = conn.execute(
                "SELECT * FROM job_runs WHERE job_type = 'lifecycle' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(lifecycle["status"], "succeeded")
        self.assertEqual(lifecycle["triggered_by"], "scheduler")

    def test_scheduled_endpoint_runs_live_score_marks_when_due(self):
        self.seed()
        with market_app.db() as conn:
            for job_type in ("pipeline", "backup", "prune"):
                conn.execute(
                    """
                    INSERT INTO job_runs
                      (league_id, job_type, status, triggered_by, started_at, completed_at, result_json)
                    VALUES (?, ?, 'succeeded', 'scheduler', ?, ?, '{}')
                    """,
                    ("1326428061876371456", job_type, market_app.now_iso(), market_app.now_iso()),
                )
        original_matchups = market_app.SleeperAdapter.get_matchups
        market_app.SleeperAdapter.get_matchups = staticmethod(lambda league_id, week: [
            {
                "roster_id": roster_id,
                "matchup_id": (roster_id + 1) // 2,
                "points": 150 if roster_id == 12 else roster_id,
                "starters_points": [25] * 6 if roster_id == 12 else [roster_id / 6] * 6,
            }
            for roster_id in range(1, 13)
        ])
        try:
            response = self.client.post("/api/admin/scheduled/run", headers=self.admin_headers())
        finally:
            market_app.SleeperAdapter.get_matchups = original_matchups
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIn("live_scores", result["completed"])
        self.assertEqual(set(result["skipped"]), {"pipeline", "backup", "prune"})
        with market_app.db() as conn:
            live_scores = conn.execute(
                "SELECT * FROM job_runs WHERE job_type = 'live_scores' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(live_scores["status"], "succeeded")
        self.assertEqual(live_scores["triggered_by"], "scheduler")

    def test_scheduled_endpoint_records_pipeline_failure_and_completes_maintenance(self):
        self.seed()
        with market_app.db() as conn:
            conn.execute(
                """
                INSERT INTO job_runs
                  (league_id, job_type, status, triggered_by, started_at, completed_at, result_json)
                VALUES (?, 'live_scores', 'succeeded', 'scheduler', ?, ?, '{}')
                """,
                ("1326428061876371456", market_app.now_iso(), market_app.now_iso()),
            )
        original_pipeline = market_app.execute_live_pipeline
        market_app.execute_live_pipeline = lambda league_id: (_ for _ in ()).throw(RuntimeError("Sleeper unavailable"))
        try:
            response = self.client.post("/api/admin/scheduled/run", headers=self.admin_headers())
        finally:
            market_app.execute_live_pipeline = original_pipeline
        self.assertEqual(response.status_code, 502, response.text)
        detail = response.json()["detail"]
        self.assertIn("pipeline", detail["errors"])
        self.assertEqual(set(detail["completed"]), {"lifecycle", "backup", "prune"})
        with market_app.db() as conn:
            statuses = {
                row["job_type"]: row["status"]
                for row in conn.execute(
                    "SELECT job_type, status FROM job_runs WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job_type)"
                ).fetchall()
            }
        self.assertEqual(statuses["pipeline"], "failed")
        self.assertEqual(statuses["backup"], "succeeded")
        self.assertEqual(statuses["prune"], "succeeded")

    def test_verified_backup_restores_a_working_exchange(self):
        token = self.join("Restore Drill")
        self.seed()
        before = self.client.get("/api/markets", headers={"X-Participant-Token": token}).json()["markets"]
        backup = market_app.create_database_backup()
        self.assertEqual(backup["integrity_check"], "ok")
        restored_path = Path(self.tempdir.name) / "restored.sqlite"
        shutil.copy2(backup["path"], restored_path)
        restored = sqlite3.connect(restored_path)
        try:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            restored.close()
        original_path = market_app.DB_PATH
        market_app.DB_PATH = restored_path
        try:
            after = self.client.get("/api/markets", headers={"X-Participant-Token": token})
            self.assertEqual(after.status_code, 200, after.text)
            self.assertEqual([market["id"] for market in after.json()["markets"]], [market["id"] for market in before])
        finally:
            market_app.DB_PATH = original_path

    def test_admin_dashboard_collapses_historical_logins_for_a_linked_identity(self):
        first = self.client.post(
            "/api/auth/join",
            json={
                "invite_code": "theleague",
                "display_name": "Commissioner",
                "sleeper_username": "same-manager",
                "league_id": "1326428061876371456",
            },
        ).json()
        self.client.post(
            "/api/auth/join",
            json={
                "invite_code": "theleague",
                "display_name": "Commissioner Again",
                "sleeper_username": "same-manager",
                "league_id": "1326428061876371456",
            },
        )
        with market_app.db() as conn:
            conn.execute(
                "UPDATE participants SET sleeper_user_id = 'manager-1' WHERE token = ?",
                (first["token"],),
            )

        dashboard = self.client.get(
            "/api/admin/dashboard?league_id=1326428061876371456",
            headers=self.admin_headers(),
        )
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        identity_actions = [
            action for action in dashboard.json()["actions"] if action["type"] == "participant_link"
        ]
        self.assertEqual(identity_actions, [])

    def test_demo_populate_creates_season_activity(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        response = self.client.post("/api/demo/populate", headers=headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["trades"], 0)
        portfolio = self.client.get("/api/portfolio", headers=headers).json()
        self.assertGreater(len(portfolio["positions"]), 0)
        self.assertGreater(portfolio["demo"]["trade_count"], 0)
        ticker = self.client.get("/api/ticker", headers=headers).json()
        self.assertGreater(len(ticker["items"]), 0)
        self.assertTrue(ticker["items"][0]["is_demo"])

    def test_demo_clear_preserves_live_trade(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        buy = self.trade(headers, market["id"], outcome_id, 3, "buy")
        self.assertEqual(buy.status_code, 200, buy.text)
        self.client.post("/api/demo/populate", headers=headers, json={})
        before_clear = self.client.get("/api/portfolio", headers=headers).json()
        self.assertGreater(before_clear["demo"]["trade_count"], 0)

        clear = self.client.post("/api/demo/clear", headers=headers, json={})
        self.assertEqual(clear.status_code, 200, clear.text)
        self.assertGreater(clear.json()["cleared_trades"], 0)
        portfolio = self.client.get("/api/portfolio", headers=headers).json()
        self.assertEqual(portfolio["demo"]["trade_count"], 0)
        self.assertTrue(any(round(position["shares"], 4) == 3 for position in portfolio["positions"]))

    def test_manual_resolution_settles_winning_positions(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        self.trade(headers, market["id"], outcome_id, 5, "buy")
        open_resolution = self.client.post(
            f"/api/admin/markets/{market['id']}/resolve",
            headers=self.admin_headers(),
            json={"winning_outcome_id": outcome_id},
        )
        self.assertEqual(open_resolution.status_code, 409, open_resolution.text)
        closed = self.client.post(
            f"/api/admin/markets/{market['id']}/close",
            headers=self.admin_headers(),
            json={},
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        response = self.client.post(
            f"/api/admin/markets/{market['id']}/resolve",
            headers=self.admin_headers(),
            json={"winning_outcome_id": outcome_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        portfolio = self.client.get("/api/portfolio", headers=headers).json()
        settlement_entries = [entry for entry in portfolio["ledger"] if entry["entry_type"] == "settlement"]
        self.assertEqual(len(settlement_entries), 1)
        self.assertAlmostEqual(settlement_entries[0]["amount"], 500)

    def test_sync_snapshot_fixture_shape(self):
        fixture = Path(__file__).resolve().parent.parent / "backend" / "fixtures" / "sleeper_sample.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        with market_app.db() as conn:
            result = market_app.sync_snapshot(conn, payload, "1326428061876371456")
            meta = market_app.active_league_meta(conn, "1326428061876371456")
        self.assertEqual(result["teams"], 12)
        self.assertGreater(result["players"], 50)
        self.assertGreater(len(result["managers"]), 0)
        self.assertEqual(meta["roster_count"], 12)
        self.assertEqual(meta["manager_count"], 12)
        managers = self.client.get(
            "/api/admin/managers?league_id=1326428061876371456",
            headers=self.admin_headers(),
        )
        self.assertEqual(managers.status_code, 200, managers.text)
        self.assertGreater(len(managers.json()["managers"]), 0)
        self.assertIn("username", managers.json()["managers"][0])

    def test_setup_league_runs_model_and_publishes_eligible_markets(self):
        fixture, projections = self.modeled_fixture()
        original_snapshot = market_app.SleeperAdapter.build_snapshot
        original_projections = market_app.SleeperAdapter.get_projections
        market_app.SleeperAdapter.build_snapshot = staticmethod(lambda league_id: deepcopy(fixture))
        market_app.SleeperAdapter.get_projections = staticmethod(
            lambda season, week: deepcopy(projections)
        )
        try:
            response = self.client.post(
                "/api/admin/setup-league",
                headers=self.admin_headers(),
                json={"league_id": "1326428061876371456", "reset_markets": True, "include_cup": True},
            )
        finally:
            market_app.SleeperAdapter.build_snapshot = original_snapshot
            market_app.SleeperAdapter.get_projections = original_projections
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertGreater(payload["synced"]["teams"], 0)
        self.assertGreater(payload["markets"]["season"], 0)
        self.assertEqual(payload["markets"]["cup"], 0)
        self.assertGreaterEqual(payload["model"]["coverage"], 0.95)
        token = self.join()
        markets = self.client.get("/api/markets", headers={"X-Participant-Token": token}).json()["markets"]
        titles = {market["title"] for market in markets}
        self.assertIn("2026 League Champion", titles)
        self.assertIn("Week 1 Top Scoring Team", titles)
        self.assertIn("Week 1 Lowest Scoring Team", titles)
        self.assertFalse(any("Commissioners Cup" in title for title in titles))
        self.assertFalse(any("Top Scoring QB" in title or "MVP" in title for title in titles))
        self.assertFalse(
            any(
                str(outcome.get("source_ref") or "").startswith("player:")
                for market in markets
                for outcome in market["outcomes"]
            )
        )

    def test_resolution_center_and_market_detail_include_evidence(self):
        token = self.join()
        self.seed()
        headers = {"X-Participant-Token": token}
        markets = self.client.get("/api/markets", headers=headers).json()["markets"]
        champion = next(market for market in markets if market["title"] == "2026 League Champion")
        outcome_id = champion["outcomes"][0]["id"]
        self.trade(headers, champion["id"], outcome_id, 4, "buy")
        detail = self.client.get(f"/api/markets/{champion['id']}", headers=headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_payload = detail.json()
        self.assertGreater(len(detail_payload["price_history"]), 0)
        self.assertIn("evidence", detail_payload["market"]["outcomes"][0])

        center = self.client.get(
            "/api/admin/resolution-center?league_id=1326428061876371456&status=actionable",
            headers=self.admin_headers(),
        )
        self.assertEqual(center.status_code, 200, center.text)
        self.assertGreater(len(center.json()["markets"]), 0)
        self.assertIn("candidates", center.json()["markets"][0])

    def test_identity_claim_and_duplicate_protection(self):
        token = self.join("Identity Trader")
        self.seed()
        headers = {"X-Participant-Token": token}
        options = self.client.get("/api/identity/options", headers=headers)
        self.assertEqual(options.status_code, 200, options.text)
        manager = options.json()["managers"][0]

        claim = self.client.post("/api/identity/claim", headers=headers, json={"user_id": manager["user_id"]})
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json()["participant"]["sleeper_user_id"], manager["user_id"])

        other_token = self.join("Other Trader")
        duplicate = self.client.post(
            "/api/identity/claim",
            headers={"X-Participant-Token": other_token},
            json={"user_id": manager["user_id"]},
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_commissioner_participant_and_invite_management(self):
        token = self.join("Managed Trader")
        self.seed()
        participant = self.client.get("/api/session", headers={"X-Participant-Token": token}).json()["participant"]
        participants = self.client.get(
            "/api/admin/participants?league_id=1326428061876371456",
            headers=self.admin_headers(),
        )
        self.assertEqual(participants.status_code, 200, participants.text)
        self.assertTrue(any(row["display_name"] == "Managed Trader" for row in participants.json()["participants"]))

        role = self.client.post(
            f"/api/admin/participants/{participant['id']}/role",
            headers=self.admin_headers(),
            json={"role": "commissioner"},
        )
        self.assertEqual(role.status_code, 200, role.text)
        self.assertEqual(role.json()["participant"]["role"], "commissioner")

        invite = self.client.post(
            "/api/admin/invites",
            headers=self.admin_headers(),
            json={"code": "test-commissioner", "role": "commissioner", "uses_remaining": 2, "league_id": "1326428061876371456"},
        )
        self.assertEqual(invite.status_code, 200, invite.text)
        invites = self.client.get(
            "/api/admin/invites?league_id=1326428061876371456",
            headers=self.admin_headers(),
        )
        self.assertTrue(any(row["code"] == "test-commissioner" for row in invites.json()["invites"]))

        with market_app.db() as conn:
            conn.execute("UPDATE participants SET environment = 'test' WHERE id = ?", (participant["id"],))
        filtered = self.client.get(
            "/api/admin/participants?league_id=1326428061876371456",
            headers=self.admin_headers(),
        )
        self.assertFalse(any(row["id"] == participant["id"] for row in filtered.json()["participants"]))

    def test_fund_summary_settings_manual_entry_and_allocation(self):
        token = self.join("Fund Trader")
        headers = {"X-Participant-Token": token}
        fund = self.client.get("/api/fund", headers=headers)
        self.assertEqual(fund.status_code, 200, fund.text)
        self.assertEqual(fund.json()["summary"]["fund_total"], 599)
        self.assertEqual(fund.json()["summary"]["side_quest_pool"], 114)

        entry = self.client.post(
            "/api/admin/fund/manual-entry",
            headers=self.admin_headers(),
            json={
                "league_id": "1326428061876371456",
                "entry_type": "income",
                "amount": 86,
                "description": "Carryover correction",
            },
        )
        self.assertEqual(entry.status_code, 200, entry.text)
        self.assertEqual(entry.json()["summary"]["fund_total"], 685)
        self.assertEqual(entry.json()["summary"]["side_quest_pool"], 200)

        settings = self.client.post(
            "/api/admin/fund/settings",
            headers=self.admin_headers(),
            json={
                "league_id": "1326428061876371456",
                "starting_balance": 700,
                "trophy_reserve": 120,
                "cup_reserve": 225,
                "draft_reserve": 100,
                "safety_buffer": 55,
                "core_pct": 0.5,
                "team_pct": 0.3,
                "player_pct": 0.2,
            },
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        allocated = self.client.post(
            "/api/admin/fund/allocate-sidequest",
            headers=self.admin_headers(),
            json={"league_id": "1326428061876371456"},
        )
        self.assertEqual(allocated.status_code, 200, allocated.text)
        budgets = {row["category"]: row["allocated_budget"] for row in allocated.json()["allocations"]}
        self.assertEqual(budgets["core"], 143)
        self.assertEqual(budgets["team"], 85.8)
        self.assertEqual(budgets["player"], 57.2)

    def test_market_prize_pool_generates_and_pays_fund_payout_plan(self):
        token = self.join("Prize Trader")
        self.seed()
        headers = {"X-Participant-Token": token}
        markets = self.client.get("/api/markets", headers=headers).json()["markets"]
        champion = next(market for market in markets if market["title"] == "2026 League Champion")
        outcome_id = champion["outcomes"][0]["id"]

        buy = self.trade(headers, champion["id"], outcome_id, 10, "buy")
        self.assertEqual(buy.status_code, 200, buy.text)

        prize = self.client.post(
            f"/api/admin/markets/{champion['id']}/fund-prize",
            headers=self.admin_headers(),
            json={
                "league_id": "1326428061876371456",
                "category": "core",
                "prize_pool": 50,
                "payout_mode": "proportional_shares",
            },
        )
        self.assertEqual(prize.status_code, 200, prize.text)
        self.assertEqual(prize.json()["prize"]["prize_pool"], 50)
        self.assertEqual(prize.json()["fund"]["summary"]["committed_market_reserves"], 50)

        resolved = self.client.post(
            f"/api/admin/markets/{champion['id']}/close",
            headers=self.admin_headers(),
            json={},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        resolved = self.client.post(
            f"/api/admin/markets/{champion['id']}/resolve",
            headers=self.admin_headers(),
            json={"winning_outcome_id": outcome_id},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        payouts = resolved.json()["fund_payouts"]
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0]["display_name"], "Prize Trader")
        self.assertEqual(payouts[0]["amount"], 50)
        self.assertEqual(resolved.json()["market"]["fund_prize"]["status"], "pending")

        paid = self.client.post(
            f"/api/admin/markets/{champion['id']}/payouts/pay",
            headers=self.admin_headers(),
            json={},
        )
        self.assertEqual(paid.status_code, 200, paid.text)
        self.assertEqual(paid.json()["prize"]["status"], "paid")
        self.assertEqual(paid.json()["fund"]["summary"]["fund_total"], 549)
        self.assertEqual(paid.json()["fund"]["summary"]["committed_market_reserves"], 0)
        self.assertTrue(any(row["source"] == "market_payout" and row["amount"] == -50 for row in paid.json()["fund"]["ledger"]))

    def test_leaderboard_separates_idle_participants(self):
        active_token = self.join("Active Trader")
        self.join("Idle Owner")
        self.seed()
        headers = {"X-Participant-Token": active_token}
        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        buy = self.trade(headers, market["id"], outcome_id, 1, "buy")
        self.assertEqual(buy.status_code, 200, buy.text)

        response = self.client.get("/api/leaderboard", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        rows = payload["leaderboard"]
        self.assertEqual(payload["summary"]["active_count"], 1)
        self.assertEqual(payload["summary"]["idle_count"], 1)
        self.assertEqual(rows[0]["display_name"], "Active Trader")
        self.assertTrue(rows[0]["is_active"])
        self.assertFalse(next(row for row in rows if row["display_name"] == "Idle Owner")["is_active"])

    def test_leaderboard_ignores_demo_and_hidden_market_activity(self):
        token = self.join("Clean Standings")
        self.seed()
        headers = {"X-Participant-Token": token}
        self.client.post("/api/demo/populate", headers=headers, json={})
        demo_row = self.client.get("/api/leaderboard", headers=headers).json()["leaderboard"][0]
        self.assertFalse(demo_row["is_active"])
        self.assertEqual(demo_row["trade_count"], 0)
        self.assertEqual(demo_row["profit"], 0)

        market = self.client.get("/api/markets", headers=headers).json()["markets"][0]
        outcome_id = market["outcomes"][0]["id"]
        self.trade(headers, market["id"], outcome_id, 1, "buy")
        with market_app.db() as conn:
            conn.execute(
                "UPDATE markets SET visibility = 'hidden', status = 'archived' WHERE id = ?",
                (market["id"],),
            )
        hidden_row = self.client.get("/api/leaderboard", headers=headers).json()["leaderboard"][0]
        self.assertFalse(hidden_row["is_active"])
        self.assertEqual(hidden_row["trade_count"], 0)
        self.assertEqual(hidden_row["profit"], 0)

    def test_leaderboard_collapses_duplicate_sleeper_usernames_and_archives_qa_names(self):
        first = self.client.post(
            "/api/auth/join",
            json={
                "invite_code": "theleague",
                "display_name": "First Login",
                "sleeper_username": "same-owner",
                "league_id": "1326428061876371456",
            },
        ).json()["token"]
        self.client.post(
            "/api/auth/join",
            json={
                "invite_code": "theleague",
                "display_name": "Second Login",
                "sleeper_username": "same-owner",
                "league_id": "1326428061876371456",
            },
        )
        rows = self.client.get(
            "/api/leaderboard", headers={"X-Participant-Token": first}
        ).json()["leaderboard"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "First Login")

        qa_token = self.join("Stock UI QA")
        with market_app.db() as conn:
            market_app.archive_legacy_ui_qa_participants(conn)
            environment = conn.execute(
                "SELECT environment FROM participants WHERE token = ?", (qa_token,)
            ).fetchone()["environment"]
        self.assertEqual(environment, "test")

    def test_market_prize_pool_is_capped_by_category_budget(self):
        token = self.join("Cap Trader")
        self.seed()
        headers = {"X-Participant-Token": token}
        champion = next(
            market
            for market in self.client.get("/api/markets", headers=headers).json()["markets"]
            if market["title"] == "2026 League Champion"
        )

        too_large = self.client.post(
            f"/api/admin/markets/{champion['id']}/fund-prize",
            headers=self.admin_headers(),
            json={
                "league_id": "1326428061876371456",
                "category": "core",
                "prize_pool": 58,
                "payout_mode": "proportional_shares",
            },
        )
        self.assertEqual(too_large.status_code, 409)
        self.assertIn("category budget", too_large.json()["detail"])

    def test_sleeper_fee_sync_is_idempotent_and_counts_adds_and_trades(self):
        fixture = Path(__file__).resolve().parent.parent / "backend" / "fixtures" / "sleeper_sample.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        with market_app.db() as conn:
            market_app.sync_snapshot(conn, payload, "1326428061876371456")

        original = market_app.SleeperAdapter.get_transactions

        def fake_transactions(league_id, round_number):
            if round_number != 1:
                return []
            return [
                {
                    "transaction_id": "trade-1",
                    "type": "trade",
                    "status": "complete",
                    "roster_ids": [1, 2],
                    "adds": None,
                },
                {
                    "transaction_id": "adds-1",
                    "type": "free_agent",
                    "status": "complete",
                    "roster_ids": [1],
                    "adds": {"100": 1, "200": 1, "300": 3},
                },
                {
                    "transaction_id": "waiver-pending",
                    "type": "waiver",
                    "status": "pending",
                    "roster_ids": [4],
                    "adds": {"400": 4},
                },
            ]

        market_app.SleeperAdapter.get_transactions = staticmethod(fake_transactions)
        try:
            first = self.client.post(
                "/api/admin/fund/sync-sleeper-fees",
                headers=self.admin_headers(),
                json={"league_id": "1326428061876371456", "start_round": 1, "end_round": 2},
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["created"], 4)
            self.assertEqual(first.json()["fund"]["summary"]["season_fee_total"], 14)

            second = self.client.post(
                "/api/admin/fund/sync-sleeper-fees",
                headers=self.admin_headers(),
                json={"league_id": "1326428061876371456", "start_round": 1, "end_round": 2},
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["created"], 0)
            self.assertEqual(second.json()["fund"]["summary"]["season_fee_total"], 14)
            teams = {row["roster_id"]: row["fee_total"] for row in second.json()["fund"]["teams"]}
            self.assertEqual(teams[1], 8)
            self.assertEqual(teams[2], 4)
            self.assertEqual(teams[3], 2)
        finally:
            market_app.SleeperAdapter.get_transactions = original

    def test_multi_league_markets_and_leaderboard_are_isolated(self):
        fixture = Path(__file__).resolve().parent.parent / "backend" / "fixtures" / "sleeper_sample.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        second_payload = deepcopy(payload)
        second_payload["league"]["name"] = "Other League"
        second_payload["league"]["league_id"] = "2222222222222222222"

        with market_app.db() as conn:
            market_app.sync_snapshot(conn, payload, "1326428061876371456")
            market_app.sync_snapshot(conn, second_payload, "2222222222222222222")
            first_seed = market_app.seed_standard_markets(conn, league_id="1326428061876371456", reset_seeded=True)
            second_seed = market_app.seed_standard_markets(conn, league_id="2222222222222222222", reset_seeded=True)

        first_token = self.join("First Trader", "1326428061876371456")
        second_token = self.join("Second Trader", "2222222222222222222")
        first_headers = {"X-Participant-Token": first_token}
        second_headers = {"X-Participant-Token": second_token}

        first_markets = self.client.get("/api/markets", headers=first_headers).json()["markets"]
        second_markets = self.client.get("/api/markets", headers=second_headers).json()["markets"]
        self.assertTrue(first_markets)
        self.assertTrue(second_markets)
        self.assertTrue(all(market["league_id"] == "1326428061876371456" for market in first_markets))
        self.assertTrue(all(market["league_id"] == "2222222222222222222" for market in second_markets))

        other_market = self.client.get(f"/api/markets/{second_seed['market_ids'][0]}", headers=first_headers)
        self.assertEqual(other_market.status_code, 404)

        first_leaders = self.client.get("/api/leaderboard", headers=first_headers).json()["leaderboard"]
        second_leaders = self.client.get("/api/leaderboard", headers=second_headers).json()["leaderboard"]
        self.assertEqual([row["display_name"] for row in first_leaders], ["First Trader"])
        self.assertEqual([row["display_name"] for row in second_leaders], ["Second Trader"])
        self.assertGreater(len(first_seed["market_ids"]), 0)
        self.assertGreater(len(second_seed["market_ids"]), 0)


if __name__ == "__main__":
    unittest.main()
