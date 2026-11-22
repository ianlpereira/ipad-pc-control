"""Comandos do iPad para o cliente do LoL: lobby/fila, seleção de campeões e páginas de runas.

Cada comando é um toque do usuário (nada de escolher ou travar sozinho). Falhas do cliente viram
`CommandError` em português, que o iPad mostra como aviso.
Caminhos da LCU: conferir em https://127.0.0.1:<porta>/swagger/v3/openapi.json com o cliente aberto.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from ..protocol import CommandError
from . import lol_select

if TYPE_CHECKING:
    from .lol import Lol

LOBBY = "/lol-lobby/v2/lobby"
SELECT = "/lol-champ-select/v1"
QUEUES_TTL = 300


def _int(v: Any, what: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise CommandError(f"{what} inválido") from None
    if n <= 0:
        raise CommandError(f"{what} inválido")
    return n


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        msg = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    return str(msg or r.text or r.status_code)[:120]


class Control:
    def __init__(self, lol: "Lol") -> None:
        self.lol = lol
        self._queues: list[dict] = []
        self._queues_at = 0.0

    async def _call(self, method: str, path: str, fail: str, **kw: Any) -> httpx.Response:
        r = await self.lol.lcu(method, path, **kw)
        if r is None:
            raise CommandError("o cliente do LoL não responde")
        if r.status_code >= 400:
            raise CommandError(f"{fail}: {_detail(r)}")
        return r

    # ---------- catálogo ----------
    async def queues(self) -> list[dict]:
        if not self._queues or time.monotonic() - self._queues_at > QUEUES_TTL:
            data = await self.lol._json("/lol-game-queues/v1/queues")
            if isinstance(data, list):
                self._queues = lol_select.pick_queues(data)
                self._queues_at = time.monotonic()
        return self._queues

    def queue_name(self, queue_id: int) -> str:
        return next((q["name"] for q in self._queues if q["id"] == queue_id), "")

    # ---------- lobby ----------
    async def lobby_state(self) -> dict | None:
        await self.queues()
        data = await self.lol._json(LOBBY)
        return lol_select.parse_lobby(data, self.queue_name) if data else None

    async def create_lobby(self, v: Any) -> None:
        qid = _int((v or {}).get("queue_id") if isinstance(v, dict) else v, "modo de jogo")
        cur = await self.lol._json(LOBBY)
        if isinstance(cur, dict) and int((cur.get("gameConfig") or {}).get("queueId") or 0) == qid:
            return
        # O cliente troca o modo do grupo (só o líder) com o mesmo POST que cria o lobby, sem desfazer o grupo.
        r = await self.lol.lcu("POST", LOBBY, json={"queueId": qid})
        if r is None:
            raise CommandError("o cliente do LoL não responde")
        if r.status_code < 400:
            return
        if isinstance(cur, dict) and len(cur.get("members") or []) <= 1:   # sozinho: sai e cria de novo
            await self._call("DELETE", LOBBY, "não deu para sair do lobby atual")
            await self._call("POST", LOBBY, "não deu para criar o lobby", json={"queueId": qid})
            return
        who = "" if (cur or {}).get("localMember", {}).get("isLeader") else " (só o líder do grupo troca o modo)"
        raise CommandError(f"não deu para trocar o modo: {_detail(r)}{who}")

    async def set_positions(self, v: Any) -> None:
        v = v if isinstance(v, dict) else {}
        first, second = str(v.get("first") or "").upper(), str(v.get("second") or "").upper()
        for p in (first, second):
            if p and p not in lol_select.POSITIONS:
                raise CommandError("rota inválida")
        if first and first == second and first != "FILL":
            second = ""
        await self._call("PUT", LOBBY + "/members/localMember/position-preferences", "não deu para trocar a rota",
                         json={"firstPreference": first or "UNSELECTED", "secondPreference": second or "UNSELECTED"})

    async def search(self, _v: Any = None) -> None:
        await self._call("POST", LOBBY + "/matchmaking/search", "não deu para buscar partida")

    async def cancel_search(self, _v: Any = None) -> None:
        await self._call("DELETE", LOBBY + "/matchmaking/search", "não deu para cancelar a busca")

    async def leave_lobby(self, v: Any = None) -> None:
        if not (isinstance(v, dict) and v.get("confirm")):
            raise CommandError("confirmação necessária")
        await self._call("DELETE", LOBBY, "não deu para sair do lobby")

    async def play_again(self, _v: Any = None) -> None:
        await self._call("POST", "/lol-lobby/v2/play-again", "não deu para voltar ao lobby")

    # ---------- seleção ----------
    async def _session(self) -> dict:
        s = await self.lol._json(SELECT + "/session")
        if not isinstance(s, dict):
            raise CommandError("você não está na seleção de campeões")
        return s

    async def hover(self, v: Any) -> None:
        cid = _int((v or {}).get("champion_id") if isinstance(v, dict) else v, "campeão")
        action = lol_select.find_action(await self._session())
        if action is None:
            raise CommandError("ainda não dá para escolher")
        await self._call("PATCH", f"{SELECT}/session/actions/{action['id']}", "o cliente recusou o campeão",
                         json={"championId": cid})

    async def lock(self, v: Any = None) -> str:
        session = await self._session()
        action = lol_select.find_action(session, in_progress_only=True)
        if action is None:
            raise CommandError("não é a sua vez")
        cid = int(action.get("championId") or 0)
        if not cid and isinstance(v, dict) and v.get("champion_id"):
            cid = _int(v["champion_id"], "campeão")
        if not cid:
            cell = session.get("localPlayerCellId")
            me = next((p for p in session.get("myTeam") or [] if p.get("cellId") == cell), {})
            cid = int(me.get("championPickIntent") or 0)
        if not cid:
            raise CommandError("toque num campeão primeiro")
        path = f"{SELECT}/session/actions/{action['id']}"
        if cid != int(action.get("championId") or 0):
            await self._call("PATCH", path, "o cliente recusou o campeão", json={"championId": cid})
        await self._call("POST", path + "/complete", "não deu para confirmar")
        return str(action.get("type") or "pick")

    async def set_spells(self, v: Any) -> None:
        v = v if isinstance(v, dict) else {}
        body = {k: _int(v[f], "feitiço") for k, f in (("spell1Id", "spell1"), ("spell2Id", "spell2")) if v.get(f)}
        if not body:
            raise CommandError("feitiço inválido")
        await self._call("PATCH", SELECT + "/session/my-selection", "não deu para trocar o feitiço", json=body)

    async def skins(self, _v: Any = None) -> list[dict]:
        data = await self.lol._json(SELECT + "/skin-carousel-skins")
        out = []
        for s in data if isinstance(data, list) else []:
            if s.get("unlocked") and not s.get("disabled", False):
                out.append({"id": int(s["id"]), "name": str(s.get("name") or ""), "tile": str(s.get("tilePath") or ""),
                            "selected": bool(s.get("selected"))})
        return out

    async def set_skin(self, v: Any) -> None:
        sid = _int((v or {}).get("skin_id") if isinstance(v, dict) else v, "skin")
        await self._call("PATCH", SELECT + "/session/my-selection", "não deu para trocar a skin",
                         json={"selectedSkinId": sid})

    async def reroll(self, _v: Any = None) -> None:
        await self._call("POST", SELECT + "/session/my-selection/reroll", "não deu para rerrolar")

    async def bench_swap(self, v: Any) -> None:
        cid = _int((v or {}).get("champion_id") if isinstance(v, dict) else v, "campeão")
        await self._call("POST", f"{SELECT}/session/bench/swap/{cid}", "não deu para trocar com o banco")

    # ---------- runas ----------
    async def rune_pages(self, _v: Any = None) -> list[dict]:
        pages = await self.lol._json("/lol-perks/v1/pages")
        b = self.lol.builds
        out = []
        for p in pages if isinstance(pages, list) else []:
            if not p.get("isValid", True):
                continue
            out.append({"id": p["id"], "name": str(p.get("name") or ""), "active": bool(p.get("current")),
                        "primary_style": b._ref("style", [p.get("primaryStyleId", 0)])[0],
                        "sub_style": b._ref("style", [p.get("subStyleId", 0)])[0],
                        "perks": b._ref("perk", p.get("selectedPerkIds") or [])})
        return out

    async def activate_page(self, v: Any) -> None:
        pid = _int((v or {}).get("id") if isinstance(v, dict) else v, "página de runas")
        await self._call("PUT", "/lol-perks/v1/currentpage", "não deu para ativar a página", json=pid)
