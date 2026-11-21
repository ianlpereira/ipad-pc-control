"""Placar ao vivo do Flamengo (ou de outro time, via `flamengo.team_id`).

Fonte: API pública do ESPN (site.api.espn.com), sem chave. NÃO é oficial e pode mudar sem
aviso: nesse caso o painel mostra o último valor e um aviso, e o resto continua funcionando.

- `schedule?fixture=true`: próximos jogos e o jogo em andamento (todas as competições).
- `schedule`: resultados passados.
- `{liga}/summary?event=ID`: lances, estatísticas, escalações, narração e tabela.

O poll roda em segundo plano com cadência adaptativa; `state()` só devolve o cache.
Os parsers são funções puras (testáveis com os JSONs reais em tests/fixtures).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .notify import notifier

log = logging.getLogger("fla")

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LANG = "lang=pt&region=br"
DEFAULT_ALERTS = {"goal": True, "kickoff": True, "red_card": True, "period": True, "final": True}

LIVE_EVERY = 10.0              # segundos entre polls do summary durante o jogo
FIXTURES_NEAR = 20.0           # fixtures quando o jogo começa em < 10 min (60 s com jogo acompanhado)
FIXTURES_SOON = 60.0           # ... em < 1 h
FIXTURES_IDLE = 600.0
RESULTS_EVERY = 1800.0
PRE_LINEUP_WINDOW = 75 * 60    # busca o summary (escalações) a partir de 75 min antes
KEEP_FINISHED = 3 * 3600       # depois do apito final, segue mostrando o jogo por 3 h

STATUS_PT = {
    "STATUS_SCHEDULED": "Agendado", "STATUS_FIRST_HALF": "1º tempo", "STATUS_HALFTIME": "Intervalo",
    "STATUS_SECOND_HALF": "2º tempo", "STATUS_END_OF_REGULATION": "Fim do tempo normal",
    "STATUS_FIRST_HALF_EXTRA_TIME": "Prorrogação (1º)", "STATUS_SECOND_HALF_EXTRA_TIME": "Prorrogação (2º)",
    "STATUS_HALFTIME_ET": "Intervalo da prorrogação", "STATUS_OVERTIME": "Prorrogação",
    "STATUS_SHOOTOUT": "Pênaltis", "STATUS_FULL_TIME": "Encerrado", "STATUS_FINAL_PEN": "Encerrado (pênaltis)",
    "STATUS_POSTPONED": "Adiado", "STATUS_DELAYED": "Atrasado", "STATUS_SUSPENDED": "Suspenso",
    "STATUS_CANCELED": "Cancelado", "STATUS_ABANDONED": "Abandonado",
}
RUNNING = {"STATUS_FIRST_HALF", "STATUS_SECOND_HALF", "STATUS_FIRST_HALF_EXTRA_TIME", "STATUS_SECOND_HALF_EXTRA_TIME"}

# (nome do ESPN, rótulo, é porcentagem?) na ordem em que aparecem na aba
STAT_ROWS = [("possessionPct", "Posse de bola", True), ("totalShots", "Finalizações", False),
             ("shotsOnTarget", "No alvo", False), ("wonCorners", "Escanteios", False),
             ("foulsCommitted", "Faltas", False), ("offsides", "Impedimentos", False),
             ("yellowCards", "Amarelos", False), ("redCards", "Vermelhos", False),
             ("saves", "Defesas", False), ("totalPasses", "Passes", False)]


# ----------------------------------------------------------------------- parsers (puros)
def _num(v: Any) -> int | None:
    """Placar vem como '1', 1, {'displayValue': '1'} ou {'value': 1.0}."""
    if isinstance(v, dict):
        v = v.get("displayValue", v.get("value"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def parse_team(c: dict) -> dict:
    t = c.get("team") or {}
    logos = t.get("logos") or []
    logo = next((x.get("href") for x in logos if "default" in (x.get("rel") or [])), None) \
        or (logos[0].get("href") if logos else t.get("logo", ""))
    return {"id": str(t.get("id", c.get("id", ""))), "name": t.get("shortDisplayName") or t.get("displayName", ""),
            "full": t.get("displayName", ""), "abbr": t.get("abbreviation", ""), "logo": logo or "",
            "home": c.get("homeAway") == "home", "score": _num(c.get("score")), "winner": c.get("winner")}


def clock_seconds(status: dict) -> int:
    """Segundos de jogo. Usa `clock`; se faltar, lê o minuto de `displayClock` ("29'", "45'+3'")."""
    if status.get("clock") is not None:
        try:
            return int(float(status["clock"]))
        except (TypeError, ValueError):
            pass
    mins = [int(x) for x in re.findall(r"\d+", str(status.get("displayClock", "")))]
    return sum(mins) * 60


def parse_status(status: dict) -> dict:
    t = status.get("type") or {}
    name = t.get("name", "")
    return {"state": t.get("state", "pre"), "name": name,
            "label": STATUS_PT.get(name) or t.get("description", ""), "display": status.get("displayClock", ""),
            "clock": clock_seconds(status), "running": name in RUNNING, "period": status.get("period", 0)}


def parse_event(e: dict) -> dict:
    """Evento de schedule/scoreboard -> jogo (sem detalhes)."""
    c = (e.get("competitions") or [{}])[0]
    teams = [parse_team(x) for x in c.get("competitors") or []]
    home = next((t for t in teams if t["home"]), teams[0] if teams else {})
    away = next((t for t in teams if not t["home"]), teams[1] if len(teams) > 1 else {})
    lg = e.get("league") or {}
    return {"id": str(e.get("id", "")), "slug": lg.get("slug", ""), "league": lg.get("name", ""),
            "date": e.get("date", ""), "ts": _epoch(e.get("date")),
            "venue": ((c.get("venue") or {}).get("fullName") or ""), "home": home, "away": away,
            "status": parse_status(c.get("status") or {})}


def parse_fixtures(data: dict) -> dict:
    """-> {'live': jogo|None, 'next': [jogos pre ordenados por data]}."""
    games = [parse_event(e) for e in data.get("events") or []]
    live = next((g for g in games if g["status"]["state"] == "in"), None)
    nxt = sorted((g for g in games if g["status"]["state"] == "pre"), key=lambda g: g["ts"] or 0)
    return {"live": live, "next": nxt}


def parse_results(data: dict, team_id: str, limit: int = 5) -> list[dict]:
    games = [parse_event(e) for e in data.get("events") or []]
    done = sorted((g for g in games if g["status"]["state"] == "post"), key=lambda g: g["ts"] or 0, reverse=True)
    out = []
    for g in done[:limit]:
        us, them = (g["home"], g["away"]) if g["home"].get("id") == team_id else (g["away"], g["home"])
        a, b = us.get("score"), them.get("score")
        res = "?" if a is None or b is None else "V" if a > b else "D" if a < b else "E"
        if res == "E" and us.get("winner") is True:   # decidido nos pênaltis
            res = "V"
        elif res == "E" and them.get("winner") is True:
            res = "D"
        out.append({**g, "opp": them, "us_score": a, "them_score": b, "result": res})
    return out


KIND_BY_TYPE = [("own-goal", "own_goal"), ("penalty---scored", "pen_goal"), ("goal", "goal"),
                ("yellow-red-card", "red"), ("red-card", "red"), ("yellow-card", "yellow"),
                ("substitution", "sub"), ("penalty---missed", "pen_miss"), ("penalty---saved", "pen_miss"),
                ("var", "var"), ("halftime", "period"), ("kickoff", "period"), ("start-2nd-half", "period"),
                ("end-regular-time", "period"), ("end-extra-time", "period"), ("shootout", "period")]


def parse_key_events(sm: dict) -> list[dict]:
    out = []
    for k in sm.get("keyEvents") or []:
        ty = (k.get("type") or {})
        raw = str(ty.get("type", ""))
        kind = next((v for key, v in KIND_BY_TYPE if raw.startswith(key)), None)
        if kind is None:
            continue
        who = [((p.get("athlete") or {}).get("displayName") or "") for p in k.get("participants") or []]
        out.append({"id": str(k.get("id", "")), "kind": kind, "minute": (k.get("clock") or {}).get("displayValue", ""),
                    "sec": int((k.get("clock") or {}).get("value", 0) or 0), "label": ty.get("text", ""),
                    "team_id": str((k.get("team") or {}).get("id", "")), "text": k.get("text", ""),
                    "player": who[0] if who else "", "other": who[1] if len(who) > 1 else ""})
    return out


def parse_stats(sm: dict) -> list[dict]:
    teams = (sm.get("boxscore") or {}).get("teams") or []
    by_side = {}
    for t in teams:
        by_side[t.get("homeAway", "")] = {s.get("name"): s.get("displayValue") for s in t.get("statistics") or []}
    home, away = by_side.get("home", {}), by_side.get("away", {})
    rows = []
    for key, label, pct in STAT_ROWS:
        if key not in home and key not in away:
            continue
        h, a = home.get(key), away.get(key)
        try:
            hv, av = float(h), float(a)
        except (TypeError, ValueError):
            hv = av = 0.0
        rows.append({"key": key, "label": label, "home": h, "away": a, "pct": pct,
                     "share": 0.5 if hv + av == 0 else hv / (hv + av)})
    return rows


def parse_lineups(sm: dict) -> list[dict]:
    out = []
    for r in sm.get("rosters") or []:
        starters, subs = [], []
        for p in r.get("roster") or []:
            row = {"n": p.get("jersey", ""), "name": (p.get("athlete") or {}).get("displayName", ""),
                   "pos": (p.get("position") or {}).get("abbreviation", ""),
                   "out": bool(p.get("subbedOut")), "in": bool(p.get("subbedIn")),
                   "place": int(p.get("formationPlace") or 0)}
            (starters if p.get("starter") else subs).append(row)
        starters.sort(key=lambda x: x["place"])
        t = r.get("team") or {}
        out.append({"team_id": str(t.get("id", "")), "abbr": t.get("abbreviation", ""), "home": r.get("homeAway") == "home",
                    "formation": r.get("formation", ""), "starters": starters, "subs": subs})
    return out


def parse_commentary(sm: dict, limit: int = 20) -> list[dict]:
    items = [{"min": (c.get("time") or {}).get("displayValue", ""), "text": c.get("text", "")}
             for c in sm.get("commentary") or [] if c.get("text")]
    return items[-limit:][::-1]


def parse_standings(sm: dict, team_id: str) -> list[dict]:
    groups = ((sm.get("standings") or {}).get("groups")) or []
    pick = next((g for g in groups if any(str(e.get("id")) == team_id
                                          for e in (g.get("standings") or {}).get("entries") or [])), None)
    entries = ((pick or (groups[0] if groups else {})).get("standings") or {}).get("entries") or []
    rows = []
    for e in entries:
        s = {x.get("name"): x.get("displayValue") for x in e.get("stats") or []}
        rows.append({"rank": _num(s.get("rank")) or 0, "id": str(e.get("id", "")), "name": e.get("team", ""),
                     "pts": s.get("points", ""), "j": s.get("gamesPlayed", ""), "v": s.get("wins", ""),
                     "e": s.get("ties", ""), "d": s.get("losses", ""), "sg": s.get("pointDifferential", "")})
    rows.sort(key=lambda r: r["rank"])
    return rows


def parse_summary(sm: dict, meta: dict, team_id: str) -> dict:
    """Summary + metadados do jogo -> jogo completo. O header do summary é o mais fresco (placar/relógio)."""
    comp = ((sm.get("header") or {}).get("competitions") or [{}])[0]
    game = parse_event({"id": meta.get("id"), "date": comp.get("date") or meta.get("date"),
                        "league": {"slug": meta.get("slug"), "name": meta.get("league")},
                        "competitions": [{"competitors": comp.get("competitors") or [],
                                          "status": comp.get("status") or {},
                                          "venue": {"fullName": ((sm.get("gameInfo") or {}).get("venue") or {})
                                                    .get("fullName", meta.get("venue", ""))}}]})
    game.update({"events": parse_key_events(sm), "stats": parse_stats(sm), "lineups": parse_lineups(sm),
                 "commentary": parse_commentary(sm), "table": parse_standings(sm, team_id)})
    return game


def form_string(results: list[dict]) -> str:
    return "".join(r["result"] for r in results)


# ----------------------------------------------------------------------- alertas (puros)
class AlertTracker:
    """Compara o jogo atual com o anterior e devolve alertas novos. A primeira vez que vê um jogo é
    só 'baseline' (não alerta o passado, por ex. quando o servidor liga no meio da partida)."""

    def __init__(self, team_id: str, enabled: dict[str, bool] | None = None) -> None:
        self.team_id = team_id
        self.enabled = {**DEFAULT_ALERTS, **(enabled or {})}
        self._seen: dict[str, set[str]] = {}
        self._state: dict[str, str] = {}
        self._status: dict[str, str] = {}
        self._score: dict[str, tuple[int, int]] = {}

    def update(self, g: dict) -> list[dict]:
        gid = g["id"]
        out: list[dict] = []
        first = gid not in self._seen
        seen = self._seen.setdefault(gid, set())
        st = g["status"]
        hs, as_ = g["home"].get("score") or 0, g["away"].get("score") or 0
        score = f"{g['home']['abbr']} {hs} x {as_} {g['away']['abbr']}"
        if first:
            seen.update(e["id"] for e in g.get("events") or [])
        else:
            new = [e for e in g.get("events") or [] if e["id"] not in seen]
            seen.update(e["id"] for e in new)
            # quem fez o gol: pela variação do placar quando há um único gol novo (cobre gol contra)
            goals = [e for e in new if e["kind"] in ("goal", "pen_goal", "own_goal")]
            ph, pa = self._score.get(gid, (hs, as_))
            ours_home = g["home"].get("id") == self.team_id
            by_score = None
            if len(goals) == 1 and (hs, as_) != (ph, pa):
                by_score = (hs > ph) == ours_home if hs != ph else (as_ > pa) != ours_home
            for e in new:
                out += self._for_event(e, score, by_score if e in goals and len(goals) == 1 else None)
            if self._state.get(gid) == "pre" and st["state"] == "in":
                out.append(self._a("kickoff", "Bola rolando!", f"{g['home']['name']} x {g['away']['name']}", "info", False))
            if st["name"] == "STATUS_HALFTIME" and self._status.get(gid) != "STATUS_HALFTIME":
                out.append(self._a("period", "Intervalo", score, "info", False))
            if st["state"] == "post" and self._state.get(gid) == "in":
                out.append(self._a("final", "Fim de jogo", score, "ok", True))
        self._state[gid], self._status[gid], self._score[gid] = st["state"], st["name"], (hs, as_)
        return [a for a in out if self.enabled.get(a["kind"], False)]

    @staticmethod
    def _a(kind: str, title: str, body: str, level: str, sound: bool) -> dict:
        return {"kind": kind, "title": title, "body": body, "level": level, "sound": sound}

    def _for_event(self, e: dict, score: str, by_score: bool | None) -> list[dict]:
        ours = e["team_id"] == self.team_id
        who = f"{e['player']} {e['minute']}".strip()
        if e["kind"] in ("goal", "pen_goal", "own_goal"):
            scored_ours = by_score if by_score is not None else ours != (e["kind"] == "own_goal")
            if scored_ours:
                return [self._a("goal", "GOOOL!", f"{score} · {who}", "alert", True)]
            return [self._a("goal", "Gol do adversário", f"{score} · {who}", "warn", False)]
        if e["kind"] == "red":
            return [self._a("red_card", "Cartão vermelho", f"{who} ({'seu time' if ours else 'adversário'})",
                            "warn", ours)]
        return []


# ----------------------------------------------------------------------- módulo
class Flamengo:
    def __init__(self, cfg: dict) -> None:
        c = cfg.get("flamengo") or {}
        self.team_id = str(c.get("team_id") or "819")
        self.auto_switch = bool(c.get("auto_switch", False))
        self.alerts = AlertTracker(self.team_id, c.get("alerts"))
        self._http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "PC-Control-Center/1.0"})
        self.live: dict | None = None
        self.live_at = 0.0
        self.next: list[dict] = []
        self.last: list[dict] = []
        self.error = ""
        self._fx_at = 0.0
        self._res_at = 0.0
        self._cur: dict | None = None        # jogo acompanhado (meta do fixtures)
        self._finished_at = 0.0
        self._task: asyncio.Task | None = None

    # ---------- rede ----------
    async def _get(self, path: str) -> dict:
        sep = "&" if "?" in path else "?"
        r = await self._http.get(f"{ESPN}/{path}{sep}{LANG}")
        r.raise_for_status()
        return r.json()

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        await self._http.aclose()

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
                self.error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - API não oficial: nunca derruba o servidor
                self.error = "Placar indisponível agora (fonte: ESPN)"
                log.warning("falha no poll do placar: %s", e)
            await asyncio.sleep(self._delay())

    def _delay(self) -> float:
        return LIVE_EVERY if self._cur and self.live and self.live["status"]["state"] == "in" else 20.0

    def _fixtures_every(self) -> float:
        if self._cur:
            return 60.0
        if self.next and self.next[0]["ts"]:
            left = self.next[0]["ts"] - time.time()
            return FIXTURES_NEAR if left < 600 else FIXTURES_SOON if left < 3600 else FIXTURES_IDLE
        return FIXTURES_IDLE

    async def _tick(self) -> None:
        now = time.time()
        if now - self._fx_at >= self._fixtures_every():
            fx = parse_fixtures(await self._get(f"all/teams/{self.team_id}/schedule?fixture=true"))
            self._fx_at, self.next = now, fx["next"]
            await self._pick(fx, now)
        if now - self._res_at >= RESULTS_EVERY:
            await self._refresh_results(now)
        if self._cur:
            await self._refresh_live(now)

    async def _refresh_results(self, now: float) -> None:
        self.last = parse_results(await self._get(f"all/teams/{self.team_id}/schedule"), self.team_id)
        self._res_at = now

    async def _pick(self, fx: dict, now: float) -> None:
        """Jogo acompanhado: em andamento > começa em <75 min (escalações) > terminado há <3 h."""
        soon = next((x for x in fx["next"] if x["ts"] and 0 < x["ts"] - now <= PRE_LINEUP_WINDOW), None)
        if fx["live"] or soon:
            self._cur, self._finished_at = fx["live"] or soon, 0.0
            return
        if self._cur is None:
            return
        if not self._finished_at:
            if self.live and self.live["status"]["state"] in ("in", "post"):   # acabou de terminar
                self._finished_at = now
                await self._refresh_results(now)
            else:                                                             # 'pre' que saiu da janela (adiado)
                self._cur = self.live = None
        elif now - self._finished_at > KEEP_FINISHED:
            self._cur = self.live = None
            self._finished_at = 0.0

    async def _refresh_live(self, now: float) -> None:
        if self._finished_at and self.live and self.live["status"]["state"] == "post":
            return   # já temos o resultado final; só mantém na tela
        cur = self._cur or {}
        sm = await self._get(f"{cur['slug']}/summary?event={cur['id']}")
        game = parse_summary(sm, cur, self.team_id)
        for a in self.alerts.update(game):
            notifier.notify("fla", a["title"], a["body"], a["level"], sound=a["sound"])
            if self.auto_switch and a["kind"] in ("goal", "kickoff"):
                notifier.event("switch_tab", tab="fla")
        self.live, self.live_at = game, now
        if game["status"]["state"] == "post" and not self._finished_at:
            self._finished_at = now
            await self._refresh_results(now)

    # ---------- estado ----------
    async def state(self) -> dict[str, Any]:
        now = time.time()
        live = None
        if self.live:
            live = {**self.live, "age": round(now - self.live_at, 1)}
        nxt = [{**g, "in_s": None if g["ts"] is None else round(g["ts"] - now)} for g in self.next[:4]]
        return {"team_id": self.team_id, "live": live, "next": nxt, "last": self.last,
                "form": form_string(self.last), "error": self.error}


_fla: Flamengo | None = None


def register(dispatcher: Any, cfg: dict) -> Flamengo:
    global _fla
    _fla = Flamengo(cfg)
    return _fla
