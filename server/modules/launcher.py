"""Lançador de apps, pastas, sites, scripts e macros.

SEGURANÇA: o iPad só envia o `id` de um botão. Caminhos, comandos e macros
vivem exclusivamente no config.json do PC. Nenhum comando arbitrário vindo
da rede é executado.

Tipos de botão (config.json -> "launcher" -> páginas -> "buttons"):
  app     target: caminho do .exe ou nome no PATH, args: lista opcional
  folder  target: caminho da pasta
  url     target: http(s)://... ou URI (spotify:, steam://, ms-settings:...)
  script  target: .bat/.cmd/.ps1/.sh/.py, args opcionais, "hidden": true p/ sem janela
  keys    keys: "ctrl+shift+esc"
  macro   steps: [{"keys": "..."} | {"text": "..."} | {"delay": ms}]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Any

from . import keys
from ..config import ROOT
from ..protocol import CommandError, Dispatcher

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _script_cmd(path: Path, args: list[str]) -> list[str]:
    ext = path.suffix.lower()
    if ext in (".bat", ".cmd"):
        return ["cmd.exe", "/c", str(path), *args]
    if ext == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path), *args]
    if ext == ".py":
        return [shutil.which("python") or "python", str(path), *args]
    if ext == ".sh":
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        if not Path(bash).exists() and not shutil.which("bash"):
            raise CommandError("bash não encontrado (instale Git for Windows ou WSL)")
        return [bash, str(path), *args]
    raise CommandError(f"tipo de script não suportado: {ext}")


class Launcher:
    def __init__(self, cfg: dict) -> None:
        self.buttons: dict[str, dict] = {}
        for page in cfg.get("launcher", []):
            for b in page.get("buttons", []):
                if "id" in b:
                    self.buttons[b["id"]] = b

    def public_pages(self, cfg: dict) -> list[dict]:
        """Versão enviada ao iPad: só id/label/ícone/cor, nunca caminhos."""
        pages = []
        for page in cfg.get("launcher", []):
            pages.append({
                "name": page.get("name", "Página"),
                "buttons": [{k: b[k] for k in ("id", "label", "icon", "color", "confirm") if k in b}
                            for b in page.get("buttons", [])],
            })
        return pages

    def run(self, v: Any) -> None:
        bid = v if isinstance(v, str) else (v or {}).get("id")
        b = self.buttons.get(bid)
        if b is None:
            raise CommandError("botão não configurado")
        kind = b.get("type")
        args = [_expand(a) for a in b.get("args", [])]
        cwd = _expand(b["cwd"]) if b.get("cwd") else None

        if kind == "app":
            target = _expand(b["target"])
            if args:
                subprocess.Popen([target, *args], cwd=cwd)  # noqa: S603
            else:
                os.startfile(target)  # noqa: S606 - resolve .lnk, apps do PATH, UWP shell:
        elif kind in ("folder", "url"):
            target = _expand(b["target"]) if kind == "folder" else b["target"]
            if kind == "url" and target.startswith(("http://", "https://")):
                webbrowser.open(target)
            else:
                os.startfile(target)  # noqa: S606
        elif kind == "script":
            path = Path(_expand(b["target"]))
            if not path.is_absolute():
                path = ROOT / path  # caminhos relativos partem da raiz do projeto
            if not path.exists():
                raise CommandError(f"script não encontrado: {path.name}")
            flags = CREATE_NO_WINDOW if b.get("hidden") else CREATE_NEW_CONSOLE
            subprocess.Popen(_script_cmd(path, args), cwd=cwd or str(path.parent),  # noqa: S603
                             creationflags=flags)
        elif kind == "keys":
            keys.press_combo(b["keys"])
        elif kind == "macro":
            keys.run_macro(b.get("steps", []))
        else:
            raise CommandError(f"tipo desconhecido: {kind}")


def register(d: Dispatcher, cfg: dict) -> Launcher:
    launcher = Launcher(cfg)
    d.on("launcher.run", launcher.run)
    return launcher
