"""Aba Jogos: reconhece os jogos instalados (Steam, Epic, GOG, Xbox/Microsoft Store),
lança com um toque e fecha pelo iPad.

SEGURANÇA: o iPad só envia o `id` opaco de um jogo. Comando, URI e pasta vêm do scan
feito pelo servidor (ou do config.json), nunca da rede. "Fechar jogo" só encerra
processos cujo .exe está dentro da pasta de instalação do próprio jogo.

config.json -> "games" (opcional):
  "hide":  ["trecho do nome ou id", ...]            esconde jogos da lista
  "extra": [{"label": "Valorant", "type": "app" | "url", "target": "...", "args": []}]
           jogos que o scan não enxerga (Riot, Battle.net, EA...)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import ROOT
from ..protocol import CommandError, Dispatcher

log = logging.getLogger("games")

CREATE_NO_WINDOW = 0x08000000
DATA_DIR = ROOT / "data"
STEAM_FALLBACK = r"C:\Program Files (x86)\Steam"
EPIC_MANIFESTS = r"%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests"
RUNNING_TTL = 3.0   # varredura de processos no máx. a cada 3 s
MAX_ART_BYTES = 3_000_000

# Ferramentas/runtimes que a Steam instala como "app" mas não são jogos.
STEAM_NOT_GAMES = {"228980", "1007", "1070560", "1391110", "1628350", "1493710", "2180100"}
STEAM_NOT_GAME_NAMES = re.compile(r"^(steamworks|steam linux runtime|proton|steam controller|steamvr)", re.I)


# ----------------------------------------------------------------------------- utilidades
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "x"


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _vdf_pairs(text: str) -> dict[str, str]:
    """Pares "chave" "valor" de um arquivo VDF/ACF (sem aninhamento; o último vence)."""
    out: dict[str, str] = {}
    for k, v in re.findall(r'"([^"\\]+)"\s+"((?:[^"\\]|\\.)*)"', text):
        out[k] = v.replace("\\\\", "\\").replace('\\"', '"')
    return out


def parse_library_paths(text: str) -> list[str]:
    """Pastas de biblioteca do libraryfolders.vdf (todas as unidades)."""
    return [v.replace("\\\\", "\\") for v in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', text)]


def _protected_roots() -> list[str]:
    roots = [os.environ.get(k) for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                                         "USERPROFILE", "APPDATA", "LOCALAPPDATA")]
    return [_norm(r) for r in roots if r]


def safe_dir(d: str | Path | None) -> bool:
    """Só pastas de instalação "de verdade" valem para detectar/fechar processos: nunca uma raiz,
    nem a pasta do Windows/Program Files/perfil, nem algo que contenha essas pastas."""
    if not d:
        return False
    p = _norm(d)
    if len(Path(p).parts) < 3:   # "D:\" ou "D:\Games" não servem
        return False
    sysroot = _norm(os.environ.get("SystemRoot", r"C:\Windows"))
    if p == sysroot or p.startswith(sysroot + os.sep):   # nada dentro do Windows
        return False
    for r in _protected_roots():
        if p == r or r.startswith(p + os.sep):
            return False
    return True


def exe_under(exe: str | None, directory: str) -> bool:
    return bool(exe) and _norm(exe).startswith(_norm(directory) + os.sep)


def make_game(gid: str, name: str, store: str, launch: dict, directory: str = "", art: str = "",
              icon_src: str = "") -> dict:
    return {"id": gid, "name": name.strip() or gid, "store": store, "launch": launch,
            "dir": directory if safe_dir(directory) else "", "art": art, "icon_src": icon_src}


# ----------------------------------------------------------------------------- Steam
def steam_root() -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            p = Path(winreg.QueryValueEx(k, "SteamPath")[0])
            if p.is_dir():
                return p
    except OSError:
        pass
    p = Path(STEAM_FALLBACK)
    return p if p.is_dir() else None


def _steam_art(root: Path, appid: str) -> str:
    cache = root / "appcache" / "librarycache"
    for name in ("library_600x900.jpg", "library_header.jpg"):
        for pattern in (f"{appid}/**/{name}", f"{appid}/{name}", f"{appid}_{name}"):
            hit = next(cache.glob(pattern), None)
            if hit:
                return str(hit)
    return ""


def scan_steam(root: Path | None) -> list[dict]:
    if root is None:
        return []
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        libs += [Path(p) for p in parse_library_paths(vdf.read_text(encoding="utf-8", errors="replace"))]
    except OSError:
        pass
    games: dict[str, dict] = {}
    seen_libs: set[str] = set()
    for lib in libs:
        if _norm(lib) in seen_libs:
            continue
        seen_libs.add(_norm(lib))
        apps = lib / "steamapps"
        try:
            manifests = sorted(apps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for mf in manifests:
            try:
                d = _vdf_pairs(mf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            appid, name = d.get("appid", ""), d.get("name", "")
            if not appid or not name or appid in STEAM_NOT_GAMES or STEAM_NOT_GAME_NAMES.match(name):
                continue
            try:
                if not int(d.get("StateFlags", "0")) & 4:   # 4 = totalmente instalado
                    continue
            except ValueError:
                continue
            games[appid] = make_game(
                f"steam-{appid}", name, "steam",
                {"kind": "uri", "target": f"steam://rungameid/{appid}"},
                str(apps / "common" / d.get("installdir", "")) if d.get("installdir") else "",
                _steam_art(root, appid))
    return list(games.values())


# ----------------------------------------------------------------------------- Epic
def scan_epic(manifest_dir: Path | None = None) -> list[dict]:
    manifest_dir = manifest_dir or Path(os.path.expandvars(EPIC_MANIFESTS))
    games = []
    try:
        items = sorted(manifest_dir.glob("*.item"))
    except OSError:
        return []
    for it in items:
        try:
            d = json.loads(it.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        cats = d.get("AppCategories") or []
        if cats and "games" not in cats:
            continue
        name, app = d.get("DisplayName"), d.get("AppName")
        ns, cid = d.get("CatalogNamespace"), d.get("CatalogItemId")
        if not (name and app and ns and cid) or d.get("bIsIncompleteInstall"):
            continue
        loc = d.get("InstallLocation") or ""
        exe = str(Path(loc) / d["LaunchExecutable"]) if loc and d.get("LaunchExecutable") else ""
        games.append(make_game(
            f"epic-{_slug(app)}", name, "epic",
            {"kind": "uri", "target": f"com.epicgames.launcher://apps/{ns}%3A{cid}%3A{app}?action=launch&silent=true"},
            loc, icon_src=exe))
    return games


# ----------------------------------------------------------------------------- GOG
def scan_gog() -> list[dict]:
    try:
        import winreg
    except ImportError:
        return []
    base = r"SOFTWARE\WOW6432Node\GOG.com"
    galaxy = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + r"\GalaxyClient\paths") as k:
            gp = Path(winreg.QueryValueEx(k, "client")[0]) / "GalaxyClient.exe"
            galaxy = str(gp) if gp.exists() else ""
    except OSError:
        pass
    games = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + r"\Games")
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    vals = {n: winreg.QueryValueEx(k, n)[0] for n in ("gameName", "gameID", "path", "exe")
                            if _has_value(k, n)}
            except OSError:
                continue
            name, gid, path, exe = vals.get("gameName"), vals.get("gameID"), vals.get("path"), vals.get("exe")
            if not (name and gid and path):
                continue
            if galaxy:
                launch = {"kind": "exe", "target": galaxy,
                          "args": ["/command=runGame", f"/gameId={gid}", f"/path={path}"], "cwd": str(Path(galaxy).parent)}
            elif exe:
                launch = {"kind": "exe", "target": exe, "args": [], "cwd": path}
            else:
                continue
            games.append(make_game(f"gog-{_slug(str(gid))}", name, "gog", launch, path, icon_src=exe or ""))
    return games


def _has_value(key: Any, name: str) -> bool:
    import winreg
    try:
        winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- Xbox / Microsoft Store
_XBOX_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @(Get-AppxPackage | Where-Object {
    $_.InstallLocation -and -not $_.IsFramework -and (Test-Path (Join-Path $_.InstallLocation 'MicrosoftGame.config'))
} | ForEach-Object {
    $m = Get-AppxPackageManifest $_
    $app = @($m.Package.Applications.Application | ForEach-Object { $_.Id })[0]
    [pscustomobject]@{ name = $_.Name; family = $_.PackageFamilyName; dir = $_.InstallLocation; app = $app }
})
ConvertTo-Json -Compress -InputObject $out
"""


