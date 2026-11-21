"""Central de notificações e eventos para o iPad.

Qualquer módulo (inclusive de outras threads) chama:
    notifier.notify("lol", "Partida encontrada!", level="alert")
    notifier.event("switch_tab", tab="lol")

O main.py liga o notifier ao broadcast do Hub com `bind()`.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any, Awaitable, Callable

from ..protocol import Dispatcher

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class Notifier:
    def __init__(self, limit: int = 50) -> None:
        self.items: deque[dict[str, Any]] = deque(maxlen=limit)
        self._ids = itertools.count(1)
        self._broadcast: Broadcast | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, broadcast: Broadcast, loop: asyncio.AbstractEventLoop) -> None:
        self._broadcast, self._loop = broadcast, loop

    def _send(self, msg: dict[str, Any]) -> None:
        loop, bc = self._loop, self._broadcast
        if loop is None or bc is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(bc(msg))
        else:  # chamado de outra thread (COM, Discord, etc.)
            loop.call_soon_threadsafe(lambda: loop.create_task(bc(msg)))

    def notify(self, kind: str, title: str, body: str = "", level: str = "info", sound: bool = True) -> None:
        """level: info | ok | warn | alert (alert = destaque + som mais forte no iPad)."""
        item = {"id": next(self._ids), "kind": kind, "title": title, "body": body,
                "level": level, "sound": sound, "ts": time.time()}
        self.items.appendleft(item)
        self._send({"t": "notify", **item})

    def event(self, name: str, **data: Any) -> None:
        self._send({"t": "event", "name": name, **data})

    def snapshot(self) -> dict[str, Any]:
        return {"t": "notify_list", "items": list(self.items)}

    def clear(self, _v: Any = None) -> None:
        self.items.clear()
        self._send(self.snapshot())


notifier = Notifier()


def register(d: Dispatcher, cfg: dict) -> None:
    d.on("notify.clear", notifier.clear)
