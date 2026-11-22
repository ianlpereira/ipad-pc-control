"""Builds para a partida: posição + campeão (cliente do LoL) -> estatísticas do op.gg.

- Dados de build: API JSON pública que alimenta o site do op.gg
  (lol-api-champion.op.gg). NÃO é oficial: pode mudar sem aviso. Se mudar, o
  painel mostra "build indisponível" e o resto continua funcionando.
- Nomes e ícones (itens, runas, feitiços, campeões) vêm dos assets locais do
  próprio cliente do LoL, já no idioma do cliente. O iPad carrega os ícones
  pelo proxy GET /lol/asset?p=/lol-game-data/assets/... (só esse prefixo).
- Importar runas usa a API local do cliente (/lol-perks/v1/pages).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import httpx

from ..protocol import CommandError
from . import lol_select

if TYPE_CHECKING:
    from .lol import Lol

log = logging.getLogger("lol.builds")

OPGG = "https://lol-api-champion.op.gg/api/global/champions"
CACHE_TTL = 6 * 3600
ASSET_PREFIX = "/lol-game-data/assets/"
PAGE_NAME = "PC Control"

# posição do cliente / partida -> slug do op.gg
POS_TO_OPGG = {"top": "top", "jungle": "jungle", "middle": "mid", "mid": "mid", "bottom": "adc",
               "adc": "adc", "utility": "support", "support": "support"}
POS_SHORT = {"top": "TOP", "jungle": "JG", "mid": "MID", "adc": "ADC", "support": "SUP", "none": "ARAM"}
POS_LABEL = {"top": "Topo", "jungle": "Selva", "mid": "Meio", "adc": "Atirador", "support": "Suporte", "none": "ARAM"}


class Builds:
    def __init__(self, lol: "Lol") -> None:
        self.lol = lol
        self._http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "PC-Control-Center/1.0"})
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._assets: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._meta: dict[str, dict[int, dict]] = {}
        self._meta_key: Any = None
        self.target: tuple[int, str, str] | None = None   # (champion_id, posição opgg, modo)
        self.build: dict | None = None
        self.loading = False
        self.error = ""
        self._task: asyncio.Task | None = None
        self.counters: list[dict] = []                  # {champion_id, play, win}: minha vitória contra cada campeão
        self._meta_try = 0.0
        self._detail: dict[int, dict] = {}              # champions/{id}.json do cliente (tacticalInfo)
        self._enemy_counters: dict[tuple, list[dict]] = {}   # (id inimigo, modo) -> counters do op.gg
        self._enemy_tasks: dict[tuple, asyncio.Task] = {}

    # ---------- metadados locais (nomes/ícones) ----------
    async def _load_meta(self) -> None:
        key = self.lol._lcu
        if self._meta and key == self._meta_key:
            return
        meta: dict[str, dict[int, dict]] = {}
        for kind, path in (("item", "items.json"), ("perk", "perks.json"),
                           ("spell", "summoner-spells.json"), ("champ", "champion-summary.json")):
            data = await self.lol._json(ASSET_PREFIX + "v1/" + path)
            meta[kind] = {}
            for e in data if isinstance(data, list) else []:
                icon = e.get("iconPath") or e.get("squarePortraitPath") or ""
                # "to" preenchido = componente (ex.: Espada Longa), não item final
                meta[kind][int(e["id"])] = {"name": e.get("name", ""), "icon": icon, "comp": bool(e.get("to")),
                                            "price": int(e.get("priceTotal") or e.get("price") or 0),
                                            "from": [int(x) for x in e.get("from") or []],
                                            "cats": list(e.get("categories") or []),
                                            "alias": str(e.get("alias") or ""), "roles": list(e.get("roles") or []),
                                            "modes": list(e.get("gameModes") or [])}
        styles = await self.lol._json(ASSET_PREFIX + "v1/perkstyles.json")
        meta["style"] = {int(s["id"]): {"name": s.get("name", ""), "icon": s.get("iconPath", "")}
                         for s in (styles or {}).get("styles", [])}
        if all(meta.values()):
            self._meta, self._meta_key = meta, key

    async def ensure_meta(self) -> None:
        """Garante nomes/ícones carregados (no máx. uma tentativa a cada 10 s se o cliente não responder)."""
        if self._meta and self.lol._lcu == self._meta_key:
            return
        if time.monotonic() - self._meta_try < 10:
            return
        self._meta_try = time.monotonic()
        await self._load_meta()

    def champions(self) -> list[dict]:
        """Catálogo para a grade de campeões do iPad (sem id -1 / "nenhum")."""
        table = self._meta.get("champ", {})
        # ids >= 10000 são variantes de modos especiais (ex.: 60103 = Ahri); fora delas o campeão aparece duas vezes
        out = [{"id": cid, "name": c["name"], "roles": c.get("roles") or []}
               for cid, c in table.items()
               if cid > 0 and c.get("name") and not (cid >= 10000 and self.base_champion(cid) in table)]
        out.sort(key=lambda c: c["name"].lower())
        return out

    def spells(self, mode: str) -> list[dict]:
        """Feitiços de invocador disponíveis no modo (ex.: Golpear não existe no ARAM)."""
        out = [{"id": sid, "name": e["name"], "icon": e["icon"]}
               for sid, e in self._meta.get("spell", {}).items()
               if e.get("name") and (not e.get("modes") or mode in e["modes"])]
        out.sort(key=lambda e: e["name"].lower())
        return out

    def _ref(self, kind: str, ids: list) -> list[dict]:
        table = self._meta.get(kind, {})
        out = []
        for i in ids:
            e = table.get(int(i), {})
            out.append({"id": int(i), "name": e.get("name") or str(i), "icon": e.get("icon", "")})
        return out

    @staticmethod
    def base_champion(champ_id: int) -> int:
        """Modos especiais usam ids-variante (ex.: 60103 = Ahri, 103). op.gg só conhece o id base."""
        champ_id = int(champ_id)
        return champ_id % 1000 if champ_id >= 10000 else champ_id

    def champion_name(self, champ_id: int) -> str:
        return self._meta.get("champ", {}).get(int(champ_id), {}).get("name", "")

    # ---------- consulta para o analisador ao vivo (lol_live.py); tudo degrada para vazio sem meta ----------
    def item_price(self, item_id: int) -> int:
        return self._meta.get("item", {}).get(int(item_id), {}).get("price", 0)

    def item_from(self, item_id: int) -> list[int]:
        return self._meta.get("item", {}).get(int(item_id), {}).get("from", [])

    def item_cats(self, item_id: int) -> list[str]:
        return self._meta.get("item", {}).get(int(item_id), {}).get("cats", [])

    def item_icon(self, item_id: int) -> str:
        return self._meta.get("item", {}).get(int(item_id), {}).get("icon", "")

    def champ_by_alias(self, alias: str) -> dict | None:
        key = alias.lower()
        for cid, c in self._meta.get("champ", {}).items():
            if c.get("alias", "").lower() == key:
                return {"id": cid, "name": c.get("name", ""), "roles": c.get("roles", [])}
        return None

    # ---------- proxy de ícones ----------
    async def asset(self, path: str) -> tuple[bytes, str] | None:
        if not path.startswith(ASSET_PREFIX) or ".." in path:
            return None
        if path in self._assets:
            self._assets.move_to_end(path)
            return self._assets[path]
        r = await self.lol.lcu("GET", path)
        if r is None or r.status_code != 200:
            return None
        item = (r.content, r.headers.get("content-type", "image/png"))
        self._assets[path] = item
        while len(self._assets) > 400:
            self._assets.popitem(last=False)
        return item

    # ---------- alvo (campeão + posição) ----------
    def set_target(self, champ_id: int, position: str, mode: str) -> None:
        """Chamado pelo módulo lol a cada tick; busca em segundo plano quando o alvo muda."""
        if not champ_id:
            return
        champ_id = self.base_champion(champ_id)
        pos ="none" if mode == "ARAM" else POS_TO_OPGG.get((position or "").lower(), "")
        target = (int(champ_id), pos, mode)
        if target == self.target:
            return
        self.target = target
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.get_running_loop().create_task(self._fetch(target))

    def clear_enemies(self) -> None:
        """Nova seleção: descarta o que foi buscado sobre inimigos da anterior."""
        for t in self._enemy_tasks.values():
            t.cancel()
        self._enemy_tasks.clear()
        self._enemy_counters.clear()

    def clear(self) -> None:
        self.target, self.build, self.error, self.loading = None, None, "", False
        self.counters = []

    async def _fetch(self, target: tuple[int, str, str]) -> None:
        champ, pos, mode = target
        self.loading, self.error = True, ""
        try:
            await self._load_meta()
            data, pos = await self._opgg(champ, pos, mode)
            build = self._parse(champ, pos, data)
            if self.target == target:
                self.build = build
                self.counters = list(data.get("counters") or [])
                log.info("build carregada: %s %s", build["champion"], pos)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("build indisponível (%s): %s", target, e)
            if self.target == target:
                self.error = "Build indisponível agora (fonte: op.gg)"
        finally:
            if self.target == target:
                self.loading = False

    async def _opgg(self, champ: int, pos: str, mode: str) -> tuple[dict, str]:
        async def get(p: str) -> dict:
            key = (champ, p, mode)
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
            kind = "aram" if mode == "ARAM" else "ranked"
            r = await self._http.get(f"{OPGG}/{kind}/{champ}/{p}")
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            data = r.json().get("data") or {}
            if data.get("runes"):  # resposta vazia (sem dados/limite) não vai para o cache
                self._cache[key] = (time.time(), data)
            return data

        if mode == "ARAM":
            data = await get("none")
            if not data.get("runes"):
                raise LookupError("sem dados de ARAM")
            return data, "none"
        if pos:
            data = await get(pos)
            if data.get("runes"):
                return data, pos
            # posição fora do comum (ex.: Lucian topo): cai na posição principal do campeão
        probe = data if pos else await get("mid")
        positions = (probe.get("summary") or {}).get("positions") or []
        best = POS_TO_OPGG.get(str(positions[0].get("name", "")).lower(), "mid") if positions else "mid"
        data = probe if best == (pos or "mid") else await get(best)
        if not data.get("runes"):
            raise LookupError("sem dados para o campeão")
        return data, best

    def _parse(self, champ: int, pos: str, d: dict) -> dict:
        first = lambda key: (d.get(key) or [{}])[0]  # noqa: E731 - opção mais popular
        summary = d.get("summary") or {}
        stats = summary.get("average_stats") or {}
        for p in summary.get("positions") or []:
            if POS_TO_OPGG.get(str(p.get("name", "")).lower()) == pos:
                stats = {**stats, **(p.get("stats") or {})}
        rune = first("runes")
        core = list(first("core_items").get("ids") or [])
        boots = list(first("boots").get("ids") or [])
        items = self._meta.get("item", {})
        situ = [x["ids"][0] for x in d.get("last_items") or []
                if x.get("ids") and x["ids"][0] not in core and x["ids"][0] not in boots
                and not items.get(x["ids"][0], {}).get("comp")][:5]
        starter = list(dict.fromkeys(first("starter_items").get("ids") or []))  # tira poções repetidas
        tier = (stats.get("tier_data") or {}).get("tier") or stats.get("tier")
        return {
            "champion_id": champ,
            "champion": self.champion_name(champ),
            "champion_icon": self._meta.get("champ", {}).get(champ, {}).get("icon", ""),
            "position": pos, "position_label": POS_LABEL.get(pos, pos),
            "win_rate": stats.get("win_rate"), "pick_rate": stats.get("pick_rate"),
            "play": stats.get("play"), "tier": tier,
            "runes": self._rune_set(rune),
            "rune_options": [self._rune_set(r) for r in (d.get("runes") or [])[:3]],
            "spells": self._ref("spell", first("summoner_spells").get("ids") or []),
            "starter": self._ref("item", starter),
            "core": self._ref("item", core),
            "boots": self._ref("item", boots),
            "situational": self._ref("item", situ),
            "skill_priority": first("skill_masteries").get("ids") or [],
            "skill_order": first("skills").get("order") or [],
            "source": "op.gg",
        }

    def _rune_set(self, rune: dict) -> dict:
        return {
            "primary_style": self._ref("style", [rune.get("primary_page_id", 0)])[0],
            "sub_style": self._ref("style", [rune.get("secondary_page_id", 0)])[0],
            "primary": self._ref("perk", rune.get("primary_rune_ids") or []),
            "secondary": self._ref("perk", rune.get("secondary_rune_ids") or []),
            "shards": self._ref("perk", rune.get("stat_mod_ids") or []),
            "win_rate": (rune.get("win", 0) / rune["play"]) if rune.get("play") else None,
            "play": rune.get("play"),
        }

    def state(self) -> dict | None:
        if self.target is None:
            return None
        return {"loading": self.loading, "error": self.error, "data": self.build}

    # ---------- time inimigo (seleção de campeões) ----------
    async def _detail_of(self, champ_id: int) -> dict:
        if champ_id not in self._detail:
            d = await self.lol._json(ASSET_PREFIX + f"v1/champions/{int(champ_id)}.json")
            self._detail[champ_id] = d if isinstance(d, dict) else {}
        return self._detail[champ_id]

    async def _fetch_enemy(self, champ: int, mode: str) -> None:
        try:
            await self._detail_of(champ)
            if mode != "ARAM":
                data, _pos = await self._opgg(champ, "", mode)
                self._enemy_counters[(champ, mode)] = list(data.get("counters") or [])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - dica extra: falhar não pode atrapalhar a seleção
            log.warning("dados do inimigo %s indisponíveis: %s", champ, e)
            self._enemy_counters[(champ, mode)] = []

    def enemy_intel(self, enemy_ids: list[int], available: set[int] | None, mode: str) -> dict:
        """Composição, tipo de dano e melhores respostas dos campeões inimigos já revelados."""
        ids = [self.base_champion(i) for i in enemy_ids if i]
        champs = self._meta.get("champ", {})
        for cid in ids:
            key = (cid, mode)
            if key not in self._enemy_counters and key not in self._enemy_tasks:
                self._enemy_tasks[key] = asyncio.get_running_loop().create_task(self._fetch_enemy(cid, mode))
        dmg = lambda cid: lol_select.damage_share(self._detail.get(cid), champs.get(cid, {}).get("roles"))  # noqa: E731
        picks = []
        for cid in ids:
            counters = self._enemy_counters.get((cid, mode))
            picks.append({"champion_id": cid, "champion": self.champion_name(cid),
                          "answers": lol_select.best_answers(counters or [], available) if counters else [],
                          "loading": counters is None})
        return {"damage": lol_select.team_damage(ids, dmg),
                "comp": lol_select.comp_roles([champs.get(i, {}).get("roles") or [] for i in ids]),
                "picks": picks}

    def matchups(self, enemy_ids: list[int]) -> list[dict]:
        """Meu campeão (build carregada) contra cada inimigo revelado: win_rate = minha taxa de vitória."""
        by_id = {int(c.get("champion_id", 0)): c for c in self.counters}
        out = []
        for e in (self.base_champion(i) for i in enemy_ids if i):
            c = by_id.get(e)
            if c and c.get("play"):
                out.append({"champion_id": e, "win_rate": c["win"] / c["play"], "play": c["play"]})
        return out

    # ---------- importar runas ----------
    async def import_runes(self, v: Any = None) -> str:
        """Importa runas da build carregada. `{"index": n}` escolhe uma das opções (0 = mais popular)."""
        b = self.build
        if not b:
            raise CommandError("nenhuma build carregada")
        idx = int(v.get("index", 0)) if isinstance(v, dict) else 0
        options = b.get("rune_options") or []
        r = options[idx] if 0 <= idx < len(options) else b["runes"]
        perks = [p["id"] for p in r["primary"] + r["secondary"] + r["shards"]]
        if len(perks) != 9:
            raise CommandError("build sem runas completas")
        name = f"{PAGE_NAME}: {b['champion']} {POS_SHORT.get(b['position'], '')}".strip()[:50]
        body = {"name": name, "primaryStyleId": r["primary_style"]["id"], "subStyleId": r["sub_style"]["id"],
                "selectedPerkIds": perks, "current": True}
        pages = await self.lol._json("/lol-perks/v1/pages") or []
        ours = next((p for p in pages if str(p.get("name", "")).startswith(PAGE_NAME) and p.get("isEditable")), None)
        if ours:
            resp = await self.lol.lcu("PUT", f"/lol-perks/v1/pages/{ours['id']}", json={**ours, **body})
            page_id = ours["id"]
        else:
            resp = await self.lol.lcu("POST", "/lol-perks/v1/pages", json=body)
            page_id = (resp.json() or {}).get("id") if resp is not None and resp.status_code < 300 else None
        if resp is None or resp.status_code >= 300:
            detail = resp.text[:120] if resp is not None else "cliente fechado"
            if resp is not None and "max" in resp.text.lower():
                # Nunca sobrescrevemos páginas do usuário: ele escolhe qual ceder renomeando-a.
                raise CommandError(f'sem espaço para página nova: renomeie uma página de runas para "{PAGE_NAME}" '
                                   "no cliente e ela passa a ser usada")
            raise CommandError(f"o cliente recusou a página de runas: {detail}")
        if page_id:
            await self.lol.lcu("PUT", "/lol-perks/v1/currentpage", json=page_id)
        return name