def _powershell(command: str, timeout: float = 60) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip()


def _pretty_package_name(name: str) -> str:
    base = name.split(".", 1)[-1]
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", base).replace(".", " ")


def parse_game_config(xml_text: str, directory: str, fallback_name: str) -> tuple[str, str]:
    """(nome, caminho do logo) a partir do MicrosoftGame.config."""
    name = ""
    m = re.search(r'DefaultDisplayName="([^"]+)"', xml_text)
    if m and not m.group(1).lower().startswith("ms-resource:"):
        name = m.group(1)
    logo = ""
    m = re.search(r'Square(?:150x150|480x480|44x44)Logo="([^"]+)"', xml_text)
    if m:
        p = Path(directory) / m.group(1)
        logo = str(p) if p.is_file() else ""
    return name or _pretty_package_name(fallback_name), logo


def scan_xbox() -> list[dict]:
    raw = _powershell(_XBOX_PS)
    if not raw:
        return []
    try:
        pkgs = json.loads(raw)
    except ValueError:
        return []
    if isinstance(pkgs, dict):
        pkgs = [pkgs]
    games = []
    for p in pkgs:
        directory, family, app = p.get("dir") or "", p.get("family"), p.get("app") or "Game"
        if not (directory and family):
            continue
        try:
            xml = (Path(directory) / "MicrosoftGame.config").read_text(encoding="utf-8", errors="replace")
        except OSError:
            xml = ""
        name, logo = parse_game_config(xml, directory, p.get("name") or family)
        games.append(make_game(f"xbox-{_slug(family)}", name, "xbox",
                               {"kind": "uri", "target": f"shell:AppsFolder\\{family}!{app}"}, directory, logo))
    return games


