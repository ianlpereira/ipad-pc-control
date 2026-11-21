"""Brilho dos monitores via DDC/CI (biblioteca monitorcontrol).

DDC/CI precisa estar ativado no menu OSD do monitor. Leituras DDC são
lentas (~50-150 ms por monitor), então nada aqui roda no tick de 1 s:
o iPad pede `display.list` ao abrir a aba e manda `display.set` ao arrastar.
Tudo serializado num executor próprio para não acumular chamadas no barramento I2C.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..protocol import CommandError, Dispatcher

log = logging.getLogger("display")
DDC_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ddc")


def _monitors():
    from monitorcontrol import get_monitors
    return get_monitors()


def list_monitors(_v: Any = None) -> list[dict]:
    out = []
    for i, m in enumerate(_monitors()):
        item: dict[str, Any] = {"index": i, "name": f"Monitor {i + 1}", "brightness": None}
        try:
            with m:
                item["brightness"] = int(m.get_luminance())
                try:
                    caps = m.get_vcp_capabilities()
                    if caps.get("model"):
                        item["name"] = str(caps["model"])
                except Exception:  # noqa: BLE001 - capabilities é opcional e lento em alguns monitores
                    pass
        except Exception as e:  # noqa: BLE001
            log.info("monitor %d sem DDC/CI: %s", i, e)
        out.append(item)
    return out


def set_brightness(v: Any) -> None:
    if not isinstance(v, dict):
        raise CommandError("payload inválido")
    idx = int(v.get("index", 0))
    value = max(0, min(100, int(v.get("value", 50))))
    mons = _monitors()
    if not 0 <= idx < len(mons):
        raise CommandError("monitor não encontrado")
    try:
        with mons[idx]:
            mons[idx].set_luminance(value)
    except Exception as e:  # noqa: BLE001
        raise CommandError(f"DDC/CI falhou: {e}") from e


def open_night_light(_v: Any = None) -> None:
    # O Windows não expõe API pública para Luz Noturna; abrimos a página de config.
    os.startfile("ms-settings:nightlight")  # noqa: S606


def register(d: Dispatcher, cfg: dict) -> None:
    d.on("display.list", list_monitors, DDC_EXECUTOR)
    d.on("display.set", set_brightness, DDC_EXECUTOR)
    d.on("display.nightlight", open_night_light)
