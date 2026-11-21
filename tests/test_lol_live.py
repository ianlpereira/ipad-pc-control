"""Testes do analisador ao vivo. Rodar: python -m unittest discover -s tests -v (da raiz do projeto)."""
import unittest

from server.modules.lol_live import LiveAnalyzer, DRAGON_FIRST, BARON_FIRST, INHIB_RESPAWN


class FakeMeta:
    ITEMS = {1: (1000, [], ["Damage"]), 2: (900, [], ["SpellDamage"]), 3: (3000, [1], ["Damage"]),
             4: (300, [], [])}
    CHAMPS = {"ahri": {"id": 103, "name": "Ahri", "roles": ["mage", "assassin"]},
              "garen": {"id": 86, "name": "Garen", "roles": ["fighter", "tank"]}}

    def item_price(self, i): return self.ITEMS.get(i, (0, [], []))[0]
    def item_from(self, i): return self.ITEMS.get(i, (0, [], []))[1]
    def item_cats(self, i): return self.ITEMS.get(i, (0, [], []))[2]
    def item_icon(self, i): return ""
    def champ_by_alias(self, a): return self.CHAMPS.get(a.lower())


def player(name, team, pos, champ="Ahri", k=0, d=0, a=0, cs=0, items=(), dead=False, respawn=0):
    return {"summonerName": name, "riotIdGameName": name, "riotId": f"{name}#BR1", "championName": champ,
            "rawChampionName": f"game_character_displayname_{champ}", "team": team, "position": pos,
            "level": 10, "isDead": dead, "respawnTimer": respawn, "isBot": False,
            "scores": {"kills": k, "deaths": d, "assists": a, "creepScore": cs, "wardScore": 5},
            "items": [{"itemID": i, "price": p, "count": 1, "slot": s, "displayName": f"i{i}", "consumable": False}
                      for s, (i, p) in enumerate(items)]}


def game(t=600.0, events=(), me_gold=500.0, **over):
    me = player("Eu", "ORDER", "MIDDLE", cs=70, k=2, d=1, a=3, items=[(1, 1000)])
    opp = player("Rival", "CHAOS", "MIDDLE", champ="Garen", cs=60, k=1, d=2, items=[(2, 900)])
    d = {"gameData": {"gameTime": t, "gameMode": "CLASSIC"},
         "activePlayer": {"summonerName": "Eu", "riotId": "Eu#BR1", "riotIdGameName": "Eu",
                          "currentGold": me_gold, "championStats": {"armor": 50.0}},
         "allPlayers": [me, opp], "events": {"Events": list(events)}}
    d.update(over)
    return d


def ev(eid, name, t, **kw):
    return {"EventID": eid, "EventName": name, "EventTime": t, **kw}


class LiveAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.an = LiveAnalyzer()
        self.meta = FakeMeta()

    def test_basic_pace_and_lane(self):
        out = self.an.update(game(t=600), self.meta)
        self.assertEqual(out["pace"]["cs_per_min"], 7.0)
        self.assertEqual(out["pace"]["cs_delta"], 0)            # benchmark mid 7.0 -> 70 cs aos 10 min
        self.assertEqual(out["pace"]["cs10"], 70)
        self.assertEqual(out["lane"]["opponent"]["name"], "Garen")
        self.assertEqual(out["lane"]["diff"]["cs"], 10)
        self.assertEqual(out["teams"]["value_diff"], 100)       # 1000 - 900

    def test_no_division_by_zero_at_start(self):
        out = self.an.update(game(t=0), self.meta)
        self.assertEqual(out["pace"]["cs_per_min"], 0.0)
        self.assertIsNone(out["pace"]["gold_per_min"])
        self.assertIsNone(out["pace"]["cs_delta"])

    def test_returns_none_without_me(self):
        g = game()
        g["activePlayer"] = {"summonerName": "Ninguem"}
        self.assertIsNone(self.an.update(g, self.meta))

    def test_works_without_meta(self):
        out = self.an.update(game(), None)
        self.assertIsNone(out["teams"]["threat"])
        self.assertEqual(out["teams"]["comp"], {"ally": {}, "enemy": {}})

    def test_aram_has_no_lane_or_objectives(self):
        g = game()
        g["gameData"]["gameMode"] = "ARAM"
        for p in g["allPlayers"]:
            p["position"] = ""
        out = self.an.update(g, self.meta)
        self.assertIsNone(out["lane"])
        self.assertIsNone(out["objectives"])
        self.assertIsNone(out["pace"]["benchmark"])

    def test_comp_and_threat(self):
        g = game()
        g["allPlayers"][1]["items"] = [
            {"itemID": 3, "price": 3000, "count": 1, "slot": 0, "displayName": "x", "consumable": False}]
        out = self.an.update(g, self.meta)
        self.assertEqual(out["teams"]["comp"]["ally"], {"mage": 1, "assassin": 1})
        self.assertEqual(out["teams"]["threat"]["hint"], "armadura")

    def test_objectives_from_events(self):
        evs = [ev(1, "DragonKill", 400, KillerName="Eu", DragonType="Fire"),
               ev(2, "DragonKill", 710, KillerName="Rival", DragonType="Water"),
               ev(3, "TurretKilled", 800, KillerName="Eu", TurretKilled="Turret_T2_L_03_A"),
               ev(4, "InhibKilled", 900, KillerName="Rival", InhibKilled="Barracks_T1_L1")]
        o = self.an.update(game(t=950, events=evs), self.meta)["objectives"]
        self.assertEqual(o["dragon"]["ally"], ["Fire"])
        self.assertEqual(o["dragon"]["enemy"], ["Water"])
        self.assertEqual(o["dragon"]["next_at"], 710 + 300)
        self.assertEqual(o["towers"], {"ally": 1, "enemy": 0})
        self.assertEqual(o["inhibs"], [{"side": "ally", "back_at": 900 + INHIB_RESPAWN}])

    def test_first_spawn_defaults(self):
        o = self.an.update(game(t=100), self.meta)["objectives"]
        self.assertEqual(o["dragon"]["next_at"], DRAGON_FIRST)
        self.assertEqual(o["baron"]["next_at"], BARON_FIRST)

    def test_soul_detection(self):
        evs = [ev(i, "DragonKill", 300 + i * 310, KillerName="Eu", DragonType="Fire") for i in range(4)]
        o = self.an.update(game(t=2000, events=evs), self.meta)["objectives"]
        self.assertEqual(o["dragon"]["soul"], "ally")
        self.assertTrue(o["dragon"]["next_is_elder"])

    def test_feed_newest_first_and_text(self):
        evs = [ev(1, "ChampionKill", 100, KillerName="Eu", VictimName="Rival"),
               ev(2, "Ace", 200, AcingTeam="CHAOS")]
        feed = self.an.update(game(t=250, events=evs), self.meta)["events"]
        self.assertEqual(feed[0]["kind"], "ace")
        self.assertEqual(feed[0]["side"], "enemy")
        self.assertEqual(feed[1]["text"], "Ahri abateu Garen")
        self.assertEqual(feed[1]["side"], "ally")

    def test_no_alert_for_past_events_on_first_update(self):
        evs = [ev(1, "BaronKill", 1300, KillerName="Rival")]
        self.an.update(game(t=1400, events=evs), self.meta)
        self.assertEqual(self.an.pop_alerts(), [])

    def test_alert_once_for_new_enemy_baron(self):
        self.an.update(game(t=1300), self.meta)
        g = game(t=1310, events=[ev(1, "BaronKill", 1305, KillerName="Rival")])
        self.an.update(g, self.meta)
        self.an.update(g, self.meta)       # mesmo evento de novo: sem alerta repetido
        alerts = self.an.pop_alerts()
        self.assertEqual([a["kind"] for a in alerts], ["objective"])
        self.assertIn("Barão", alerts[0]["title"])

    def test_objective_countdown_alert_once(self):
        self.an.update(game(t=275), self.meta)
        self.an.update(game(t=280), self.meta)
        alerts = [a for a in self.an.pop_alerts() if "Dragão" in a["title"]]
        self.assertEqual(len(alerts), 1)

    def test_disabled_alert_kinds_are_filtered(self):
        self.an.update(game(t=500), self.meta)
        g = game(t=510)
        g["allPlayers"][0]["isDead"] = True
        g["allPlayers"][0]["respawnTimer"] = 20
        self.an.update(g, self.meta)
        self.assertEqual([a for a in self.an.pop_alerts() if a["kind"] == "death"], [])  # death desligado por padrão
        an = LiveAnalyzer({"alerts": {"death": True}})
        an.update(game(t=500), self.meta)
        an.update(g, self.meta)
        self.assertEqual([a["kind"] for a in an.pop_alerts()], ["death"])

    def test_idle_gold_alert_after_delay_only_once(self):
        self.an.update(game(t=500, me_gold=1500), self.meta)
        self.assertEqual(self.an.pop_alerts(), [])
        self.an.update(game(t=525, me_gold=1500), self.meta)
        self.an.update(game(t=540, me_gold=1600), self.meta)
        self.assertEqual([a["kind"] for a in self.an.pop_alerts()], ["idle_gold"])

    def test_next_item_uses_owned_components(self):
        build = {"core": [{"id": 3, "name": "Item 3", "icon": ""}]}
        nxt = self.an.update(game(me_gold=1500), self.meta, build)["me"]["next_item"]
        self.assertEqual(nxt["cost"], 2000)        # 3000 - componente 1000 que já tem
        self.assertEqual(nxt["missing"], 500)

    def test_reset_when_game_time_goes_back(self):
        self.an.update(game(t=900), self.meta)
        self.an.update(game(t=20), self.meta)
        self.assertEqual(self.an.update(game(t=25), self.meta)["series"]["t"], [20])

    def test_series_is_bounded(self):
        for t in range(0, 4000, 10):
            out = self.an.update(game(t=float(t)), self.meta)
        self.assertLessEqual(len(out["series"]["t"]), 181)


if __name__ == "__main__":
    unittest.main()
