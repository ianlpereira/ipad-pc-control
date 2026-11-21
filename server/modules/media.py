"""Mídia via SMTC (System Media Transport Controls) do Windows.

Funciona com qualquer app que aparece no "flyout" de mídia do Windows:
Spotify, navegadores (YouTube), Media Player, VLC 4, etc.
Se o SMTC falhar, os comandos caem para as teclas de mídia virtuais.

IMPORTANTE: o `comtypes` (usado pelo áudio) inicializa a thread principal
como STA. Operações assíncronas WinRT do SMTC disparadas de uma thread STA
sem bomba de mensagens NUNCA completam, o que congelava o broadcast de estado.
Por isso todo o SMTC roda num event loop próprio, numa thread MTA dedicada,
e cada chamada tem timeout.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Coroutine

from . import keys
from ..protocol import Dispatcher

log = logging.getLogger("media")

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    HAS_WINRT = True
except Exception as e:  # noqa: BLE001
    log.warning("winrt indisponível (%s); mídia só via teclas virtuais", e)
    HAS_WINRT = False

_STATUS = {4: "playing", 5: "paused", 3: "stopped", 2: "changing", 1: "opened", 0: "closed"}


class _MtaLoop:
    """Event loop asyncio rodando numa thread inicializada como MTA."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="winrt-mta", daemon=True).start()
        self._ready.wait(5)

    def _run(self) -> None:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # 0 = COINIT_MULTITHREADED
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    async def call(self, coro: Coroutine, timeout: float) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.TimeoutError:
            fut.cancel()
            raise


def _app_name(aumid: str) -> str:
    """'Spotify.exe' -> 'Spotify'; 'Chrome._crx_abc...' (PWA) -> 'Chrome'."""
    name = (aumid or "").split("!")[-1]
    name = name.split("._crx_")[0].split("_crx_")[0]
    name = name.removesuffix(".exe")
    if "." in name and " " not in name:  # IDs de pacote UWP: 'SpotifyAB.SpotifyMusic_zpdnekdrzrea0'
        name = name.split("_")[0].split(".")[-1]
    return name[:40]


class Media:
    def __init__(self) -> None:
        self._mgr = None
        self._art_key: str | None = None
        self._art_track: str | None = None
        self.art: tuple[bytes, str] | None = None  # (bytes, content-type) servidos em /media/art
        self._mta: _MtaLoop | None = None
        self._last: dict[str, Any] | None = None

    def _runner(self) -> _MtaLoop:
        if self._mta is None:
            self._mta = _MtaLoop()
        return self._mta

    # ---------- API pública (chamada do loop principal) ----------
    async def state(self) -> dict[str, Any] | None:
        if not HAS_WINRT:
            return None
        try:
            self._last = await self._runner().call(self._state(), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("SMTC não respondeu em 2 s; usando último estado")
            self._mgr = None
        return self._last

    async def toggle(self, _v: Any = None) -> None:
        await self._command("try_toggle_play_pause_async", "media_playpause")

    async def next(self, _v: Any = None) -> None:
        await self._command("try_skip_next_async", "media_next")

    async def prev(self, _v: Any = None) -> None:
        await self._command("try_skip_previous_async", "media_prev")

    async def _command(self, method: str, fallback_key: str) -> None:
        if not HAS_WINRT:
            keys.tap_vk(fallback_key)
            return
        try:
            await self._runner().call(self._do(method, fallback_key), timeout=2.0)
        except asyncio.TimeoutError:
            keys.tap_vk(fallback_key)

    # ---------- implementação (roda na thread MTA) ----------
    async def _session(self):
        if not HAS_WINRT:
            return None
        if self._mgr is None:
            self._mgr = await SessionManager.request_async()
        return self._mgr.get_current_session()

    async def _state(self) -> dict[str, Any] | None:
        try:
            s = await self._session()
            if s is None:
                return None
            props = await s.try_get_media_properties_async()
            info = s.get_playback_info()
            tl = s.get_timeline_properties()
            status = _STATUS.get(int(info.playback_status), "unknown")
            title = props.title or ""
            artist = props.artist or ""
            track_id = f"{s.source_app_user_model_id}|{title}|{artist}|{props.album_title}"
            if track_id != self._art_track:
                self._art_track = track_id
                await self._load_art(props, track_id)
            app = _app_name(s.source_app_user_model_id)
            duration = tl.end_time.total_seconds()
            position = tl.position.total_seconds()
            # Muitos apps só atualizam a timeline em eventos (play/pause/seek):
            # projeta a posição atual a partir de last_updated_time.
            if status == "playing" and tl.last_updated_time is not None:
                try:
                    elapsed = (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
                    if 0 < elapsed < 24 * 3600:
                        position += elapsed
                except (TypeError, ValueError):
                    pass
            if duration > 0:
                position = min(position, duration)
            return {
                "title": title, "artist": artist, "album": props.album_title or "",
                "app": app, "status": status, "art": self._art_key,
                "position": round(position), "duration": round(duration),
            }
        except Exception as e:  # noqa: BLE001
            log.debug("SMTC falhou: %s", e)
            self._mgr = None  # recria no próximo tick
            return None

    async def _load_art(self, props, track_id: str) -> None:
        self.art, self._art_key = None, None
        if props.thumbnail is None:
            return
        try:
            stream = await props.thumbnail.open_read_async()
            size = int(stream.size)
            if not 0 < size < 5_000_000:
                return
            buf = Buffer(size)
            result = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            data = bytes(memoryview(result))[: result.length]
            ctype = stream.content_type or "image/png"
            self.art = (data, ctype)
            self._art_key = hashlib.sha1(track_id.encode()).hexdigest()[:12]
        except Exception as e:  # noqa: BLE001
            log.debug("capa indisponível: %s", e)

    async def _do(self, method: str, fallback_key: str) -> None:
        try:
            s = await self._session()
            if s is not None and await getattr(s, method)():
                return
        except Exception as e:  # noqa: BLE001
            log.debug("SMTC %s falhou: %s", method, e)
        keys.tap_vk(fallback_key)


media = Media()


def register(d: Dispatcher, cfg: dict) -> None:
    d.on("media.toggle", media.toggle, push_state=True)
    d.on("media.next", media.next, push_state=True)
    d.on("media.prev", media.prev, push_state=True)
