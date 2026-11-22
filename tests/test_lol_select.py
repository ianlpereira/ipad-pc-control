"""Testes da seleção de campeões e dos comandos de lobby. Rodar: python -m unittest discover -s tests -v"""
import asyncio
import unittest

import httpx

from server.modules import lol_select as ls
from server.modules.lol_control import Control
from server.protocol import CommandError

NAMES = {103: "Ahri", 86: "Garen", 266: "Aatrox", 22: "Ashe", 157: "Yasuo"}
name_of = lambda i: NAMES.get(i, str(i))  # noqa: E731


def cell(c, champ=0, intent=0, pos="", **kw):
    return {"cellId": c, "championId": champ, "championPickIntent": intent, "assignedPosition": pos,
            "spell1Id": 4, "spell2Id": 14, "selectedSkinId": 0, **kw}


def act(aid, actor, kind, champ=0, done=False, going=False):
    return {"id": aid, "actorCellId": actor, "type": kind, "championId": champ, "completed": done,
            "isInProgress": going, "isAllyAction": actor < 5}


def session(**over):
    s = {"localPlayerCellId": 2,
         "myTeam": [cell(i, pos=p) for i, p in enumerate(["top", "jungle", "middle", "bottom", "utility"])],
         "theirTeam": [cell(i + 5) for i in range(5)],
         "bans": {"myTeamBans": [], "theirTeamBans": []},
         "actions": [[act(1, 2, "ban", going=True)], [act(10, 2, "pick")]],
         "timer": {"phase": "BAN_PICK", "adjustedTimeLeftInPhase": 25000, "totalTimeInPhase": 30000},
         "allowRerolling": False}
    s.update(over)
    return s


class ParseSession(unittest.TestCase):
    def test_ban_turn(self):
        s = session()
        s["actions"][0][0]["championId"] = 157           # banimento marcado, ainda não confirmado
        out = ls.parse_session(s, name_of, pickable=[103, 86], bannable=[157, 266])
        self.assertEqual(out["turn"], {"action_id": 1, "type": "ban"})
        self.assertEqual(out["timer"]["label"], "Sua vez de banir")
        self.assertEqual(out["timer"]["left"], 25.0)
        self.assertEqual(out["me"]["hover"], 157)
        self.assertEqual(out["champion_id"], 0)           # o banido nunca vira alvo da build
        self.assertEqual(out["avail_kind"], "ban")
        self.assertEqual(out["avail"], [157, 266])
        self.assertTrue(next(m for m in out["my_team"] if m["me"])["acting"])

    def test_pick_hover_then_locked(self):
        s = session(actions=[[act(1, 2, "ban", 157, done=True)], [act(10, 2, "pick", 103, going=True)]])
        s["myTeam"][2]["championPickIntent"] = 103
        out = ls.parse_session(s, name_of, pickable=[103, 86])
        self.assertEqual(out["turn"]["type"], "pick")
        self.assertEqual((out["champion_id"], out["locked"], out["me"]["hover"]), (103, False, 103))
        self.assertEqual(out["avail"], [103, 86])
        s["myTeam"][2]["championId"] = 103
        s["actions"][1][0].update(completed=True, isInProgress=False)
        out = ls.parse_session(s, name_of)
        self.assertTrue(out["locked"])
        self.assertIsNone(out["turn"])
        self.assertEqual(out["champion"], "Ahri")

    def test_hover_before_pick_turn(self):
        # outros jogadores banindo: dá para marcar o campeão na ação de escolha (ainda não em andamento)
        s = session(actions=[[act(1, 7, "ban", going=True)], [act(10, 2, "pick")]])
        action = ls.find_action(s)
        self.assertEqual(action["id"], 10)
        self.assertIsNone(ls.find_action(s, in_progress_only=True))
        self.assertIsNone(ls.parse_session(s, name_of)["turn"])

    def test_enemy_hidden_until_locked(self):
        s = session()
        s["theirTeam"][0].update(championId=266)
        s["theirTeam"][1].update(championPickIntent=22)   # intenção do inimigo não aparece no jogo
        out = ls.parse_session(s, name_of)
        self.assertEqual([m["champion_id"] for m in out["enemy_team"]], [266, 0, 0, 0, 0])
        self.assertTrue(out["enemy_team"][0]["locked"])
        self.assertEqual(out["enemy_team"][0]["champion"], "Aatrox")

    def test_aram_bench_and_reroll(self):
        s = session(actions=[], benchChampions=[{"championId": 22}, {"championId": 0}],
                    allowRerolling=True, rerollsRemaining=2)
        out = ls.parse_session(s, name_of)
        self.assertEqual(out["bench"], [22])
        self.assertTrue(out["can_reroll"])
        self.assertEqual(out["rerolls"], 2)

    def test_not_in_team_is_none(self):
        self.assertIsNone(ls.parse_session(session(localPlayerCellId=9), name_of))

    def test_position_normalized(self):
        out = ls.parse_session(session(), name_of)
        self.assertEqual([m["position"] for m in out["my_team"]], ["top", "jungle", "mid", "adc", "support"])


