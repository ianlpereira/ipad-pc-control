"""Servidor principal: FastAPI + WebSocket + arquivos estáticos do painel.

Rodar (na raiz do projeto):  python -m server.main
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable

import psutil
import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .config import WEB_DIR, load_config
from .modules import audio as audio_mod
from .modules import display, launcher as launcher_mod, media as media_mod, power, windows
from .modules import claude as claude_mod
from .modules import discord_rpc as discord_mod
from .modules import lol as lol_mod
from .modules import notify as notify_mod
from .modules.notify import notifier
from .modules.telemetry import Telemetry, ensure_lhm_running
from .protocol import Dispatcher

LOG_DIR = WEB_DIR.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
_file = RotatingFileHandler(LOG_DIR / "server.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_file.setFormatter(_fmt)
logging.getLogger().addHandler(_file)
logging.getLogger("httpx").setLevel(logging.WARNING)  # sem log a cada leitura do LHM
log = logging.getLogger("server")

# Requisições HTTP e diagnósticos do iPad vão só para arquivo (não poluem o console).
http_log = logging.getLogger("http")
http_log.propagate = False
http_log.addHandler(_file)
client_log = logging.getLogger("client")
client_log.propagate = False
_client_file = RotatingFileHandler(LOG_DIR / "client-debug.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
_client_file.setFormatter(_fmt)
client_log.addHandler(_client_file)

cfg = load_config()
dispatcher = Dispatcher()
audio_mod.register(dispatcher, cfg)
media_mod.register(dispatcher, cfg)
windows.register(dispatcher, cfg)
power.register(dispatcher, cfg)
display.register(dispatcher, cfg)
launcher = launcher_mod.register(dispatcher, cfg)
notify_mod.register(dispatcher, cfg)

# Partes extras do estado: nome -> (coroutine function sem args, timeout).
# Módulos novos (LoL, Claude, Discord) se registram aqui.
STATE_PROVIDERS: dict[str, tuple[Callable[[], Awaitable[Any]], float]] = {}
# Coroutines chamadas no startup (depois do event loop existir).
STARTUP_HOOKS: list[Callable[[], Awaitable[None]]] = []

lol = lol_mod.register(dispatcher, cfg)
STATE_PROVIDERS["lol"] = (lol.state, 2.5)
claude = claude_mod.register(dispatcher, cfg)
STATE_PROVIDERS["claude"] = (claude.state, 1.0)
discord = discord_mod.register(dispatcher, cfg)
STATE_PROVIDERS["discord"] = (discord.state, 1.0)


async def _start_discord() -> None:
    asyncio.create_task(discord.run())


STARTUP_HOOKS.append(_start_discord)


async def _ping(v: Any) -> Any:
    return v  # eco: o iPad mede o RTT com o timestamp que ele mesmo mandou


dispatcher.on("ping", _ping)


class Hub:
    """Conjunto de clientes conectados + montagem/broadcast do estado."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.telemetry: Telemetry | None = None
        self._state_lock = asyncio.Lock()
        self._last: dict[str, Any] = {}

    async def _guard(self, name: str, aw: Any, timeout: float) -> Any:
        """Cada parte do estado tem timeout próprio: uma API do Windows travada
        não pode congelar o painel inteiro. Em falha, reaproveita o último valor."""
        try:
            value = await asyncio.wait_for(aw, timeout)
            self._last[name] = value
            return value
        except asyncio.TimeoutError:
            log.warning("%s demorou mais de %.1fs; usando último valor", name, timeout)
        except Exception:  # noqa: BLE001
            log.exception("%s falhou", name)
        return self._last.get(name)

    async def build_state(self, with_sys: bool = True) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        audio_f = self._guard("audio", loop.run_in_executor(audio_mod.COM_EXECUTOR, audio_mod.audio.state), 2.0)
        media_f = self._guard("media", media_mod.media.state(), 2.5)
        names = list(STATE_PROVIDERS)
        extra_f = [self._guard(n, STATE_PROVIDERS[n][0](), STATE_PROVIDERS[n][1]) for n in names]
        if with_sys and self.telemetry is not None:
            sys_f = self._guard("sys", self.telemetry.sample(), 2.0)
            sys_data, audio_state, media_state, *extra = await asyncio.gather(sys_f, audio_f, media_f, *extra_f)
        else:
            audio_state, media_state, *extra = await asyncio.gather(audio_f, media_f, *extra_f)
            sys_data = self._last.get("sys")
        state = {"t": "state", "sys": sys_data or {}, "audio": audio_state, "media": media_state, "ts": time.time()}
        state.update(zip(names, extra))
        return state

    async def send(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        try:
            await ws.send_text(json.dumps(msg, separators=(",", ":"), ensure_ascii=False))
        except Exception:  # noqa: BLE001 - cliente caiu; o loop de recepção limpa
            self.clients.discard(ws)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        if self.clients:
            await asyncio.gather(*(self.send(ws, msg) for ws in list(self.clients)))

    async def push_state(self) -> None:
        """Push imediato após um comando (sem re-amostrar CPU para não distorcer a média)."""
        if not self.clients:
            return
        try:
            async with self._state_lock:
                state = await self.build_state(with_sys=False)
            await self.broadcast(state)
        except Exception:  # noqa: BLE001
            log.exception("push_state falhou")

    async def tick_loop(self, interval: float) -> None:
        while True:
            started = time.monotonic()
            if self.clients:
                try:
                    async with self._state_lock:
                        state = await self.build_state()
                    await self.broadcast(state)
                except Exception:  # noqa: BLE001
                    log.exception("tick falhou")
            await asyncio.sleep(max(0.05, interval - (time.monotonic() - started)))


hub = Hub()
dispatcher.on_state_change = hub.push_state


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hub.telemetry = Telemetry(cfg)
    notifier.bind(hub.broadcast, asyncio.get_running_loop())
    for start in STARTUP_HOOKS:
        await start()
    # Abre o LibreHardwareMonitor em paralelo (o prompt do UAC não segura o servidor).
    asyncio.get_running_loop().run_in_executor(
        None, ensure_lhm_running, cfg.get("lhm_path", ""), bool(cfg.get("lhm_minimized", True)))
    task = asyncio.create_task(hub.tick_loop(float(cfg["tick_seconds"])))
    _print_banner()
    yield
    task.cancel()
    await lol.shutdown()  # se o servidor fechar no meio da partida, devolve os volumes
    await hub.telemetry.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    # O Safari do iPad cacheia agressivamente WebClips; sem isso, updates do painel não aparecem.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    if request.url.path != "/debug":
        http_log.info("%s %s %s -> %s | %s", request.client.host if request.client else "?", request.method,
                      request.url.path, resp.status_code, request.headers.get("user-agent", "")[:120])
    return resp


@app.post("/debug")
async def client_debug(request: Request) -> Response:
    """Recebe erros de JS e medidas de layout do iPad (grava em logs/client-debug.log)."""
    body = (await request.body())[:20_000].decode("utf-8", "replace")
    client_log.info("%s %s", request.client.host if request.client else "?", body)
    return Response(status_code=204)


def _token_ok(token: str | None) -> bool:
    if not cfg.get("require_token"):
        return True  # modo LAN confiável: qualquer dispositivo da rede pode usar o painel
    return bool(token) and secrets.compare_digest(token, cfg["token"])


@app.get("/media/art")
async def media_art(token: str = Query(""), k: str = Query("")) -> Response:
    art = media_mod.media.art
    if not _token_ok(token) or art is None:
        return Response(status_code=404)
    return Response(content=art[0], media_type=art[1], headers={"Cache-Control": "max-age=3600"})


@app.get("/lol/asset")
async def lol_asset(p: str = Query("")) -> Response:
    """Proxy de ícones do cliente do LoL (só caminhos /lol-game-data/assets/...)."""
    item = await lol.builds.asset(p)
    if item is None:
        return Response(status_code=404)
    return Response(content=item[0], media_type=item[1], headers={"Cache-Control": "max-age=86400"})


@app.get("/lol/champion-icon/{champ_id}")
async def lol_champion_icon(champ_id: int) -> Response:
    icon = await lol.champion_icon(champ_id)
    if icon is None:
        return Response(status_code=404)
    return Response(content=icon[0], media_type=icon[1], headers={"Cache-Control": "max-age=86400"})


def _is_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1")


@app.post("/claude/event")
async def claude_event(request: Request) -> Response:
    """Eventos dos hooks do Claude Code (só aceitos do próprio PC)."""
    if not _is_local(request):
        return Response(status_code=403)
    try:
        claude.on_event(json.loads(await request.body()))
    except ValueError:
        return Response(status_code=400)
    asyncio.create_task(hub.push_state())
    return Response(status_code=204)


@app.post("/claude/permission")
async def claude_permission(request: Request) -> Response:
    """Hook PermissionRequest: segura a resposta até o iPad decidir (ou timeout)."""
    if not _is_local(request):
        return Response(status_code=403)
    try:
        data = json.loads(await request.body())
    except ValueError:
        return Response(status_code=400)
    task = asyncio.create_task(claude.on_permission(data))
    await asyncio.sleep(0)
    asyncio.create_task(hub.push_state())
    result = await task
    asyncio.create_task(hub.push_state())
    return Response(content=json.dumps(result), media_type="application/json")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query("")) -> None:
    await ws.accept()
    if not _token_ok(token):
        await ws.close(code=4401, reason="token inválido")
        return
    peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
    log.info("cliente conectado: %s", peer)
    hub.clients.add(ws)
    await hub.send(ws, {
        "t": "config",
        "host": socket.gethostname(),
        "launcher": launcher.public_pages(cfg),
        "night_dim": cfg.get("night_dim"),
    })
    await hub.send(ws, notifier.snapshot())
    asyncio.create_task(hub.push_state())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("t") == "cmd":
                # Cada comando em sua própria task: um script lento não segura os sliders.
                asyncio.create_task(_run_cmd(ws, msg))
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
        log.info("cliente desconectado: %s", peer)


