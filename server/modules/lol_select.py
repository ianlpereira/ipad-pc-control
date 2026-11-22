"""Seleção de campeões: interpretação da sessão do cliente (LCU) em dados prontos para o iPad.

Tudo aqui é puro (sem I/O): recebe o JSON de `/lol-champ-select/v1/session` e devolve o que o
painel mostra. Assim dá para testar com sessões de fixture, sem o cliente do jogo aberto.
Só usamos informação que o jogador já vê na tela de seleção.
"""
from __future__ import annotations

from typing import Any, Callable

NameOf = Callable[[int], str]

PHASE_LABEL = {"PLANNING": "Planejamento", "BAN_PICK": "Seleção", "FINALIZATION": "Finalização",
               "GAME_STARTING": "Partida começando"}
POS_KEY = {"top": "top", "jungle": "jungle", "middle": "mid", "mid": "mid", "bottom": "adc", "adc": "adc",
           "utility": "support", "support": "support"}

# tacticalInfo.damageType do cliente -> peso de dano físico
DAMAGE_AD = {"kPhysical": 1.0, "kMixed": 0.5, "kMagic": 0.0}
# Sem dado de dano por campeão: estimativa pela classe (mago = AP, o resto = AD).
ROLE_AD = {"marksman": 1.0, "fighter": 0.8, "assassin": 0.7, "tank": 0.5, "support": 0.4, "mage": 0.0}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _actions(session: dict) -> list[dict]:
    """Achata `actions` (lista de listas, uma por rodada) numa lista só."""
    out: list[dict] = []
    for group in session.get("actions") or []:
        for a in group if isinstance(group, list) else [group]:
            if isinstance(a, dict):
                out.append(a)
    return out


def find_action(session: dict, in_progress_only: bool = False) -> dict | None:
    """Ação do jogador local que dá para alterar agora.

    Prioridade: a que está em andamento (banir/escolher); senão a próxima escolha ainda não
    travada (permite passar o "palpite" de campeão enquanto os outros banem).
    """
    cell = session.get("localPlayerCellId")
    mine = [a for a in _actions(session) if a.get("actorCellId") == cell and not a.get("completed")]
    for a in mine:
        if a.get("isInProgress"):
            return a
    if in_progress_only:
        return None
    return next((a for a in mine if a.get("type") == "pick"), None)


def _cells_acting(session: dict) -> set[int]:
    return {int(a["actorCellId"]) for a in _actions(session)
            if a.get("isInProgress") and a.get("actorCellId") is not None}


def _member(p: dict, cell: int | None, acting: set[int], name_of: NameOf, ally: bool) -> dict:
    cid = _int(p.get("championId"))
    intent = _int(p.get("championPickIntent"))
    shown = cid or (intent if ally else 0)  # intenção do inimigo não é visível no jogo
    pos = POS_KEY.get(str(p.get("assignedPosition") or "").lower(), "")
    return {"cell": p.get("cellId"), "champion_id": shown, "champion": name_of(shown) if shown else "",
            "locked": bool(cid), "position": pos, "me": ally and p.get("cellId") == cell,
            "acting": p.get("cellId") in acting}


