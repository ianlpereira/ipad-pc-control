"""Controle de janelas e produtividade (atalhos nativos do Windows + screenshot)."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

from . import keys
from ..protocol import Dispatcher

# ação -> combinação de teclas do Windows
COMBOS = {
    "win.desktop_prev": "ctrl+win+left",
    "win.desktop_next": "ctrl+win+right",
    "win.desktop_new": "ctrl+win+d",
    "win.task_view": "win+tab",
    "win.minimize_all": "win+d",
    "win.close": "alt+f4",
    "win.snip": "win+shift+s",
    "win.maximize": "win+up",
    "win.snap_left": "win+left",
    "win.snap_right": "win+right",
    "win.explorer": "win+e",
}


def _combo_handler(combo: str):
    def handler(_v: Any = None) -> None:
        keys.press_combo(combo)
    return handler


def alt_tab(_v: Any = None) -> None:
    keys.press_combo("alt+tab")


def make_screenshot(cfg: dict):
    def screenshot(_v: Any = None) -> str:
        from PIL import ImageGrab  # import tardio: Pillow só carrega se usado

        folder = Path(cfg.get("screenshot_dir") or Path.home() / "Pictures" / "ControlCenter")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
        ImageGrab.grab(all_screens=True).save(path, "PNG")
        return str(path)
    return screenshot


def open_screenshots(cfg: dict):
    def handler(_v: Any = None) -> None:
        folder = Path(cfg.get("screenshot_dir") or Path.home() / "Pictures" / "ControlCenter")
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606
    return handler


def register(d: Dispatcher, cfg: dict) -> None:
    for name, combo in COMBOS.items():
        d.on(name, _combo_handler(combo))
    d.on("win.alt_tab", alt_tab)
    d.on("win.screenshot", make_screenshot(cfg))
    d.on("win.screenshots_folder", open_screenshots(cfg))
