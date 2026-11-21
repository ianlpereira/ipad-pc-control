"""Energia e sistema: desligar, reiniciar, suspender, bloquear, explorer, monitor.

Ações destrutivas exigem {"confirm": true} no payload. A UI só envia isso
depois do gesto de "segurar para confirmar"; um cliente que mande o comando
sem o campo recebe erro.
A execução é atrasada ~0,6 s para o ack chegar ao iPad antes do PC cair.
"""
from __future__ import annotations

import asyncio
import ctypes
import subprocess
from typing import Any, Callable

from ..protocol import CommandError, Dispatcher

CREATE_NO_WINDOW = 0x08000000
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170


def _run(cmd: list[str]) -> None:
    subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)  # noqa: S603


def _shutdown() -> None:
    _run(["shutdown", "/s", "/t", "0"])


def _restart() -> None:
    _run(["shutdown", "/r", "/t", "0"])


def _sleep() -> None:
    # SetSuspendState(hibernate=False, force=False, disableWakeEvents=False)
    # Se a hibernação estiver ativa, o Windows pode hibernar em vez de suspender;
    # desative com `powercfg /hibernate off` se quiser sempre suspensão.
    ctypes.WinDLL("powrprof").SetSuspendState(False, False, False)


def _lock() -> None:
    ctypes.windll.user32.LockWorkStation()


def _restart_explorer() -> None:
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], creationflags=CREATE_NO_WINDOW,  # noqa: S603, S607
                   capture_output=True, check=False)
    subprocess.Popen(["explorer.exe"])  # noqa: S603, S607


def _monitor_off() -> None:
    # PostMessage (não SendMessage) para não travar esperando janelas que não respondem.
    ctypes.windll.user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)


def _guarded(func: Callable[[], None], need_confirm: bool, delay: float):
    async def handler(v: Any = None) -> str:
        if need_confirm and not (isinstance(v, dict) and v.get("confirm") is True):
            raise CommandError("confirmação necessária")
        loop = asyncio.get_running_loop()
        loop.call_later(delay, lambda: loop.run_in_executor(None, func))
        return "ok"
    return handler


def register(d: Dispatcher, cfg: dict) -> None:
    d.on("power.shutdown", _guarded(_shutdown, True, 0.6))
    d.on("power.restart", _guarded(_restart, True, 0.6))
    d.on("power.sleep", _guarded(_sleep, True, 0.6))
    d.on("power.lock", _guarded(_lock, True, 0.1))
    d.on("power.explorer", _guarded(_restart_explorer, True, 0.1))
    d.on("power.monitor_off", _guarded(_monitor_off, False, 0.3))