class Helpers(unittest.TestCase):
    def test_best_answers_uses_inverse_winrate(self):
        counters = [{"champion_id": 122, "play": 1789, "win": 833},    # X ganha 46,6% dele -> resposta forte
                    {"champion_id": 24, "play": 2254, "win": 1262},    # X ganha 56% -> fraca
                    {"champion_id": 50, "play": 40, "win": 1}]         # amostra pequena demais
        rows = ls.best_answers(counters, None)
        self.assertEqual([r["champion_id"] for r in rows], [122, 24])
        self.assertAlmostEqual(rows[0]["win_rate"], 1 - 833 / 1789)
        self.assertEqual([r["champion_id"] for r in ls.best_answers(counters, {24})], [24])

    def test_damage_share_prefers_game_data(self):
        self.assertEqual(ls.damage_share({"tacticalInfo": {"damageType": "kMagic"}}, ["fighter"]), 0.0)
        self.assertEqual(ls.damage_share(None, ["marksman"]), 1.0)
        self.assertIsNone(ls.damage_share(None, []))

    def test_team_damage_hint(self):
        self.assertIsNone(ls.team_damage([1], lambda i: 1.0))
        self.assertEqual(ls.team_damage([1, 2, 3], lambda i: 1.0)["hint"], "armadura")
        self.assertEqual(ls.team_damage([1, 2], lambda i: 0.0)["hint"], "resistência mágica")
        self.assertEqual(ls.team_damage([1, 2], lambda i: 0.5)["hint"], "mistura de armadura e RM")

    def test_pick_queues_grouped_like_client(self):
        def q(i, name, mode, group, cat="PvP", **kw):
            return {"id": i, "name": name, "queueAvailability": "Available", "category": cat, "gameMode": mode,
                    "gameSelectModeGroup": group, "isVisible": True, **kw}
        raw = [q(450, "ARAM", "ARAM", "kARAM"),
               q(880, "Iniciante", "SWIFTPLAY", "kSummonersRift", cat="VersusAi"),
               q(1750, "Arena 3x6", "CHERRY", "kAlternativeLeagueGameModes"),
               q(420, "Ranqueada Solo/Duo", "CLASSIC", "kSummonersRift", showPositionSelector=True),
               q(400, "Normal", "CLASSIC", "kSummonersRift", showPositionSelector=True),
               q(700, "Clash", "CLASSIC", "kSummonersRift", isVisible=False),
               q(710, "Ranqueada-5", "CLASSIC", "kSummonersRift"),
               q(1100, "TFT", "TFT", "kTeamfightTactics"),
               q(2000, "Tutorial", "TUTORIAL_MODULE_1", "kAlternativeLeagueGameModes"),
               q(3100, "Personalizado", "CLASSIC", "kAlternativeLeagueGameModes", isCustom=True),
               q(440, "Flex", "CLASSIC", "kSummonersRift", queueAvailability="PlatformDisabled"),
               q(843, "", "SWIFTPLAY", "")]
        out = ls.pick_queues(raw)
        self.assertEqual([(x["id"], x["group"]) for x in out],
                         [(400, "Summoner's Rift"), (420, "Summoner's Rift"), (450, "ARAM"),
                          (1750, "Outros modos"), (880, "Contra bots")])
        self.assertTrue(out[0]["positions"])
        self.assertFalse(out[2]["positions"])

    def test_parse_lobby(self):
        lobby = {"gameConfig": {"queueId": 420, "gameMode": "CLASSIC", "showPositionSelector": True},
                 "localMember": {"puuid": "a", "isLeader": True, "firstPositionPreference": "MIDDLE",
                                 "secondPositionPreference": "UNSELECTED"},
                 "canStartActivity": True,
                 "members": [{"puuid": "a", "summonerName": "Eu", "isLeader": True, "ready": True}]}
        out = ls.parse_lobby(lobby, lambda q: "Solo/Duo")
        self.assertEqual((out["queue"], out["first"], out["second"], out["can_start"]),
                         ("Solo/Duo", "MIDDLE", "", True))
        self.assertTrue(out["members"][0]["me"])
        self.assertIsNone(ls.parse_lobby(None, str))


