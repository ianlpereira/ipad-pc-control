"""Roteador de comandos do WebSocket.

Formato de entrada:  {"t": "cmd", "a": "audio.master.set", "v": 0.5, "id": 12}
Formato de saída:    {"t": "ack", "id": 12, "ok": true, "r": <resultado>}
                     {"t": "ack", "id": 12, "ok": false, "err": "mensagem"}

Cada módulo registra seus handlers com `dispatcher.on("nome", func)`.
Handlers podem ser síncronos ou `async`; síncronos rodam no executor
informado (ex.: thread COM do áudio) para nunca travar o event loop.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from concurrent.futures import Executor
from typing import Any, Awaitable, Callable

log = logging.getLogger("protocol")

Handler = Callable[[Any], Any]


class CommandError(Exception):
    """Erro de negócio que vai direto para o toast do iPad."""


class Dispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, tuple[Handler, Executor | None, bool]] = {}
        self._with_peer: set[str] = set()  # handlers que recebem (valor, ip_do_cliente)
        # Chamado após comandos que mudam estado (ex.: volume) para empurrar o
        # estado novo a todos os clientes sem esperar o próximo tick.
        self.on_state_change: Callable[[], Awaitable[None]] | None = None

    def on(self, name: str, func: Handler, executor: Executor | None = None, push_state: bool = False,
           with_peer: bool = False) -> None:
        self._handlers[name] = (func, executor, push_state)
        if with_peer:
            self._with_peer.add(name)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    async def handle(self, msg: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        mid = msg.get("id")
        action = msg.get("a")
        entry = self._handlers.get(action) if isinstance(action, str) else None
        if entry is None:
            return {"t": "ack", "id": mid, "ok": False, "err": f"ação desconhecida: {action}"}

        func, executor, push_state = entry
        args: tuple[Any, ...] = (msg.get("v"),)
        if action in self._with_peer:
            args = (msg.get("v"), peer)
        try:
            if inspect.iscoroutinefunction(func):
                result = await func(*args)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(executor, func, *args)
        except CommandError as e:
            return {"t": "ack", "id": mid, "ok": False, "err": str(e)}
        except Exception as e:  # noqa: BLE001 - qualquer falha vira toast, servidor segue vivo
            log.exception("falha em %s", action)
            return {"t": "ack", "id": mid, "ok": False, "err": f"{type(e).__name__}: {e}"}

        if push_state and self.on_state_change is not None:
            asyncio.create_task(self.on_state_change())
        return {"t": "ack", "id": mid, "ok": True, "r": result}
