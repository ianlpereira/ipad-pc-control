"""Áudio via Core Audio (pycaw/comtypes).

- Volume mestre + mute do dispositivo de saída padrão.
- Mixer por aplicativo (sessões de áudio agrupadas pelo nome do .exe).
- Lista de saídas ativas e troca do dispositivo padrão (IPolicyConfig,
  exposto pelo pycaw em AudioUtilities.SetDefaultDevice).

Todo acesso COM roda numa ÚNICA thread dedicada (COM_EXECUTOR) que chamou
CoInitialize. Objetos COM não devem cruzar threads de apartamento.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import comtypes
from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
from pycaw.utils import AudioUtilities

from ..protocol import CommandError, Dispatcher

log = logging.getLogger("audio")

COM_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="com-audio",
                                  initializer=comtypes.CoInitialize)

# Nomes amigáveis para executáveis comuns (o resto usa o nome do .exe).
PRETTY = {
    "discord.exe": "Discord", "spotify.exe": "Spotify", "chrome.exe": "Chrome", "msedge.exe": "Edge",
    "firefox.exe": "Firefox", "steam.exe": "Steam", "steamwebhelper.exe": "Steam", "vlc.exe": "VLC",
    "obs64.exe": "OBS", "teams.exe": "Teams", "ms-teams.exe": "Teams", "zoom.exe": "Zoom",
    "brave.exe": "Brave", "opera.exe": "Opera", "wmplayer.exe": "Media Player",
    "microsoft.media.player.exe": "Media Player", "applemusic.exe": "Apple Music",
}


class Audio:
    """Somente use estes métodos dentro do COM_EXECUTOR."""

    def __init__(self) -> None:
        self.hide: list[str] = []  # trechos de nome de dispositivos a esconder (config "audio_device_hide")
        self._dev = None
        self._dev_id: str | None = None
        self._devices_cache: list[dict] = []
        self._devices_at = 0.0

    # ---------- dispositivo padrão ----------
    def _default(self):
        """Retorna o AudioDevice padrão, recriando-o se o padrão mudou."""
        enum = AudioUtilities.GetDeviceEnumerator()
        raw = enum.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value)
        dev_id = raw.GetId()
        if dev_id != self._dev_id or self._dev is None:
            self._dev = AudioUtilities.CreateDevice(raw)
            self._dev_id = dev_id
        return self._dev

    def _endpoint(self):
        return self._default().EndpointVolume

    def devices(self, force: bool = False) -> list[dict]:
        # Ler property stores é relativamente caro; cache de 5 s.
        if not force and time.monotonic() - self._devices_at < 5:
            return self._devices_cache
        out = []
        for d in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value):
            if d is None:
                continue
            name = d.FriendlyName or d.id
            if any(h in name.lower() for h in self.hide):
                continue
            out.append({"id": d.id, "name": name})
        out.sort(key=lambda d: d["name"].lower())
        self._devices_cache = out
        self._devices_at = time.monotonic()
        return out

    # ---------- sessões por app ----------
    @staticmethod
    def _sessions() -> dict[str, dict]:
        groups: dict[str, dict] = {}
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.Process is None:
                    key, name = "__system__", "Sons do sistema"
                else:
                    exe = s.Process.name().lower()
                    key, name = exe, PRETTY.get(exe, exe.removesuffix(".exe").capitalize())
                vol = s.SimpleAudioVolume
                g = groups.setdefault(key, {"key": key, "name": name, "items": []})
                g["items"].append(vol)
            except Exception:  # processo morreu entre a enumeração e a leitura
                continue
        return groups

    # ---------- estado ----------
    def state(self) -> dict[str, Any]:
        try:
            ep = self._endpoint()
            master = round(ep.GetMasterVolumeLevelScalar(), 3)
            muted = bool(ep.GetMute())
        except Exception as e:  # noqa: BLE001 - sem dispositivo de saída
            log.debug("sem endpoint: %s", e)
            master, muted = 0.0, True

        apps = []
        for g in self._sessions().values():
            first = g["items"][0]
            apps.append({
                "key": g["key"], "name": g["name"],
                "volume": round(first.GetMasterVolume(), 3),
                "muted": bool(first.GetMute()),
            })
        apps.sort(key=lambda a: (a["key"] == "__system__", a["name"].lower()))
        return {
            "master": master, "muted": muted, "device": self._dev_id,
            "devices": self.devices(), "apps": apps,
        }

    # ---------- comandos ----------
    def set_master(self, v: Any) -> None:
        self._endpoint().SetMasterVolumeLevelScalar(_clamp(v), None)

    def mute_master(self, v: Any) -> None:
        ep = self._endpoint()
        new = (not ep.GetMute()) if v is None else bool(v)
        ep.SetMute(new, None)

    def set_app(self, v: Any) -> None:
        g = self._group(v)
        level = _clamp(v.get("volume"))
        for item in g["items"]:
            item.SetMasterVolume(level, None)

    def mute_app(self, v: Any) -> None:
        g = self._group(v)
        cur = bool(g["items"][0].GetMute())
        new = (not cur) if v.get("mute") is None else bool(v["mute"])
        for item in g["items"]:
            item.SetMute(new, None)

    def set_device(self, v: Any) -> None:
        dev_id = v if isinstance(v, str) else (v or {}).get("id")
        if dev_id not in {d["id"] for d in self.devices(force=True)}:
            raise CommandError("dispositivo de áudio não encontrado")
        AudioUtilities.SetDefaultDevice(dev_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
        self._dev = None  # força recriar o endpoint no próximo acesso

    def _group(self, v: Any) -> dict:
        if not isinstance(v, dict) or "key" not in v:
            raise CommandError("payload inválido")
        g = self._sessions().get(v["key"])
        if g is None:
            raise CommandError("app não está tocando áudio")
        return g


def _clamp(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        raise CommandError("volume inválido") from None


audio = Audio()


def register(d: Dispatcher, cfg: dict) -> None:
    audio.hide = [h.lower() for h in cfg.get("audio_device_hide", [])]
    ex = COM_EXECUTOR
    d.on("audio.master.set", audio.set_master, ex, push_state=True)
    d.on("audio.master.mute", audio.mute_master, ex, push_state=True)
    d.on("audio.app.set", audio.set_app, ex, push_state=True)
    d.on("audio.app.mute", audio.mute_app, ex, push_state=True)
    d.on("audio.device.set", audio.set_device, ex, push_state=True)