def parse_session(session: dict, name_of: NameOf, pickable: list[int] | None = None,
                  bannable: list[int] | None = None) -> dict[str, Any] | None:
    """Sessão do cliente -> estado completo da seleção. None se a sessão não tem o jogador."""
    cell = session.get("localPlayerCellId")
    my_team = session.get("myTeam") or []
    me = next((p for p in my_team if p.get("cellId") == cell), None)
    if me is None:
        return None
    acting = _cells_acting(session)
    action = find_action(session)
    turn_action = action if action and action.get("isInProgress") else None

    locked = _int(me.get("championId"))
    intent = _int(me.get("championPickIntent"))
    kind = (turn_action or {}).get("type") or "pick"
    act_champ = _int(action.get("championId")) if action else 0
    # Escolha: a intenção (ou o campeão marcado na ação). Banimento: o campeão marcado na ação.
    pick_hover = (act_champ if action and action.get("type") == "pick" else 0) or intent
    champ = locked or pick_hover                       # campeão que a build/runas seguem (nunca o banido)
    hover = act_champ if kind == "ban" else (locked or pick_hover)   # o que o iPad destaca na grade

    timer = session.get("timer") or {}
    left = _int(timer.get("adjustedTimeLeftInPhase")) / 1000.0
    total = _int(timer.get("totalTimeInPhase")) / 1000.0
    phase = str(timer.get("phase") or "")
    if turn_action and phase == "BAN_PICK":
        label = "Sua vez de banir" if kind == "ban" else "Sua vez de escolher"
    else:
        label = PHASE_LABEL.get(phase, phase.title())

    bans = session.get("bans") or {}
    bench = [_int(b.get("championId")) for b in session.get("benchChampions") or []]
    return {
        "champion_id": champ, "champion": name_of(champ) if champ else "", "locked": bool(locked),
        "position": str(me.get("assignedPosition") or ""),
        "me": {"cell": cell, "hover": hover, "locked_id": locked,
               "spell1": _int(me.get("spell1Id")), "spell2": _int(me.get("spell2Id")),
               "skin": _int(me.get("selectedSkinId"))},
        "my_team": [_member(p, cell, acting, name_of, True) for p in my_team],
        "enemy_team": [_member(p, cell, acting, name_of, False) for p in session.get("theirTeam") or []],
        "bans": {"mine": [b for b in map(_int, bans.get("myTeamBans") or []) if b],
                 "theirs": [b for b in map(_int, bans.get("theirTeamBans") or []) if b]},
        "timer": {"phase": phase, "label": label, "left": max(0.0, left), "total": total,
                  "infinite": bool(timer.get("isInfinite"))},
        "turn": ({"action_id": turn_action.get("id"), "type": kind} if turn_action else None),
        "avail_kind": kind,
        "avail": list((bannable if kind == "ban" else pickable) or []),
        "bench": [b for b in bench if b], "can_reroll": bool(session.get("allowRerolling")),
        "rerolls": _int(session.get("rerollsRemaining")),
        "allow_skin": bool(session.get("allowSkinSelection", True)),
    }


# ---------------------------------------------------------------- lobby
QUEUE_ORDER = [400, 420, 440, 480, 450, 2400]   # normal, solo/duo, flex, jogo dinâmico, ARAM, ARAM desordem
QUEUE_SKIP = {710}                              # "Ranqueada-5": visível na lista, mas o cliente não oferece
POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", "FILL"]
# categorias na ordem em que aparecem, como no seletor de modos do cliente
QUEUE_GROUPS = ["Summoner's Rift", "ARAM", "Outros modos", "Contra bots"]


def _queue_group(q: dict) -> str:
    if q.get("category") == "VersusAi" or q.get("gameSelectCategory") == "kVersusAI":
        return "Contra bots"
    g, mode = q.get("gameSelectModeGroup") or "", q.get("gameMode")
    if g == "kSummonersRift" or (not g and mode in ("CLASSIC", "SWIFTPLAY")):
        return "Summoner's Rift"
    if g == "kARAM" or (not g and mode in ("ARAM", "KIWI")):
        return "ARAM"
    return "Outros modos"


