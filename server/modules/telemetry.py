"""Telemetria: CPU, RAM, rede (psutil) + temperaturas/GPU (LibreHardwareMonitor).

O LibreHardwareMonitor (LHM) precisa estar aberto como administrador com
"Options > Remote Web Server > Run" ativo (porta 8085). Ele expõe
http://127.0.0.1:8085/data.json com a árvore de sensores. Se o LHM não
estiver rodando, os campos de GPU/temperatura vão como null e a UI mostra "—".
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import httpx
import psutil

log = logging.getLogger("telemetry")

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_user32 = ctypes.WinDLL("user32")
_user32.GetForegroundWindow.restype = wintypes.HWND


def _lhm_set_start_minimized(config_path: Path) -> None:
    """Liga "Start Minimized" e "Minimize To Tray" no LibreHardwareMonitor.config.

    Só pode ser feito com o LHM FECHADO: ao sair, ele regrava o arquivo com as
    opções que tem em memória. Como as gravamos antes de abri-lo, elas persistem.
    """
    import xml.etree.ElementTree as ET

    wanted = {"startMinMenuItem": "true", "minTrayMenuItem": "true"}
    try:
        tree = ET.parse(config_path)
        settings = tree.getroot().find("appSettings")
        if settings is None:
            return
        changed = False
        for key, value in wanted.items():
            node = settings.find(f"add[@key='{key}']")
            if node is None:
                ET.SubElement(settings, "add", {"key": key, "value": value})
                changed = True
            elif node.get("value") != value:
                node.set("value", value)
                changed = True
        if changed:
            tree.write(config_path, encoding="utf-8", xml_declaration=True)
            log.info("LibreHardwareMonitor configurado para iniciar minimizado na bandeja")
    except (OSError, ET.ParseError) as e:
        log.warning("não foi possível ajustar %s: %s", config_path.name, e)


def ensure_lhm_running(path: str, minimized: bool = True) -> None:
    """Abre o LibreHardwareMonitor se ele ainda não estiver rodando.

    `path` pode ser a pasta do LHM ou o .exe. O LHM exige admin, então o
    Windows mostra o prompt do UAC (ShellExecute eleva automaticamente).
    Bloqueia até o UAC ser respondido: chame fora do event loop.
    """
    if not path:
        return
    exe = Path(os.path.expandvars(os.path.expanduser(path)))
    if exe.is_dir():
        exe = exe / "LibreHardwareMonitor.exe"
    if not exe.is_file():
        log.warning("LibreHardwareMonitor não encontrado em %s (config lhm_path)", exe)
        return
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == "librehardwaremonitor.exe":
            log.info("LibreHardwareMonitor já está rodando")
            return

    if minimized:
        _lhm_set_start_minimized(exe.with_name("LibreHardwareMonitor.config"))
    # SW_SHOWMINNOACTIVE (7) como reforço: mesmo sem a opção, abre minimizado e sem roubar o foco.
    show = 7 if minimized else 1
    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", str(exe), None, str(exe.parent), show)
    if rc > 32:
        log.info("LibreHardwareMonitor iniciado: %s", exe)
    else:
        log.warning("não foi possível abrir o LibreHardwareMonitor (código %s; UAC recusado?)", rc)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM.search(str(value))
    return float(m.group(0).replace(",", ".")) if m else None


def active_window_title() -> str:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


# Categorias do LHM (nó pai do sensor) -> tipo, para versões sem campo "Type".
_CATEGORY_TYPE = {
    "temperatures": "Temperature", "load": "Load", "powers": "Power", "data": "Data",
    "smalldata": "SmallData", "clocks": "Clock", "fans": "Fan", "voltages": "Voltage",
}


class Telemetry:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.lhm_url: str = cfg.get("lhm_url") or ""
        self.gpu_filter: str = (cfg.get("gpu_name_filter") or "").lower()
        self.net_exclude = [s.lower() for s in cfg.get("net_exclude", [])]
        self._http = httpx.AsyncClient(timeout=0.4)
        self._last_net: tuple[float, int, int] | None = None
        self._lhm_ok = False
        self._lhm_fail_until = 0.0
        psutil.cpu_percent(None)  # 1ª leitura sempre é 0; "aquece" o contador

    async def close(self) -> None:
        await self._http.aclose()

    # ---------- psutil ----------
    def _net_rates(self) -> tuple[float, float]:
        counters = psutil.net_io_counters(pernic=True)
        rx = tx = 0
        for name, c in counters.items():
            if any(x in name.lower() for x in self.net_exclude):
                continue
            rx += c.bytes_recv
            tx += c.bytes_sent
        now = time.monotonic()
        last = self._last_net
        self._last_net = (now, rx, tx)
        if last is None or now <= last[0]:
            return 0.0, 0.0
        dt = now - last[0]
        return max(0.0, (rx - last[1]) / dt), max(0.0, (tx - last[2]) / dt)

    def sample_basic(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        down, up = self._net_rates()
        return {
            "cpu": psutil.cpu_percent(None),
            "ram": vm.percent,
            "ram_used": round(vm.used / 2**30, 1),
            "ram_total": round(vm.total / 2**30, 1),
            "net_down": round(down),
            "net_up": round(up),
            "uptime": int(time.time() - psutil.boot_time()),
            "window": active_window_title()[:120],
        }

    # ---------- LibreHardwareMonitor ----------
    async def _fetch_lhm(self) -> dict | None:
        if not self.lhm_url or time.monotonic() < self._lhm_fail_until:
            return None
        try:
            r = await self._http.get(self.lhm_url)
            r.raise_for_status()
            self._lhm_ok = True
            return r.json()
        except Exception as e:  # noqa: BLE001
            if self._lhm_ok:
                log.warning("LibreHardwareMonitor indisponível: %s", e)
            self._lhm_ok = False
            self._lhm_fail_until = time.monotonic() + 5  # tenta de novo em 5 s (LHM pode estar abrindo)
            return None

    @staticmethod
    def _collect(tree: dict) -> list[dict]:
        """Achata a árvore em lista de hardwares com seus sensores.

        Estrutura do LHM: raiz -> computador -> HARDWARE -> categoria -> sensor
        (sub-hardwares podem adicionar níveis; ficam sob o hardware de nível 2).
        """
        hardware: list[dict] = []

        def walk(node: dict, depth: int, hw: dict | None, category: str) -> None:
            children = node.get("Children") or []
            if depth == 2:
                hw = {"name": node.get("Text", ""), "image": node.get("ImageURL", ""), "sensors": []}
                hardware.append(hw)
            if hw is not None and not children and depth >= 4:
                stype = node.get("Type") or _CATEGORY_TYPE.get(category.lower().replace(" ", ""), "")
                hw["sensors"].append({
                    "name": node.get("Text", ""),
                    "type": stype,
                    "id": node.get("SensorId", ""),
                    "value": _num(node.get("RawValue", node.get("Value"))),
                })
                return
            for child in children:
                walk(child, depth + 1, hw, node.get("Text", "") if depth >= 2 else category)

        walk(tree, 0, None, "")
        return hardware

    @staticmethod
    def _kind(hw: dict) -> str:
        ids = " ".join(s["id"] for s in hw["sensors"][:3]).lower()
        img = hw["image"].lower()
        name = hw["name"].lower()
        if "cpu" in ids or "cpu.png" in img:
            return "cpu"
        if "gpu" in ids or any(x in img for x in ("ati", "nvidia", "amd", "intel")) or "radeon" in name:
            return "gpu"
        return "other"

    @staticmethod
    def _pick(sensors: list[dict], stype: str, names: list[str], fallback_max: bool = False) -> float | None:
        of_type = [s for s in sensors if s["type"].lower() == stype.lower() and s["value"] is not None]
        for wanted in names:
            for s in of_type:
                if s["name"].lower() == wanted.lower():
                    return s["value"]
        for wanted in names:
            for s in of_type:
                if wanted.lower() in s["name"].lower():
                    return s["value"]
        if fallback_max and of_type:
            return max(s["value"] for s in of_type)
        return None

    def _choose_gpu(self, gpus: list[dict]) -> dict | None:
        if not gpus:
            return None
        if self.gpu_filter:
            for g in gpus:
                if self.gpu_filter in g["name"].lower():
                    return g
        # Heurística: GPU dedicada tem mais VRAM total; iGPU (Ryzen "Radeon(TM) Graphics") vem por último.
        def score(g: dict) -> float:
            vram = self._pick(g["sensors"], "SmallData", ["GPU Memory Total", "D3D Dedicated Memory Total"]) or 0
            penalty = -1e6 if "(tm) graphics" in g["name"].lower() else 0
            return vram + penalty
        return max(gpus, key=score)

    async def sample_lhm(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lhm": False, "cpu_temp": None, "cpu_power": None, "gpu": None, "gpu_temp": None,
            "gpu_hotspot": None, "gpu_power": None, "vram_used": None, "vram_total": None, "gpu_name": None,
        }
        tree = await self._fetch_lhm()
        if not tree:
            return out
        hw = self._collect(tree)
        cpus = [h for h in hw if self._kind(h) == "cpu"]
        gpus = [h for h in hw if self._kind(h) == "gpu"]
        out["lhm"] = True
        if cpus:
            s = cpus[0]["sensors"]
            out["cpu_temp"] = self._pick(s, "Temperature",
                                         ["Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Tctl", "Tdie", "Package"],
                                         fallback_max=True)
            out["cpu_power"] = self._pick(s, "Power", ["Package", "CPU Package"])
        gpu = self._choose_gpu(gpus)
        if gpu:
            s = gpu["sensors"]
            out["gpu_name"] = gpu["name"]
            out["gpu"] = self._pick(s, "Load", ["GPU Core", "D3D 3D"])
            out["gpu_temp"] = self._pick(s, "Temperature", ["GPU Core", "GPU Edge"], fallback_max=True)
            out["gpu_hotspot"] = self._pick(s, "Temperature", ["GPU Hot Spot"])
            out["gpu_power"] = self._pick(s, "Power", ["GPU Package", "GPU Core", "GPU Power"])
            out["vram_used"] = self._pick(s, "SmallData", ["GPU Memory Used", "D3D Dedicated Memory Used"])
            out["vram_total"] = self._pick(s, "SmallData", ["GPU Memory Total", "D3D Dedicated Memory Total"])
        return out

    async def sample(self) -> dict[str, Any]:
        data = self.sample_basic()
        data.update(await self.sample_lhm())
        for k, v in list(data.items()):
            if isinstance(v, float):
                data[k] = round(v, 1)
        return data
