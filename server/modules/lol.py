"""League of Legends: cliente (LCU) + partida ao vivo (Live Client Data API).

Ambas são APIs locais oficiais expostas pelo próprio jogo:
- LCU: https://127.0.0.1:<porta> com usuário "riot" e a senha do arquivo
  `lockfile` (formato "LeagueClient:pid:porta:senha:https"). Certificado autoassinado.
- Live Client Data: https://127.0.0.1:2999/liveclientdata/allgamedata, só
  durante a partida.

Só usamos informação que o jogador já vê na tela (nada de timers de inimigos),
de acordo com a política de aplicações de terceiros da Riot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import psutil

from . import audio as audio_mod
from .lol_live import LiveAnalyzer
from .notify import notifier
from ..protocol import CommandError, Dispatcher

log = logging.getLogger("lol")

CREATE_NO_WINDOW = 0x08000000
READY_CHECK_SECONDS = 12.0  # janela de aceite da fila


class Lol:
    def __init__(self, cfg: dict) -> None:
        c = cfg.get("lol") or {}
        self.dir = Path(os.path.expandvars(os.path.expanduser(c.get("dir") or r"C:\Riot Games\League of Legends")))
        self.riot_client = Path(os.path.expandvars(os.path.expanduser(
            c.get("riot_client") or r"C:\Riot Games\Riot Client\RiotClientServices.exe")))
        self.auto_accept: bool = bool(c.get("auto_accept", False))
        self.game_mode: dict = c.get("game_mode") or {}
        self._http = httpx.AsyncClient(verify=False, timeout=1.0)  # noqa: S501 - certificado local da Riot
        self._lcu: tuple[str, httpx.BasicAuth] | None = None
        self._lockfile_mtime = 0.0
        self.phase = "closed"
        self._accepted_once = False
        self._saved_volumes: dict[str, float] | None = None
        self._champ_alias_to_id: dict[str, int] = {}
        from .lol_builds import Builds  # import tardio: lol_builds referencia esta classe
        self.builds = Builds(self)
        self.analyzer = LiveAnalyzer(c.get("live"))
        self._mode = "CLASSIC"
        self._locked_champ = 0

    # ---------- LCU ----------
    def _read_lockfile(self) -> tuple[str, httpx.BasicAuth] | None:
        lf = self.dir / "lockfile"
        try:
            st = lf.stat()
        except OSError:
            self._lcu = None
            return None
        if self._lcu is None or st.st_mtime != self._lockfile_mtime:
            try:
                _name, _pid, port, password, proto = lf.read_text(encoding="utf-8").strip().split(":")
            except (OSError, ValueError):
                return None
            self._lcu = (f"{proto}://127.0.0.1:{port}", httpx.BasicAuth("riot", password))
            self._lockfile_mtime = st.st_mtime
            self._champ_alias_to_id = {}
        return self._lcu

    async def lcu(self, method: str, path: str, **kw: Any) -> httpx.Response | None:
        lcu = self._read_lockfile()
        if lcu is None:
            return None
        base, auth = lcu
        try:
            return await self._http.request(method, base + path, auth=auth, **kw)
        except httpx.HTTPError:
            return None

    async def _json(self, path: str) -> Any:
        r = await self.lcu("GET", path)
        if r is None or r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # ---------- estado ----------
    async def state(self) -> dict[str, Any]:
        prev = self.phase
        phase = await self._json("/lol-gameflow/v1/gameflow-phase")
        self.phase = phase if isinstance(phase, str) else "closed"
        out: dict[str, Any] = {"phase": self.phase, "auto_accept": self.auto_accept,
                               "ready_check": None, "queue": None, "game": None}

        if self.phase == "Matchmaking":
            s = await self._json("/lol-matchmaking/v1/search")
            if isinstance(s, dict):
                out["queue"] = {"time": s.get("timeInQueue", 0), "estimated": s.get("estimatedQueueTime", 0)}

        if self.phase == "ReadyCheck":
            rc = await self._json("/lol-matchmaking/v1/ready-check")
            if isinstance(rc, dict):
                resp = rc.get("playerResponse", "None")
                out["ready_check"] = {"response": resp,
                                      "remaining": max(0.0, READY_CHECK_SECONDS - float(rc.get("timer", 0)))}
                if prev != "ReadyCheck":
                    self._accepted_once = False
                    if self.auto_accept:
                        notifier.notify("lol", "Partida encontrada", "Aceita automaticamente", "ok")
                    else:
                        notifier.notify("lol", "Partida encontrada!", "Toque em ACEITAR", "alert")
                if self.auto_accept and resp == "None" and not self._accepted_once:
                    self._accepted_once = True
                    await self.lcu("POST", "/lol-matchmaking/v1/ready-check/accept")

        if self.phase == "ChampSelect":
            out["select"] = await self._champ_select(prev != "ChampSelect")

        if self.phase == "InProgress":
            out["game"] = await self._live_game()
            g = out["game"]
            if g and g.get("champion_id"):
                self.builds.set_target(g["champion_id"], g.get("position", ""), g.get("mode", "CLASSIC"))

        if self.phase in ("closed", "None", "Lobby", "Matchmaking", "ReadyCheck"):
            self.builds.clear()
            self._locked_champ = 0
        out["build"] = self.builds.state()

        if prev != self.phase:
            await self._on_phase_change(prev, self.phase)
        return out

    async def _champ_select(self, entering: bool) -> dict[str, Any] | None:
        if entering:
            gf = await self._json("/lol-gameflow/v1/session")
            self._mode = str((((gf or {}).get("gameData") or {}).get("queue") or {}).get("gameMode") or "CLASSIC")
            if self.game_mode.get("switch_tab"):
                notifier.event("switch_tab", tab=self.game_mode["switch_tab"])
        s = await self._json("/lol-champ-select/v1/session")
        if not isinstance(s, dict):
            return None
        cell = s.get("localPlayerCellId")
        me = next((p for p in s.get("myTeam") or [] if p.get("cellId") == cell), None)
        if me is None:
            return None
        locked = int(me.get("championId") or 0)
        champ = locked or int(me.get("championPickIntent") or 0)
        position = str(me.get("assignedPosition") or "")
        self.builds.set_target(champ, position, self._mode)
        name = self.builds.champion_name(champ) if champ else ""
        if locked and locked != self._locked_champ:
            self._locked_champ = locked
            notifier.notify("lol", f"{name or 'Campeão'} confirmado", "Build e runas na aba LoL", "info", sound=False)
        return {"champion_id": champ, "champion": name, "locked": bool(locked),
                "position": position, "mode": self._mode}

    async def _live_game(self) -> dict[str, Any] | None:
        try:
            r = await self._http.get("https://127.0.0.1:2999/liveclientdata/allgamedata")
            if r.status_code != 200:
                return None
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None  # partida ainda carregando
        ap = data.get("activePlayer") or {}
        my_ids = {ap.get("riotId"), ap.get("summonerName"), ap.get("riotIdGameName")} - {None, ""}
        players = data.get("allPlayers") or []
        me = next((p for p in players if {p.get("riotId"), p.get("summonerName"), p.get("riotIdGameName")} & my_ids), None)
        kills = {"ORDER": 0, "CHAOS": 0}
        for p in players:
            kills[p.get("team", "ORDER")] = kills.get(p.get("team", "ORDER"), 0) + int((p.get("scores") or {}).get("kills", 0))
        sc = (me or {}).get("scores") or {}
        raw = (me or {}).get("rawChampionName", "")
        live = None
        try:  # a análise rica nunca pode derrubar os campos básicos abaixo
            live = self.analyzer.update(data, self.builds, self.builds.build)
            for a in self.analyzer.pop_alerts():
                notifier.notify("lol", a["title"], a["body"], a["level"], sound=a["sound"])
        except Exception:  # noqa: BLE001
            log.exception("falha na análise ao vivo")
        return {
            "live": live,
            "time": round(float((data.get("gameData") or {}).get("gameTime", 0))),
            "champion": (me or {}).get("championName", ""),
            "champion_id": await self._champion_id(raw.rsplit("_", 1)[-1] if raw else ""),
            "level": ap.get("level", (me or {}).get("level", 0)),
            "gold": ap.get("currentGold", 0),
            "kills": sc.get("kills", 0), "deaths": sc.get("deaths", 0), "assists": sc.get("assists", 0),
            "cs": sc.get("creepScore", 0), "ward_score": sc.get("wardScore", 0),
            "my_team": (me or {}).get("team", "ORDER"),
            "position": str((me or {}).get("position") or "").lower(),
            "mode": str((data.get("gameData") or {}).get("gameMode") or "CLASSIC"),
            "blue_kills": kills.get("ORDER", 0), "red_kills": kills.get("CHAOS", 0),
        }

    async def _champion_id(self, alias: str) -> int | None:
        if not alias:
            return None
        if not self._champ_alias_to_id:
            summary = await self._json("/lol-game-data/assets/v1/champion-summary.json")
            if isinstance(summary, list):
                self._champ_alias_to_id = {c.get("alias", "").lower(): c.get("id") for c in summary}
        return self._champ_alias_to_id.get(alias.lower())

    async def champion_icon(self, champ_id: int) -> tuple[bytes, str] | None:
        r = await self.lcu("GET", f"/lol-game-data/assets/v1/champion-icons/{int(champ_id)}.png")
        if r is None or r.status_code != 200:
            return None
        return r.content, r.headers.get("content-type", "image/png")

    # ---------- modo jogo ----------
    async def _on_phase_change(self, prev: str, new: str) -> None:
        log.info("fase do LoL: %s -> %s", prev, new)
        if prev == "InProgress":
            self.analyzer.reset()
        if new == "InProgress":
            await self._enter_game_mode()
        elif prev in ("InProgress", "WaitingForStats", "PreEndOfGame") and new not in ("InProgress",):
            await self._exit_game_mode()
        if new == "InProgress":
            notifier.notify("lol", "Partida começou", "Boa sorte!", "info", sound=False)
        elif new == "EndOfGame":
            notifier.notify("lol", "Partida terminou", "", "info", sound=False)

    async def _enter_game_mode(self) -> None:
        if self.game_mode.get("switch_tab"):
            notifier.event("switch_tab", tab=self.game_mode["switch_tab"])
        volumes: dict[str, float] = self.game_mode.get("volumes") or {}
        if not volumes or self._saved_volumes is not None:
            return
        loop = asyncio.get_running_loop()

        def apply() -> dict[str, float]:
            current = {a["key"]: a["volume"] for a in audio_mod.audio.state()["apps"]}
            saved = {}
            for key, vol in volumes.items():
                key = key.lower()
                if key in current:
                    saved[key] = current[key]
                    audio_mod.audio.set_app({"key": key, "volume": vol})
            return saved

        self._saved_volumes = await loop.run_in_executor(audio_mod.COM_EXECUTOR, apply)
        if self._saved_volumes:
            notifier.notify("lol", "Modo jogo ativado",
                            ", ".join(f"{k.removesuffix('.exe')} {int(volumes[k] * 100)}%" for k in self._saved_volumes),
                            "info", sound=False)

    async def _exit_game_mode(self) -> None:
        saved, self._saved_volumes = self._saved_volumes, None
        if not saved:
            return
        loop = asyncio.get_running_loop()

        def restore() -> None:
            for key, vol in saved.items():
                try:
                    audio_mod.audio.set_app({"key": key, "volume": vol})
                except CommandError:
                    pass  # app fechou durante a partida

        await loop.run_in_executor(audio_mod.COM_EXECUTOR, restore)
        notifier.notify("lol", "Modo jogo desativado", "Volumes restaurados", "info", sound=False)

    async def shutdown(self) -> None:
        try:
            await self._exit_game_mode()
        except Exception:  # noqa: BLE001 - no desligamento, só tenta
            log.exception("falha ao restaurar volumes")
        await self._http.aclose()
        await self.builds._http.aclose()

    # ---------- comandos ----------
    async def accept(self, _v: Any = None) -> None:
        r = await self.lcu("POST", "/lol-matchmaking/v1/ready-check/accept")
        if r is None or r.status_code >= 400:
            raise CommandError("nenhuma partida para aceitar")

    async def decline(self, _v: Any = None) -> None:
        r = await self.lcu("POST", "/lol-matchmaking/v1/ready-check/decline")
        if r is None or r.status_code >= 400:
            raise CommandError("nenhuma partida para recusar")

    async def set_auto_accept(self, v: Any) -> bool:
        self.auto_accept = (not self.auto_accept) if v is None else bool(v)
        return self.auto_accept

    async def close_client(self, v: Any = None) -> None:
        if not (isinstance(v, dict) and v.get("confirm")):
            raise CommandError("confirmação necessária")
        if self.phase in ("InProgress", "GameStart", "Reconnect"):
            raise CommandError("não dá para fechar o cliente durante a partida")
        await self.lcu("POST", "/process-control/v1/process/quit")
        await asyncio.sleep(3)
        # Garante: cliente do LoL e Riot Client (ambos pesam em CPU/RAM parados).
        for name in ("LeagueClient.exe", "LeagueClientUx.exe", "LeagueClientUxRender.exe",
                     "Riot Client.exe", "RiotClientServices.exe"):
            subprocess.run(["taskkill", "/im", name, "/f"], capture_output=True,  # noqa: S603, S607
                           creationflags=CREATE_NO_WINDOW, check=False)
        self._lcu = None

    async def open_client(self, _v: Any = None) -> None:
        if any((p.info.get("name") or "").lower() == "leagueclient.exe" for p in psutil.process_iter(["name"])):
            raise CommandError("o cliente já está aberto")
        if not self.riot_client.is_file():
            raise CommandError(f"Riot Client não encontrado: {self.riot_client}")
        subprocess.Popen([str(self.riot_client), "--launch-product=league_of_legends",  # noqa: S603
                          "--launch-patchline=live"])


_lol: Lol | None = None


def get() -> Lol:
    assert _lol is not None
    return _lol


def register(d: Dispatcher, cfg: dict) -> Lol:
    global _lol
    _lol = Lol(cfg)
    d.on("lol.accept", _lol.accept)
    d.on("lol.decline", _lol.decline)
    d.on("lol.auto_accept", _lol.set_auto_accept)
    d.on("lol.close_client", _lol.close_client)
    d.on("lol.open_client", _lol.open_client)
    d.on("lol.import_runes", _lol.builds.import_runes)
    return _lol