async def _run_cmd(ws: WebSocket, msg: dict[str, Any]) -> None:
    if msg.get("a") != "ping":
        log.info("cmd %s %s", msg.get("a"), json.dumps(msg.get("v"), ensure_ascii=False)[:80])
    peer = ws.client.host if ws.client else None
    await hub.send(ws, await dispatcher.handle(msg, peer))


# Painel web (index.html, app.js, style.css...). Montado por último para não sombrear /ws.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def _lan_ips() -> list[str]:
    ips = []
    for name, addrs in psutil.net_if_addrs().items():
        if any(x in name.lower() for x in ("loopback", "vethernet", "virtualbox", "vmware")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                ips.append(a.address)
    return ips


def _print_banner() -> None:
    port = cfg["port"]
    print("\n" + "=" * 64)
    print("  Control Center rodando. Abra no Safari do iPad:")
    query = f"?token={cfg['token']}" if cfg.get("require_token") else ""
    for ip in _lan_ips() or ["<ip-do-pc>"]:
        print(f"    http://{ip}:{port}/{query}")
    if not cfg.get("require_token"):
        print("  Token desativado: qualquer dispositivo desta rede pode controlar o PC.")
    print("  Depois: Compartilhar > Adicionar à Tela de Início.")
    print("=" * 64 + "\n", flush=True)


def _minimize_console() -> None:
    """Minimiza a janela do console (atalho de inicialização passa --min).
    Garantia extra: o "Executar: Minimizado" do atalho nem sempre é respeitado."""
    import ctypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 7)  # SW_SHOWMINNOACTIVE: minimiza sem roubar o foco


def main() -> None:
    import sys
    if "--min" in sys.argv[1:]:
        _minimize_console()
    uvicorn.run(
        app,
        host=cfg["host"],
        port=int(cfg["port"]),
        log_level="warning",
        ws_ping_interval=15,
        ws_ping_timeout=10,
        ws_per_message_deflate=False,  # compressão custa CPU no A8X e não ajuda em payloads de 2 KB
    )


if __name__ == "__main__":
    main()
