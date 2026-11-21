"""Testes do módulo do Flamengo, com respostas reais do ESPN em tests/fixtures/."""
import copy
import json
import time
import unittest
from pathlib import Path

from server.modules import flamengo as f

FIX = Path(__file__).parent / "fixtures"
TEAM = "819"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def game(**over):
    g = {"id": "1", "status": {"state": "in", "name": "STATUS_FIRST_HALF"},
         "home": {"id": "2029", "name": "Palmeiras", "abbr": "PAL", "score": 0},
         "away": {"id": TEAM, "name": "Flamengo", "abbr": "FLA", "score": 0}, "events": []}
    g.update(over)
    return g


def goal(eid, team, kind="goal", player="Fulano"):
    return {"id": eid, "kind": kind, "team_id": team, "player": player, "minute": "10'"}


class ParserTests(unittest.TestCase):
    def test_fixtures_split_live_and_next(self):
        fx = f.parse_fixtures(load("fixtures"))
        self.assertEqual(fx["live"]["id"], "401841322")
        self.assertEqual(fx["live"]["slug"], "bra.1")
        self.assertEqual(fx["live"]["status"]["state"], "in")
        self.assertEqual([g["status"]["state"] for g in fx["next"]], ["pre", "pre"])
        self.assertLess(fx["next"][0]["ts"], fx["next"][1]["ts"])

    def test_live_event_has_teams_clock_and_scores(self):
        g = f.parse_fixtures(load("fixtures"))["live"]
        self.assertEqual((g["home"]["abbr"], g["away"]["abbr"]), ("PAL", "FLA"))
        self.assertTrue(g["status"]["running"])
        self.assertGreater(g["status"]["clock"], 0)
        self.assertIsInstance(g["home"]["score"], int)
        self.assertTrue(g["home"]["logo"].startswith("https://"))

    def test_results_have_form_and_opponent(self):
        res = f.parse_results(load("results"), TEAM)
        self.assertEqual(len(res), 5)
        self.assertEqual(res[0]["result"], "V")            # FLA 2 x 1 CAP
        self.assertNotEqual(res[0]["opp"]["id"], TEAM)
        self.assertEqual(len(f.form_string(res)), 5)
        self.assertTrue(set(f.form_string(res)) <= set("VED"))

    def test_post_summary(self):
        meta = {"id": "401841313", "slug": "bra.1", "league": "Campeonato Brasileiro"}
        g = f.parse_summary(load("summary_post"), meta, TEAM)
        self.assertEqual(g["status"]["state"], "post")
        self.assertEqual((g["home"]["score"], g["away"]["score"]), (2, 1))
        goals = [e for e in g["events"] if e["kind"] in ("goal", "pen_goal")]
        self.assertEqual(len(goals), 3)
        self.assertIn("pen_goal", [e["kind"] for e in goals])
        self.assertTrue(any(e["kind"] == "sub" and e["other"] for e in g["events"]))
        self.assertEqual(g["lineups"][0]["formation"] != "", True)
        self.assertEqual(len(g["lineups"][0]["starters"]), 11)

    def test_stats_rows_and_share(self):
        g = f.parse_summary(load("summary_live"), {"id": "1", "slug": "bra.1"}, TEAM)
        rows = {r["key"]: r for r in g["stats"]}
        self.assertIn("possessionPct", rows)
        self.assertTrue(0 <= rows["possessionPct"]["share"] <= 1)
        self.assertTrue(rows["possessionPct"]["pct"])

    def test_commentary_newest_first_and_bounded(self):
        g = f.parse_summary(load("summary_post"), {"id": "1", "slug": "bra.1"}, TEAM)
        self.assertLessEqual(len(g["commentary"]), 20)
        self.assertIn("Fim do Jogo", g["commentary"][0]["text"])

    def test_standings_sorted_with_team(self):
        g = f.parse_summary(load("summary_post"), {"id": "1", "slug": "bra.1"}, TEAM)
        self.assertEqual(len(g["table"]), 20)
        self.assertEqual(g["table"][0]["rank"], 1)
        self.assertIn(TEAM, [r["id"] for r in g["table"]])

    def test_missing_parts_do_not_crash(self):
        g = f.parse_summary({"header": {"competitions": [{}]}}, {"id": "9", "slug": "x"}, TEAM)
        self.assertEqual((g["events"], g["stats"], g["lineups"], g["commentary"], g["table"]), ([], [], [], [], []))

    def test_clock_fallback_from_display(self):
        self.assertEqual(f.clock_seconds({"displayClock": "29'"}), 29 * 60)
        self.assertEqual(f.clock_seconds({"displayClock": "45'+3'"}), 48 * 60)
        self.assertEqual(f.clock_seconds({"clock": 1800.0, "displayClock": "x"}), 1800)

    def test_score_formats(self):
        self.assertEqual(f._num("2"), 2)
        self.assertEqual(f._num({"displayValue": "3"}), 3)
        self.assertIsNone(f._num(None))


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.t = f.AlertTracker(TEAM)

    def test_baseline_is_silent(self):
        g = game(events=[goal("a", "2029")])
        self.assertEqual(self.t.update(g), [])

    def test_our_goal_is_loud_and_only_once(self):
        self.t.update(game())
        g = game(events=[goal("a", TEAM)])
        g["away"]["score"] = 1
        a = self.t.update(g)
        self.assertEqual([(x["kind"], x["level"], x["sound"]) for x in a], [("goal", "alert", True)])
        self.assertEqual(self.t.update(g), [])

    def test_opponent_goal_is_quiet(self):
        self.t.update(game())
        g = game(events=[goal("a", "2029")])
        g["home"]["score"] = 1
        a = self.t.update(g)
        self.assertEqual((a[0]["level"], a[0]["sound"]), ("warn", False))

    def test_own_goal_credited_by_score(self):
        self.t.update(game())
        g = game(events=[goal("a", "2029", kind="own_goal")])   # jogador do PAL fez contra
        g["away"]["score"] = 1                                      # ponto do Flamengo (visitante)
        self.assertEqual(self.t.update(g)[0]["level"], "alert")

    def test_kickoff_halftime_final(self):
        self.t.update(game(status={"state": "pre", "name": "STATUS_SCHEDULED"}))
        self.assertEqual([a["kind"] for a in self.t.update(game())], ["kickoff"])
        self.assertEqual([a["kind"] for a in self.t.update(game(status={"state": "in", "name": "STATUS_HALFTIME"}))], ["period"])
        self.assertEqual(self.t.update(game(status={"state": "in", "name": "STATUS_HALFTIME"})), [])
        self.assertEqual([a["kind"] for a in self.t.update(game(status={"state": "post", "name": "STATUS_FULL_TIME"}))], ["final"])

    def test_red_card_and_disabled_kinds(self):
        self.t.update(game())
        red = {"id": "r", "kind": "red", "team_id": TEAM, "player": "X", "minute": "80'"}
        self.assertEqual([a["kind"] for a in self.t.update(game(events=[red]))], ["red_card"])
        quiet = f.AlertTracker(TEAM, {"red_card": False})
        quiet.update(game())
        self.assertEqual(quiet.update(game(events=[red])), [])