def pick_queues(queues: list[dict]) -> list[dict]:
    """Filas jogáveis (PvP e contra bots) agrupadas como no cliente. Sem TFT, tutoriais, Clash nem personalizadas."""
    ok = []
    for q in queues or []:
        if not isinstance(q, dict) or q.get("queueAvailability") != "Available" or q.get("isCustom"):
            continue
        qid = _int(q.get("id"))
        name = str(q.get("shortName") or q.get("name") or "")
        if (not qid or qid in QUEUE_SKIP or not name or q.get("isVisible") is False
                or q.get("category") not in ("PvP", "VersusAi") or q.get("gameMode") == "TFT"
                or q.get("gameSelectModeGroup") == "kTeamfightTactics"
                or str(q.get("gameMode") or "").startswith("TUTORIAL")):
            continue
        ok.append((q, qid, name))
    rank = {x: i for i, x in enumerate(QUEUE_ORDER)}
    ok.sort(key=lambda t: (QUEUE_GROUPS.index(_queue_group(t[0])), rank.get(t[1], 99), t[1]))
    return [{"id": qid, "name": name, "mode": str(q.get("gameMode") or ""), "group": _queue_group(q),
             "positions": bool(q["showPositionSelector"]) if "showPositionSelector" in q else q.get("gameMode") == "CLASSIC"}
            for q, qid, name in ok]


def parse_lobby(lobby: dict, queue_name: Callable[[int], str]) -> dict[str, Any] | None:
    """`/lol-lobby/v2/lobby` -> {queue, mode, positions: bool, first, second, can_start, leader, members}."""
    if not isinstance(lobby, dict):
        return None
    cfg = lobby.get("gameConfig") or {}
    local = lobby.get("localMember") or {}
    qid = _int(cfg.get("queueId"))
    first = str(local.get("firstPositionPreference") or "")
    second = str(local.get("secondPositionPreference") or "")
    members = [{"name": str(m.get("summonerName") or m.get("gameName") or ""), "ready": bool(m.get("ready")),
                "leader": bool(m.get("isLeader")), "me": m.get("puuid") == local.get("puuid")}
               for m in lobby.get("members") or []]
    return {"queue_id": qid, "queue": queue_name(qid), "mode": str(cfg.get("gameMode") or ""),
            "positions": bool(cfg.get("showPositionSelector")),
            "first": first if first in POSITIONS else "", "second": second if second in POSITIONS else "",
            "can_start": bool(lobby.get("canStartActivity")), "leader": bool(local.get("isLeader")),
            "members": members}


# ---------------------------------------------------------------- análise do time inimigo
def team_damage(ids: list[int], damage_of: Callable[[int], float | None]) -> dict[str, Any] | None:
    """Mix de dano físico/mágico do time (campeões revelados). None com menos de 2 campeões."""
    shares = [s for s in (damage_of(i) for i in ids) if s is not None]
    if len(shares) < 2:
        return None
    share = sum(shares) / len(shares)
    hint = "armadura" if share >= 0.65 else "resistência mágica" if share <= 0.35 else "mistura de armadura e RM"
    return {"ad_share": round(share, 2), "hint": hint, "known": len(shares), "estimate": True}


def damage_share(detail: dict | None, roles: list[str] | None) -> float | None:
    """Peso físico (0-1) de um campeão: `tacticalInfo.damageType` do cliente, senão pela classe."""
    kind = ((detail or {}).get("tacticalInfo") or {}).get("damageType")
    if kind in DAMAGE_AD:
        return DAMAGE_AD[kind]
    r = [ROLE_AD[x] for x in roles or [] if x in ROLE_AD]
    return sum(r) / len(r) if r else None


def comp_roles(team_roles: list[list[str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for roles in team_roles:
        for r in roles or []:
            out[r] = out.get(r, 0) + 1
    return out


def best_answers(counters: list[dict], available: set[int] | None, min_play: int = 150,
                 limit: int = 8) -> list[dict]:
    """Melhores respostas a um campeão X, a partir da lista `counters` do op.gg de X.

    Cada entrada {champion_id, play, win} traz as vitórias de X contra aquele campeão (conferido:
    a lista de A contra B soma ~100% com a de B contra A). Resposta forte = X ganha pouco.
    """
    rows = []
    for c in counters or []:
        play = _int(c.get("play"))
        cid = _int(c.get("champion_id"))
        if play < min_play or not cid or (available is not None and cid not in available):
            continue
        rows.append({"champion_id": cid, "win_rate": 1 - _int(c.get("win")) / play, "play": play})
    rows.sort(key=lambda r: r["win_rate"], reverse=True)
    return rows[:limit]