# ----------------------------------------------------------------------------- extras (config.json)
def scan_extra(cfg_games: dict) -> list[dict]:
    games = []
    for e in cfg_games.get("extra") or []:
        label, target, kind = e.get("label"), e.get("target"), e.get("type", "app")
        if not (label and target) or kind not in ("app", "url"):
            continue
        target = os.path.expandvars(os.path.expanduser(target))
        args = [os.path.expandvars(a) for a in e.get("args") or []]
        is_file = kind == "app" and Path(target).is_absolute()
        launch = ({"kind": "exe", "target": target, "args": args, "cwd": str(Path(target).parent)}
                  if is_file and args else {"kind": "uri", "target": target})
        games.append(make_game(f"extra-{_slug(label)}", label, "extra", launch,
                               str(Path(target).parent) if is_file else "",
                               icon_src=target if is_file else ""))
    return games


def apply_hide(games: list[dict], hide: list[str]) -> list[dict]:
    needles = [h.lower() for h in hide if h]
    return [g for g in games if not any(n == g["id"].lower() or n in g["name"].lower() for n in needles)]


# ----------------------------------------------------------------------------- ícone do .exe (sem capa)
_icon_lock = threading.Lock()
_ICON_PS = (r"Add-Type -AssemblyName System.Drawing; "
            r"$i=[System.Drawing.Icon]::ExtractAssociatedIcon('{src}'); "
            r"if($i){{$b=$i.ToBitmap(); $b.Save('{dst}',[System.Drawing.Imaging.ImageFormat]::Png)}}")


def extract_icon(src: str, dst: Path) -> bool:
    if not (src and Path(src).is_file() and src.lower().endswith(".exe") and "'" not in src):
        return False
    with _icon_lock:   # um PowerShell por vez
        if dst.exists():
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        _powershell(_ICON_PS.format(src=src, dst=str(dst)), timeout=20)
        return dst.exists()


# ----------------------------------------------------------------------------- serviço
def scan_all(cfg_games: dict, steam: Path | None = None) -> list[dict]:
    found: dict[str, dict] = {}
    for scanner in (lambda: scan_steam(steam if steam else steam_root()), scan_epic, scan_gog, scan_xbox,
                    lambda: scan_extra(cfg_games)):
        try:
            for g in scanner():
                found.setdefault(g["id"], g)
        except Exception:  # noqa: BLE001 - uma loja com problema não derruba as outras
            log.exception("scan falhou")
    return apply_hide(list(found.values()), cfg_games.get("hide") or [])