class PollTests(unittest.TestCase):
    def test_fixtures_interval_adapts_to_kickoff(self):
        m = f.Flamengo({})
        self.assertEqual(m._fixtures_every(), f.FIXTURES_IDLE)
        m.next = [{"ts": time.time() + 7200}]
        self.assertEqual(m._fixtures_every(), f.FIXTURES_IDLE)
        m.next = [{"ts": time.time() + 1800}]
        self.assertEqual(m._fixtures_every(), f.FIXTURES_SOON)
        m.next = [{"ts": time.time() + 300}]
        self.assertEqual(m._fixtures_every(), f.FIXTURES_NEAR)
        m._cur = {"id": "1"}
        self.assertEqual(m._fixtures_every(), 60.0)

    def test_state_shape_without_data(self):
        import asyncio
        st = asyncio.run(f.Flamengo({}).state())
        self.assertEqual((st["live"], st["next"], st["last"], st["form"]), (None, [], [], ""))

    def test_config_team_id_and_alerts(self):
        m = f.Flamengo({"flamengo": {"team_id": 123, "alerts": {"goal": False}}})
        self.assertEqual(m.team_id, "123")
        self.assertFalse(m.alerts.enabled["goal"])
        self.assertTrue(m.alerts.enabled["final"])


if __name__ == "__main__":
    unittest.main()
