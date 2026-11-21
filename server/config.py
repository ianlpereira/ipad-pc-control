"""Carrega o config.json da raiz do projeto.

No primeiro start, se config.json não existir, ele é criado a partir de
config.example.json com um token aleatório. O token protege o WebSocket:
sem ele, ninguém na rede consegue mandar comandos para o PC.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
WEB_DIR = ROOT / "web"

DEFAULTS: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "require_token": False,         # True = WebSocket exige ?token= (recomendado em redes compartilhadas)
    "tick_seconds": 1.0,
    "lhm_url": "http://127.0.0.1:8085/data.json",
    "lhm_path": "",                 # pasta ou .exe do LibreHardwareMonitor; aberto ao iniciar se não estiver rodando
    "lhm_minimized": True,          # abre o LHM minimizado na bandeja (ajusta o .config dele)
    "gpu_name_filter": "",          # ex.: "9060" para forçar a GPU dedicada
    "net_exclude": ["loopback", "vethernet", "virtualbox", "vmware", "bluetooth"],
    "screenshot_dir": "",           # vazio = ~/Pictures/ControlCenter
    "audio_device_hide": [],        # ex.: ["steam streaming"] esconde saídas virtuais
    "night_dim": {"enabled": False, "start": "23:00", "end": "07:00", "opacity": 0.75},
    "launcher": [],
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        base = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")) if EXAMPLE_PATH.exists() else {}
        base["token"] = secrets.token_urlsafe(12)
        CONFIG_PATH.write_text(json.dumps(base, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[config] config.json criado com token novo em {CONFIG_PATH}")

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not data.get("token"):
        data["token"] = secrets.token_urlsafe(12)
        CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    cfg = dict(DEFAULTS)
    cfg.update(data)
    # Overrides por variável de ambiente (útil para testar só em localhost).
    if os.environ.get("CC_HOST"):
        cfg["host"] = os.environ["CC_HOST"]
    if os.environ.get("CC_PORT"):
        cfg["port"] = int(os.environ["CC_PORT"])
    return cfg