class Games:
    def __init__(self, cfg: dict, data_dir: Path = DATA_DIR) -> None:
        self.cfg_games: dict = cfg.get("games") or {}
        self.data_dir = data_dir
        self.games: dict[str, dict] = {}
        self.recent: dict[str, float] = {}
        self.scanning = False
        self.scanned_at = 0.0
        self._running: list[str] = []
        self._running_at = 0.0
        self._lock = threading.Lock()
        self._load()

    # ---- persistência
    def _load(self) -> None:
        try:
            data = json.loads((self.data_dir / "games.json").read_text(encoding="utf-8"))
            self.games = {g["id"]: g for g in data.get("games", [])}
            self.scanned_at = float(data.get("scanned_at", 0))
        except (OSError, ValueError, KeyError):
            pass
        try:
            self.recent = json.loads((self.data_dir / "games_state.json").read_text(encoding="utf-8")).get("recent", {})
        except (OSError, ValueError):
            pass

    def _save(self, name: str, payload: dict) -> None:
        try:
            self.data_dir.mkdir(exist_ok=True)
            (self.data_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.exception("não consegui gravar %s", name)

    # ---- scan
    def scan(self) -> int:
        if self.scanning:
            return len(self.games)
        self.scanning = True
        try:
            found = scan_all(self.cfg_games)
            with self._lock:
                self.games = {g["id"]: g for g in found}
                self.scanned_at = time.time()
            self._save("games.json", {"scanned_at": self.scanned_at, "games": found})
            log.info("scan de jogos: %d encontrados", len(found))
            return len(found)
        finally:
            self.scanning = False

    async def startup(self) -> None:
        asyncio.get_running_loop().run_in_executor(None, self.scan)

    async def rescan(self, _v: Any = None) -> int:
        return await asyncio.get_running_loop().run_in_executor(None, self.scan)

    # ---- API
    def public_list(self, _v: Any = None) -> list[dict]:
        with self._lock:
            games = list(self.games.values())
        out = [{"id": g["id"], "name": g["name"], "store": g["store"],
                "art": "cover" if g.get("art") else ("icon" if g.get("icon_src") else ""),
                "recent": self.recent.get(g["id"], 0)} for g in games]
        out.sort(key=lambda g: (-g["recent"], g["name"].lower()))
        return out

    def _get(self, v: Any) -> dict:
        gid = v if isinstance(v, str) else (v.get("id") if isinstance(v, dict) else None)
        g = self.games.get(gid) if isinstance(gid, str) else None
        if g is None:
            raise CommandError("jogo não encontrado (rescaneie a lista)")
        return g

    def run(self, v: Any) -> str:
        g = self._get(v)
        if g["id"] in self.running_ids(force=True):
            raise CommandError(f"{g['name']} já está rodando")
        spec = g["launch"]
        target = spec["target"]
        if spec["kind"] == "uri":
            os.startfile(target)  # noqa: S606 - steam://, com.epicgames.launcher://, shell:AppsFolder\...
        elif spec.get("args") or spec.get("cwd"):
            subprocess.Popen([target, *spec.get("args", [])], cwd=spec.get("cwd") or None)  # noqa: S603
        else:
            os.startfile(target)  # noqa: S606
        self.recent[g["id"]] = time.time()
        self._save("games_state.json", {"recent": self.recent})
        self._running_at = 0.0   # próximo estado reavalia
        return g["name"]

    def _matching_procs(self, directory: str) -> list[Any]:
        import psutil
        procs = []
        for p in psutil.process_iter(["exe"]):
            if exe_under(p.info.get("exe"), directory):
                procs.append(p)
        return procs

    def running_ids(self, force: bool = False) -> list[str]:
        now = time.monotonic()
        if not force and now - self._running_at < RUNNING_TTL:
            return self._running
        import psutil
        exes = []
        for p in psutil.process_iter(["exe"]):
            e = p.info.get("exe")
            if e:
                exes.append(_norm(e))
        with self._lock:
            games = list(self.games.values())
        self._running = [g["id"] for g in games
                         if g.get("dir") and any(e.startswith(_norm(g["dir"]) + os.sep) for e in exes)]
        self._running_at = now
        return self._running

    def close(self, v: Any) -> int:
        g = self._get(v)
        if not g.get("dir") or not safe_dir(g["dir"]):
            raise CommandError("não sei qual processo é este jogo")
        procs = self._matching_procs(g["dir"])
        if not procs:
            raise CommandError(f"{g['name']} não está rodando")
        import psutil
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=3)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        self._running_at = 0.0
        return len(procs)

    async def state(self) -> dict:
        loop = asyncio.get_running_loop()
        running = await loop.run_in_executor(None, self.running_ids)
        return {"running": running, "scanning": self.scanning, "count": len(self.games)}

    def art(self, gid: str) -> tuple[bytes, str] | None:
        g = self.games.get(gid)
        if g is None:
            return None
        path = g.get("art")
        if path and Path(path).is_file():
            try:
                p = Path(path)
                if p.stat().st_size <= MAX_ART_BYTES:
                    return p.read_bytes(), "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            except OSError:
                pass
        if g.get("icon_src"):
            dst = self.data_dir / "icons" / f"{_slug(gid)}.png"
            if extract_icon(g["icon_src"], dst):
                return dst.read_bytes(), "image/png"
        return None


def register(d: Dispatcher, cfg: dict) -> Games:
    games = Games(cfg)
    d.on("games.list", games.public_list)
    d.on("games.run", games.run)
    d.on("games.close", games.close)
    d.on("games.rescan", games.rescan)
    return games