class FakeLol:
    """Cliente do LoL falso: guarda as chamadas feitas e devolve respostas programadas."""

    def __init__(self, session_data=None, status=204):
        self.calls = []
        self.session = session_data
        self.status = status
        self.builds = None

    async def lcu(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        return httpx.Response(self.status, json={"message": "recusado"} if self.status >= 400 else None)

    async def _json(self, path):
        if path == "/lol-lobby/v2/lobby":
            return getattr(self, "lobby", None)
        return self.session if path.endswith("/session") else None


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Commands(unittest.TestCase):
    def test_hover_patches_my_action(self):
        lol = FakeLol(session())
        run(Control(lol).hover({"champion_id": 103}))
        self.assertEqual(lol.calls, [("PATCH", "/lol-champ-select/v1/session/actions/1", {"championId": 103})])

    def test_hover_without_session_or_action(self):
        with self.assertRaises(CommandError):
            run(Control(FakeLol(None)).hover({"champion_id": 103}))
        done = session(actions=[[act(1, 2, "ban", 157, done=True)]])
        with self.assertRaises(CommandError):
            run(Control(FakeLol(done)).hover({"champion_id": 103}))
        with self.assertRaises(CommandError):
            run(Control(FakeLol(session())).hover({"champion_id": "x"}))

    def test_lock_completes_in_progress_action(self):
        s = session()
        s["actions"][0][0]["championId"] = 157
        lol = FakeLol(s)
        self.assertEqual(run(Control(lol).lock()), "ban")
        self.assertEqual(lol.calls, [("POST", "/lol-champ-select/v1/session/actions/1/complete", None)])

    def test_lock_sets_champion_then_completes(self):
        lol = FakeLol(session())
        run(Control(lol).lock({"champion_id": 157}))
        self.assertEqual([c[0] for c in lol.calls], ["PATCH", "POST"])
        self.assertEqual(lol.calls[0][2], {"championId": 157})

    def test_lock_needs_turn_and_champion(self):
        with self.assertRaises(CommandError):   # sem ação em andamento
            run(Control(FakeLol(session(actions=[[act(10, 2, "pick")]]))).lock())
        with self.assertRaises(CommandError):   # nada marcado
            run(Control(FakeLol(session())).lock())

    def test_lobby_commands(self):
        lol = FakeLol()
        c = Control(lol)
        run(c.create_lobby({"queue_id": 420}))
        run(c.set_positions({"first": "middle", "second": "bottom"}))
        run(c.set_positions({"first": "top", "second": "top"}))
        run(c.search())
        run(c.cancel_search())
        self.assertEqual(lol.calls, [
            ("POST", "/lol-lobby/v2/lobby", {"queueId": 420}),
            ("PUT", "/lol-lobby/v2/lobby/members/localMember/position-preferences",
             {"firstPreference": "MIDDLE", "secondPreference": "BOTTOM"}),
            ("PUT", "/lol-lobby/v2/lobby/members/localMember/position-preferences",
             {"firstPreference": "TOP", "secondPreference": "UNSELECTED"}),
            ("POST", "/lol-lobby/v2/lobby/matchmaking/search", None),
            ("DELETE", "/lol-lobby/v2/lobby/matchmaking/search", None)])
        with self.assertRaises(CommandError):
            run(c.set_positions({"first": "bard"}))

    def test_change_queue_in_party_keeps_group(self):
        lol = FakeLol()
        lol.lobby = {"gameConfig": {"queueId": 420}, "members": [{"puuid": "a"}, {"puuid": "b"}],
                     "localMember": {"isLeader": True}}
        c = Control(lol)
        run(c.create_lobby({"queue_id": 420}))    # mesmo modo: nada a fazer
        self.assertEqual(lol.calls, [])
        run(c.create_lobby({"queue_id": 450}))
        self.assertEqual(lol.calls, [("POST", "/lol-lobby/v2/lobby", {"queueId": 450})])   # sem DELETE

    def test_change_queue_refused_in_party(self):
        lol = FakeLol(status=400)
        lol.lobby = {"gameConfig": {"queueId": 420}, "members": [{}, {}], "localMember": {"isLeader": False}}
        with self.assertRaises(CommandError) as cm:
            run(Control(lol).create_lobby({"queue_id": 450}))
        self.assertIn("líder", str(cm.exception))
        self.assertEqual([c[0] for c in lol.calls], ["POST"])

    def test_leave_requires_confirm(self):
        lol = FakeLol()
        with self.assertRaises(CommandError):
            run(Control(lol).leave_lobby())
        self.assertEqual(lol.calls, [])
        run(Control(lol).leave_lobby({"confirm": True}))
        self.assertEqual(lol.calls, [("DELETE", "/lol-lobby/v2/lobby", None)])

    def test_client_error_becomes_command_error(self):
        with self.assertRaises(CommandError) as cm:
            run(Control(FakeLol(status=400)).search())
        self.assertIn("recusado", str(cm.exception))

    def test_spells_partial_and_invalid(self):
        lol = FakeLol()
        run(Control(lol).set_spells({"spell2": 4}))
        self.assertEqual(lol.calls[0][2], {"spell2Id": 4})
        with self.assertRaises(CommandError):
            run(Control(lol).set_spells({}))


if __name__ == "__main__":
    unittest.main()
