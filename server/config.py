"""Carrega o config.json da raiz do projeto.

O config.json é criado pelo assistente de primeira configuração
(`start.bat` roda `python -m server.setup` quando ele não existe). Se o
servidor for iniciado sem o assistente (ex.: atalho de inicialização sem
console), o config.json é gerado a partir do config.example.json, com um
token aleatório.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("CC_CONFIG") or ROOT / "config.json")  # CC_CONFIG: outro arquivo (testes)
EXAMPLE_PATH = ROOT / "config.example.json"
WEB_DIR = ROOT / "web"

# Abas opcionais do painel. Desligada = a aba some do iPad e o módulo não roda em segundo plano.
DEFAULT_FEATURES: dict[str, bool] = {"lol": True, "football": True, "discord": True, "claude": True, "games": True}

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
    "features": DEFAULT_FEATURES,
    "launcher": [],
    "games": {},                    # {"hide": [...], "extra": [...]}; ver README (aba Jogos)
}


def read_example() -> dict[str, Any]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")) if EXAMPLE_PATH.exists() else {}


def write_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def feature_on(cfg: dict[str, Any], name: str) -> bool:
    return bool((cfg.get("features") or {}).get(name, DEFAULT_FEATURES.get(name, True)))


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        base = read_example()
        base["token"] = secrets.token_urlsafe(12)
        write_config(base)
        print(f"[config] config.json criado com valores padrão em {CONFIG_PATH}")
        print("[config] Para configurar passo a passo, rode: start.bat --setup")

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not data.get("token"):
        data["token"] = secrets.token_urlsafe(12)
        write_config(data)

    cfg = dict(DEFAULTS)
    cfg.update(data)
    cfg["features"] = {**DEFAULT_FEATURES, **(data.get("features") or {})}
    # Overrides por variável de ambiente (útil para testar só em localhost).
    if os.environ.get("CC_HOST"):
        cfg["host"] = os.environ["CC_HOST"]
    if os.environ.get("CC_PORT"):
        cfg["port"] = int(os.environ["CC_PORT"])
    return cfg
