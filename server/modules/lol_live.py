"""Análise ao vivo da partida a partir da Live Client Data API.

Módulo puro: recebe o dict de `/liveclientdata/allgamedata` e devolve o painel
rico (ritmo, rota, times, objetivos, eventos, séries) mais alertas pendentes.
Sem I/O, então dá para testar com fixtures.

Só usa informação que o próprio jogo publica a todos (placar/itens da tela Tab e
eventos públicos). Não há cooldowns de feitiços inimigos nem rastreio de selva.
Valores derivados (valor de itens, ameaça AD/AP) são ESTIMATIVAS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- Tempos de objetivos (segundos de jogo). VALIDAR contra o patch atual: mudam entre patches. ---
DRAGON_FIRST = 300.0
DRAGON_RESPAWN = 300.0
BARON_FIRST = 1200.0
BARON_RESPAWN = 360.0
BARON_BUFF = 180.0
INHIB_RESPAWN = 300.0
OBJECTIVE_WARN = 30.0      # avisa quando falta isso para o objetivo nascer

SAMPLE_EVERY = 10.0        # intervalo da série temporal
MAX_POINTS = 180
FEED_SIZE = 15
SOUL_AT = 4

DEFAULT_BENCHMARKS = {"top": 7.0, "jungle": 5.0, "mid": 7.0, "adc": 7.5, "support": 1.5}
DEFAULT_ALERTS = {"objective": True, "idle_gold": True, "death": False, "ace": False, "inhib": False}

POS_MAP = {"top": "top", "jungle": "jungle", "middle": "mid", "mid": "mid", "bottom": "adc", "adc": "adc",
           "utility": "support", "support": "support"}
POS_ORDER = ["top", "jungle", "mid", "adc", "support"]
DRAGON_NAMES = {"Fire": "Infernal", "Earth": "Montanha", "Water": "Oceano", "Air": "Nuvem",
                "Hextech": "Hextec", "Chemtech": "Químico", "Elder": "Ancião"}
AD_CATS = {"Damage", "CriticalStrike", "ArmorPenetration"}
AP_CATS = {"SpellDamage", "MagicPenetration"}
MULTI = {2: "duplo abate", 3: "triplo abate", 4: "quádruplo abate", 5: "PENTA ABATE"}
TEAM_RE = re.compile(r"_T(\d)_")
OTHER = {"ally": "enemy", "enemy": "ally"}


@dataclass
class LiveConfig:
    benchmarks: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BENCHMARKS))
    alerts: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_ALERTS))
    idle_gold_threshold: int = 1300
    idle_gold_seconds: float = 20.0

    @classmethod
    def from_dict(cls, c: dict | None) -> "LiveConfig":
        c = c or {}
        return cls(
            benchmarks={**DEFAULT_BENCHMARKS, **(c.get("benchmarks") or {})},
            alerts={**DEFAULT_ALERTS, **(c.get("alerts") or {})},
            idle_gold_threshold=int(c.get("idle_gold_threshold", 1300)),
            idle_gold_seconds=float(c.get("idle_gold_seconds", 20)),
        )


def norm_position(p: Any) -> str:
    return POS_MAP.get(str(p or "").lower(), "")


def _ids(p: dict) -> set[str]:
    out = {p.get("riotId"), p.get("summonerName"), p.get("riotIdGameName")}
    out |= {s.split("#", 1)[0] for s in list(out) if isinstance(s, str)}
    return {s for s in out if s}


class LiveAnalyzer:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = LiveConfig.from_dict(cfg)
        self.reset()

    # ------------------------------------------------------------------ estado
    def reset(self) -> None:
        self._seen: set[int] = set()
        self._baseline = False
        self._last_t = -1.0
        self._series: list[tuple[float, int, int, int, int, int]] = []  # t, cs, ally_val, enemy_val, ally_k, enemy_k
        self._sample_every = SAMPLE_EVERY
        self._next_sample = 0.0
        self._snap: dict[str, int] = {}
        self._pending: list[dict] = []
        self._alerted: set[str] = set()
        self._was_dead = False
        self._idle_since: float | None = None
        self._idle_alerted = False

    def pop_alerts(self) -> list[dict]:
        out = [a for a in self._pending if self.cfg.alerts.get(a["kind"], False)]
        self._pending = []
        return out

    def _alert(self, kind: str, key: str, title: str, body: str = "", level: str = "info", sound: bool = False) -> None:
        if key in self._alerted:
            return
        self._alerted.add(key)
        self._pending.append({"kind": kind, "key": key, "title": title, "body": body, "level": level, "sound": sound})

    # ------------------------------------------------------------------ entrada
    def update(self, data: dict, meta: Any = None, build: dict | None = None) -> dict | None:
        """Devolve o painel ao vivo ou None se os dados forem inúteis."""
        gd = data.get("gameData") or {}
        t = float(gd.get("gameTime", 0) or 0)
        if t < self._last_t - 5:   # nova partida (ou replay): recomeça
            self.reset()
        self._last_t = t

        players = data.get("allPlayers") or []
        ap = data.get("activePlayer") or {}
        me = self._find_me(ap, players)
        if not players or me is None:
            return None
        my_team = me.get("team", "ORDER")
        side_of_team = lambda team: "ally" if team == my_team else "enemy"  # noqa: E731
        by_name: dict[str, dict] = {}
        for p in players:
            for i in _ids(p):
                by_name[i] = p

        events = ((data.get("events") or {}).get("Events")) or []
        mode = str(gd.get("gameMode") or "CLASSIC")
        rows = [(p, self._player(p, meta, p is me)) for p in players]
        mine = next(s for p, s in rows if p is me)
        ally = [s for p, s in rows if p.get("team") == my_team]
        enemy = [s for p, s in rows if p.get("team") != my_team]
        for lst in (ally, enemy):
            lst.sort(key=lambda x: POS_ORDER.index(x["position"]) if x["position"] in POS_ORDER else 9)
        ally_val, enemy_val = sum(x["items_value"] for x in ally), sum(x["items_value"] for x in enemy)
        ally_k, enemy_k = sum(x["k"] for x in ally), sum(x["k"] for x in enemy)

        ctx = {"t": t, "by_name": by_name, "my_team": my_team, "side": side_of_team}
        objectives = self._objectives(events, ctx) if mode == "CLASSIC" else None
        feed = self._feed(events, ctx)
        self._process_events(events, ctx, objectives)
        self._sample(t, mine["cs"], ally_val, enemy_val, ally_k, enemy_k)

        pace = self._pace(t, mine, ap, me, ally_val)
        out = {
            "me": self._me(t, ap, me, mine, meta, build),
            "pace": pace,
            "lane": self._lane(mine, enemy),
            "teams": {
                "ally": {"kills": ally_k, "items_value": ally_val, "players": ally,
                         **self._team_extra(objectives, "ally")},
                "enemy": {"kills": enemy_k, "items_value": enemy_val, "players": enemy,
                          **self._team_extra(objectives, "enemy")},
                "value_diff": ally_val - enemy_val,
                "value_trend": self._trend(),
                "threat": self._threat(players, my_team, meta),
                "comp": {"ally": self._comp(ally), "enemy": self._comp(enemy)},
            },
            "objectives": objectives,
            "events": feed,
            "series": self._series_out(),
            "t": round(t),
        }
        self._alerts_me(t, me, mine, out["me"])
        return out

    @staticmethod
    def _find_me(ap: dict, players: list[dict]) -> dict | None:
        mine = {ap.get("riotId"), ap.get("summonerName"), ap.get("riotIdGameName")} - {None, ""}
        return next((p for p in players if _ids(p) & mine), None)

    # ------------------------------------------------------------------ jogadores
    @staticmethod
    def _price(i: dict, meta: Any) -> int:
        """Preço TOTAL do item. O campo `price` da Live Client API é só o custo de montagem
        (Gume do Infinito vem 725, não 3500), então o total vem do items.json do cliente."""
        total = meta.item_price(int(i.get("itemID", 0))) if meta else 0
        return int(total or i.get("price", 0) or 0)

    def _item_value(self, items: list[dict], meta: Any) -> int:
        return sum(self._price(i, meta) * int(i.get("count", 1) or 1) for i in items if not i.get("consumable"))

    def _player(self, p: dict, meta: Any, is_me: bool) -> dict:
        sc = p.get("scores") or {}
        items = p.get("items") or []
        raw = str(p.get("rawChampionName") or "")
        champ = meta.champ_by_alias(raw.rsplit("_", 1)[-1]) if meta and raw else None
        return {
            "name": p.get("championName", ""), "champion_id": (champ or {}).get("id"),
            "level": int(p.get("level", 0) or 0), "position": norm_position(p.get("position")),
            "k": int(sc.get("kills", 0)), "d": int(sc.get("deaths", 0)), "a": int(sc.get("assists", 0)),
            "cs": int(sc.get("creepScore", 0)), "ward": round(float(sc.get("wardScore", 0) or 0)),
            "dead": bool(p.get("isDead")), "respawn": round(float(p.get("respawnTimer", 0) or 0)),
            "items": [{"id": int(i.get("itemID", 0)), "name": i.get("displayName", ""),
                       "icon": meta.item_icon(int(i.get("itemID", 0))) if meta else ""}
                      for i in items if int(i.get("slot", 0)) < 6],
            "roles": list((champ or {}).get("roles") or []),
            "items_value": self._item_value(items, meta),
            "me": is_me, "bot": bool(p.get("isBot")),
        }

    # ------------------------------------------------------------------ meu estado / ritmo / rota
    def _me(self, t: float, ap: dict, me: dict, mine: dict, meta: Any, build: dict | None) -> dict:
        stats = ap.get("championStats") or {}
        pick = ("attackDamage", "abilityPower", "armor", "magicResist", "attackSpeed", "critChance",
                "abilityHaste", "moveSpeed", "physicalLethality", "magicLethality", "lifeSteal", "omnivamp",
                "maxHealth", "currentHealth", "tenacity")
        gold = float(ap.get("currentGold", 0) or 0)
        return {
            "stats": {k: round(float(stats.get(k, 0) or 0), 2) for k in pick if k in stats},
            "gold": int(gold), "dead": mine["dead"], "respawn": mine["respawn"],
            "idle_gold": {"active": gold >= self.cfg.idle_gold_threshold, "threshold": self.cfg.idle_gold_threshold},
            "next_item": self._next_item(gold, mine, meta, build),
        }

    @staticmethod
    def _effective_cost(item_id: int, owned: list[int], meta: Any) -> int | None:
        total = meta.item_price(item_id)
        if not total:
            return None
        pool = list(owned)
        for comp in meta.item_from(item_id):   # componentes diretos que você já tem abatem o preço
            if comp in pool:
                pool.remove(comp)
                total -= meta.item_price(comp) or 0
        return max(0, total)

    def _next_item(self, gold: float, mine: dict, meta: Any, build: dict | None) -> dict | None:
        if not build:
            return None
        owned_ids = [i["id"] for i in mine["items"]]
        nxt = next((i for i in build.get("core") or [] if i.get("id") not in set(owned_ids)), None)
        if nxt is None:
            return None
        cost = self._effective_cost(nxt["id"], owned_ids, meta) if meta else None
        return {"id": nxt["id"], "name": nxt.get("name", ""), "icon": nxt.get("icon", ""), "cost": cost,
                "missing": None if cost is None else max(0, int(cost - gold))}

    def _pace(self, t: float, mine: dict, ap: dict, me: dict, ally_val: int) -> dict:
        minutes = t / 60
        cs = mine["cs"]
        bench = self.cfg.benchmarks.get(mine["position"]) if mine["position"] else None
        for mark, key in ((600, "cs10"), (900, "cs15")):
            if key not in self._snap and mark <= t < mark + 30:   # só se acompanhou desde o início
                self._snap[key] = cs
        own_value = mine["items_value"]
        gold = float(ap.get("currentGold", 0) or 0)
        return {
            "cs": cs,
            "cs_per_min": round(cs / minutes, 1) if t > 60 else 0.0,
            "benchmark": bench,
            "cs_delta": round(cs - bench * minutes) if bench and t >= 180 else None,
            "cs10": self._snap.get("cs10"), "cs15": self._snap.get("cs15"),
            "gold_per_min": round(max(0.0, own_value + gold - 500) / minutes) if t > 120 else None,  # estimativa
            "position": mine["position"],
        }

    @staticmethod
    def _lane(mine: dict, enemy: list[dict]) -> dict | None:
        if not mine["position"]:
            return None
        opp = next((e for e in enemy if e["position"] == mine["position"]), None)
        if opp is None:
            return None
        kda = lambda x: round((x["k"] + x["a"]) / max(1, x["d"]), 1)  # noqa: E731
        return {"opponent": opp, "me": mine,
                "diff": {"level": mine["level"] - opp["level"], "cs": mine["cs"] - opp["cs"],
                         "items": mine["items_value"] - opp["items_value"],
                         "kda": round(kda(mine) - kda(opp), 1)}}

    # ------------------------------------------------------------------ times
    @staticmethod
    def _comp(team: list[dict]) -> dict[str, int]:
        roles: dict[str, int] = {}
        for p in team:
            for r in p.get("roles") or []:
                roles[r] = roles.get(r, 0) + 1
        return roles

    @staticmethod
    def _team_extra(obj: dict | None, side: str) -> dict:
        if not obj:
            return {"towers": 0, "dragons": [], "heralds": 0, "grubs": 0, "barons": 0}
        return {"towers": obj["towers"][side], "dragons": obj["dragon"][side], "heralds": obj["herald"][side],
                "grubs": obj["grubs"][side], "barons": obj["baron"]["count"][side]}

    def _threat(self, players: list[dict], my_team: str, meta: Any) -> dict | None:
        if meta is None:
            return None
        ad = ap = 0
        for p in players:
            if p.get("team") == my_team:
                continue
            for i in p.get("items") or []:
                if i.get("consumable"):
                    continue
                cats = set(meta.item_cats(int(i.get("itemID", 0))))
                price = self._price(i, meta) * int(i.get("count", 1) or 1)
                if cats & AD_CATS:
                    ad += price
                if cats & AP_CATS:
                    ap += price
        total = ad + ap
        if total < 3000:    # pouco dado: não opinar
            return None
        share = ad / total
        hint = "armadura" if share >= 0.65 else "resistência mágica" if share <= 0.35 else "mistura de armadura e RM"
        return {"ad_share": round(share, 2), "hint": hint, "estimate": True}

    # ------------------------------------------------------------------ objetivos
    def _team_of_event(self, name: Any, ctx: dict) -> str | None:
        p = ctx["by_name"].get(str(name or ""))
        return p.get("team") if p else None

    def _objectives(self, events: list[dict], ctx: dict) -> dict:
        """Recalculado a cada tick a partir da lista completa de eventos (sem estado)."""
        t, side = ctx["t"], ctx["side"]
        dragon: dict[str, list[str]] = {"ally": [], "enemy": []}
        herald = {"ally": 0, "enemy": 0}
        grubs = {"ally": 0, "enemy": 0}
        towers = {"ally": 0, "enemy": 0}
        barons: list[tuple[float, str]] = []
        inhibs: dict[str, tuple[str, float]] = {}
        last_drag = None
        for e in events:
            name, et = e.get("EventName"), float(e.get("EventTime", 0) or 0)
            killer_team = self._team_of_event(e.get("KillerName"), ctx)
            ks = side(killer_team) if killer_team else None
            if name == "DragonKill" and ks:
                dragon[ks].append(str(e.get("DragonType") or ""))
                last_drag = et
            elif name == "HeraldKill" and ks:
                herald[ks] += 1
            elif name == "HordeKill" and ks:
                grubs[ks] += 1
            elif name == "BaronKill" and ks:
                barons.append((et, ks))
            elif name == "TurretKilled":
                m = TEAM_RE.search(str(e.get("TurretKilled") or ""))
                if m:
                    towers[OTHER[side("ORDER" if m.group(1) == "1" else "CHAOS")]] += 1
            elif name == "InhibKilled":
                key = str(e.get("InhibKilled") or "")
                m = TEAM_RE.search(key)
                if m:
                    inhibs[key] = (side("ORDER" if m.group(1) == "1" else "CHAOS"), et + INHIB_RESPAWN)
            elif name == "InhibRespawned":
                inhibs.pop(str(e.get("InhibRespawned") or ""), None)

        normal = {s: [d for d in dragon[s] if d != "Elder"] for s in dragon}
        soul = next((s for s in ("ally", "enemy") if len(normal[s]) >= SOUL_AT), None)
        next_drag = (last_drag + DRAGON_RESPAWN) if last_drag is not None else DRAGON_FIRST
        next_baron = (barons[-1][0] + BARON_RESPAWN) if barons else BARON_FIRST
        baron_buff = None
        if barons and t < barons[-1][0] + BARON_BUFF:
            baron_buff = {"side": barons[-1][1], "until": barons[-1][0] + BARON_BUFF}
        return {
            "dragon": {**dragon, "next_at": next_drag, "next_is_elder": soul is not None, "soul": soul,
                       "names": DRAGON_NAMES},
            "baron": {"count": {s: sum(1 for _, x in barons if x == s) for s in ("ally", "enemy")},
                      "next_at": next_baron, "buff": baron_buff},
            "herald": herald, "grubs": grubs, "towers": towers,
            "inhibs": [{"side": s, "back_at": b} for s, b in inhibs.values() if b > t],
        }

    # ------------------------------------------------------------------ eventos
    def _champ_of(self, name: Any, ctx: dict) -> str:
        p = ctx["by_name"].get(str(name or ""))
        return (p or {}).get("championName") or ("Servo" if str(name or "").startswith("Minion") else
                                                 "Torre" if str(name or "").startswith("Turret") else "Neutro")

    def _feed(self, events: list[dict], ctx: dict) -> list[dict]:
        side, out = ctx["side"], []
        for e in events:
            n, et = e.get("EventName"), round(float(e.get("EventTime", 0) or 0))
            kt = self._team_of_event(e.get("KillerName"), ctx)
            ks = side(kt) if kt else "neutral"
            kill = self._champ_of(e.get("KillerName"), ctx)
            if n == "FirstBlood":
                out.append({"t": et, "kind": "first_blood", "side": ks,
                            "text": f"Primeiro sangue: {self._champ_of(e.get('Recipient'), ctx)}"})
            elif n == "ChampionKill":
                out.append({"t": et, "kind": "kill", "side": ks,
                            "text": f"{kill} abateu {self._champ_of(e.get('VictimName'), ctx)}"})
            elif n == "Multikill":
                out.append({"t": et, "kind": "multikill", "side": ks,
                            "text": f"{kill}: {MULTI.get(int(e.get('KillStreak', 0) or 0), 'sequência de abates')}"})
            elif n == "Ace":
                team = e.get("AcingTeam")
                out.append({"t": et, "kind": "ace", "side": side(team) if team else "neutral", "text": "ACE"})
            elif n == "DragonKill":
                d = DRAGON_NAMES.get(str(e.get("DragonType")), "Dragão")
                steal = " (roubado)" if e.get("Stolen") in (True, "True") else ""
                out.append({"t": et, "kind": "dragon", "side": ks, "text": f"Dragão {d}{steal}: {kill}"})
            elif n == "HeraldKill":
                out.append({"t": et, "kind": "herald", "side": ks, "text": f"Arauto: {kill}"})
            elif n == "HordeKill":
                out.append({"t": et, "kind": "grub", "side": ks, "text": f"Vastilarva: {kill}"})
            elif n == "BaronKill":
                out.append({"t": et, "kind": "baron", "side": ks, "text": f"Barão: {kill}"})
            elif n in ("TurretKilled", "InhibKilled"):
                key = str(e.get(n) or "")
                m = TEAM_RE.search(key)
                if m:
                    lost = side("ORDER" if m.group(1) == "1" else "CHAOS")
                    taker = OTHER[lost]
                    out.append({"t": et, "kind": "turret" if n == "TurretKilled" else "inhib", "side": taker,
                                "text": "Torre derrubada" if n == "TurretKilled" else "Inibidor derrubado"})
        return out[-FEED_SIZE:][::-1]

    # ------------------------------------------------------------------ alertas (só eventos novos)
    def _process_events(self, events: list[dict], ctx: dict, obj: dict | None) -> None:
        first = not self._baseline
        new = [e for e in events if int(e.get("EventID", -1)) not in self._seen]
        for e in events:
            self._seen.add(int(e.get("EventID", -1)))
        self._baseline = True
        t = ctx["t"]
        if obj:   # contagem regressiva: avisa uma vez por spawn
            d, b = obj["dragon"], obj["baron"]
            for label, at in (("Ancião" if d["next_is_elder"] else "Dragão", d["next_at"]), ("Barão", b["next_at"])):
                if 0 < at - t <= OBJECTIVE_WARN:
                    self._alert("objective", f"soon:{label}:{int(at)}", f"{label} nasce em {int(at - t)}s",
                                "Prepare a visão", "warn", True)
        if first:     # servidor ligou no meio da partida: não alerta o passado
            return
        side = ctx["side"]
        for e in new:
            n = e.get("EventName")
            kt = self._team_of_event(e.get("KillerName"), ctx)
            ks = side(kt) if kt else None
            eid = e.get("EventID")
            if n == "BaronKill" and ks == "enemy":
                self._alert("objective", f"baron:{eid}", "Inimigo pegou o Barão", "", "alert", True)
            elif n == "DragonKill" and ks == "enemy" and obj and obj["dragon"]["soul"] == "enemy":
                self._alert("objective", f"soul:{eid}", "Alma de dragão inimiga", "", "alert", True)
            elif n == "Ace":
                self._alert("ace", f"ace:{eid}", "ACE", "", "info")
            elif n == "InhibKilled":
                m = TEAM_RE.search(str(e.get("InhibKilled") or ""))
                if m:
                    lost = side("ORDER" if m.group(1) == "1" else "CHAOS")
                    self._alert("inhib", f"inhib:{eid}",
                                "Inibidor inimigo derrubado" if lost == "enemy" else "Inibidor aliado caiu", "", "info")

    def _alerts_me(self, t: float, me: dict, mine: dict, st: dict) -> None:
        if mine["dead"] and not self._was_dead:
            self._alert("death", f"death:{int(t)}", "Você morreu", f"Volta em {mine['respawn']}s")
        self._was_dead = mine["dead"]
        gold, thr = st["gold"], self.cfg.idle_gold_threshold
        if gold < thr * 0.6:
            self._idle_since, self._idle_alerted = None, False
        elif gold >= thr and not mine["dead"]:
            if self._idle_since is None:
                self._idle_since = t
            if t - self._idle_since >= self.cfg.idle_gold_seconds and not self._idle_alerted:
                self._idle_alerted = True
                nxt = st.get("next_item")
                body = f"Próximo: {nxt['name']}" if nxt else "Compre itens"
                self._pending.append({"kind": "idle_gold", "key": f"idle:{int(t)}", "title": f"{gold} de ouro parado",
                                      "body": body, "level": "warn", "sound": False})

    # ------------------------------------------------------------------ série temporal
    def _sample(self, t: float, cs: int, ally_val: int, enemy_val: int, ak: int, ek: int) -> None:
        if t < self._next_sample:
            return
        self._series.append((round(t), cs, ally_val, enemy_val, ak, ek))
        self._next_sample = t + self._sample_every
        if len(self._series) > MAX_POINTS:     # decima: mantém a cobertura, perde resolução
            self._series = self._series[::2]
            self._sample_every *= 2

    def _series_out(self) -> dict:
        s = self._series
        return {"t": [x[0] for x in s], "cs": [x[1] for x in s],
                "value_diff": [x[2] - x[3] for x in s], "kill_diff": [x[4] - x[5] for x in s]}

    def _trend(self) -> int | None:
        """Variação da diferença de valor de itens nos últimos ~2 min."""
        s = self._series
        if len(s) < 2:
            return None
        cur = s[-1]
        past = next((x for x in reversed(s) if x[0] <= cur[0] - 120), None)
        return None if past is None else (cur[2] - cur[3]) - (past[2] - past[3])
