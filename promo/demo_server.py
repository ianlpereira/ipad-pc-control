"""Servidor de demonstração: serve web/ com estado FALSO (screenshots do site e do vídeo).

Uso: python promo/demo_server.py promo/data/real_state.json
POST /demo com JSON sobrescreve seções do estado (merge raso por seção).
"""
import asyncio, io, json, math, random, sys, time
from pathlib import Path
import httpx, uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES = {}
REAL = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
EXAMPLE = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
app = FastAPI()

def art_png():
    img = Image.new("RGB", (300, 300))
    d = ImageDraw.Draw(img)
    for y in range(300):
        t = y / 300
        d.line([(0, y), (300, y)], fill=(int(80 + 150 * t), int(40 + 30 * t), int(200 - 80 * t)))
    d.ellipse((60, 150, 240, 330), fill=(255, 140, 90))
    for i in range(6):
        d.line([(0, 200 + i * 18), (300, 200 + i * 18)], fill=(40, 20, 90), width=4)
    b = io.BytesIO(); img.filter(ImageFilter.GaussianBlur(0.6)).save(b, "PNG"); return b.getvalue()
ART = art_png()

CONFIG = {"t": "config", "host": "MEU-PC", "launcher": [
    {"name": p["name"], "buttons": [{k: b[k] for k in ("id", "label", "icon", "color", "confirm") if k in b} for b in p["buttons"]]}
    for p in EXAMPLE["launcher"]], "night_dim": EXAMPLE["night_dim"],
    "features": {"lol": True, "football": True, "discord": True, "claude": True, "games": True}, "football_label": "Flamengo"}

GAMES = [("steam-1", "Elden Ring", "steam"), ("steam-2", "Hades II", "steam"), ("steam-3", "Cyberpunk 2077", "steam"),
         ("steam-4", "Baldur's Gate 3", "steam"), ("steam-5", "Hollow Knight", "steam"), ("steam-6", "God of War Ragnarök", "steam"),
         ("epic-1", "Fortnite", "epic"), ("epic-2", "Alan Wake 2", "epic"), ("epic-3", "Rocket League", "epic"),
         ("gog-1", "The Witcher 3", "gog"), ("gog-2", "Disco Elysium", "gog"), ("xbox-1", "Forza Horizon 5", "xbox"),
         ("xbox-2", "Halo Infinite", "xbox"), ("extra-1", "Valorant", "extra"), ("extra-2", "Overwatch 2", "extra"),
         ("extra-3", "Apex Legends", "extra")]
GAMES_PUBLIC = [{"id": i, "name": n, "store": st, "art": True, "recent": 0} for i, n, st in GAMES]

def game_art(name):
    h = sum(map(ord, name)) * 37 % 360
    img = Image.new("RGB", (300, 450)); d = ImageDraw.Draw(img)
    import colorsys
    for y in range(450):
        r, g, b = colorsys.hsv_to_rgb(h / 360, 0.65, 0.25 + 0.55 * (1 - y / 450))
        d.line([(0, y), (300, y)], fill=(int(r * 255), int(g * 255), int(b * 255)))
    r, g, b = colorsys.hsv_to_rgb(((h + 40) % 360) / 360, 0.5, 0.95)
    d.ellipse((70, 70, 230, 230), fill=(int(r * 255), int(g * 255), int(b * 255)))
    bio = io.BytesIO(); img.filter(ImageFilter.GaussianBlur(1.2)).save(bio, "JPEG", quality=85); return bio.getvalue()

T0 = time.time()
def state():
    t = time.time() - T0
    s = REAL["state"]
    sys_ = dict(s["sys"]); sys_.update(cpu=23 + 6 * math.sin(t / 3) + random.random() * 3, gpu=41 + 8 * math.sin(t / 5),
        ram=52.0, ram_used=16.6, ram_total=31.9, cpu_temp=58.0, gpu_temp=54.0, gpu_hotspot=67.0,
        net_down=int(2_400_000 + 900_000 * math.sin(t)), net_up=int(310_000 + 90_000 * math.cos(t)),
        window="Visual Studio Code", gpu_name="Radeon RX 7800 XT", vram_used=5200.0, vram_total=16368.0)
    audio = {"master": 0.62, "muted": False, "device": "hs", "devices": [
        {"id": "hs", "name": "Fone de ouvido (Headset USB)"}, {"id": "sp", "name": "Alto-falantes (Realtek Audio)"},
        {"id": "tv", "name": "Monitor LG (HDMI)"}], "apps": [
        {"key": "spotify.exe", "name": "Spotify", "volume": 0.55, "muted": False},
        {"key": "discord.exe", "name": "Discord", "volume": 0.9, "muted": False},
        {"key": "chrome.exe", "name": "Google Chrome", "volume": 0.4, "muted": False},
        {"key": "leagueclient.exe", "name": "League of Legends", "volume": 0.7, "muted": False},
        {"key": "steam.exe", "name": "Steam", "volume": 0.3, "muted": True}]}
    media = {"title": "Neon Avenue", "artist": "Synthwave Demo · Noites de 1986", "app": "Spotify", "status": "playing",
             "position": 74 + (t % 120), "duration": 213, "art": "demo1"}
    discord = {"configured": True, "connected": True, "authorized": True, "error": "", "self": {"mute": False, "deaf": False},
        "channel": {"id": "1", "name": "Sala do grupo", "guild": "Amigos", "members": [
            {"id": str(i), "name": n, "avatar": "", "mute": m, "deaf": False, "volume": 100, "is_self": i == 1, "speaking": sp}
            for i, (n, m, sp) in enumerate([("Você", False, False), ("Lucas", False, True), ("Bia", True, False), ("Rafa", False, False)], 1)]}}
    now = time.time()
    claude = {"hooks": True, "remote": True, "permissions": [
        {"id": "p1", "project": "meu-site", "tool": "Bash", "summary": "npm run build"}],
        "sessions": [
            {"id": "a", "project": "meu-site", "status": "waiting", "message": "Pedido de permissão: Bash", "ts": now - 40},
            {"id": "b", "project": "api-financas", "status": "working", "message": "Rodando os testes", "ts": now - 200},
            {"id": "c", "project": "ipad-pc-control", "status": "done", "message": "Pronto: README atualizado", "ts": now - 900}],
        "usage": {"today": 1_840_000, "week": 9_300_000, "top": [{"project": "meu-site", "tokens": 4_100_000}, {"project": "api-financas", "tokens": 2_900_000}]},
        "launch": s["claude"].get("launch", [])}
    lol = s["lol"]
    games = {"running": ["steam-1"], "scanning": False, "count": len(GAMES)}
    out = {"t": "state", "games": games, "sys": sys_, "audio": audio, "media": media, "ts": now, "lol": lol,
           "claude": claude, "discord": discord, "fla": s["fla"]}
    for k, v in OVERRIDES.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

@app.get("/games/art/{gid}")
async def games_art(gid: str):
    for i, n, _ in GAMES:
        if i == gid: return Response(game_art(n), media_type="image/jpeg")
    return Response(status_code=404)

@app.get("/media/art")
async def art(): return Response(ART, media_type="image/png")

PROXY = httpx.AsyncClient(base_url="http://127.0.0.1:8766", timeout=5)
@app.get("/lol/{path:path}")
async def lol(path: str, request: Request):
    try:
        r = await PROXY.get("/lol/" + path, params=dict(request.query_params))
    except httpx.HTTPError:  # servidor real desligado: sem ícones do LoL
        return Response(status_code=404)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))

@app.post("/demo")
async def demo(request: Request):
    OVERRIDES.clear(); OVERRIDES.update(await request.json())
    return Response(status_code=204)

@app.post("/debug")
async def dbg(): return Response(status_code=204)

@app.websocket("/ws")
async def ws(w: WebSocket):
    await w.accept()
    await w.send_text(json.dumps(CONFIG, ensure_ascii=False))
    await w.send_text(json.dumps({"t": "notify_list", "items": []}))
    async def reader():
        while True:
            m = json.loads(await w.receive_text())
            if m.get("t") == "cmd":
                r = m.get("v") if m.get("a") == "ping" else GAMES_PUBLIC if m.get("a") == "games.list" else None
                await w.send_text(json.dumps({"t": "ack", "id": m.get("id"), "ok": True, "r": r}))
    task = asyncio.create_task(reader())
    try:
        while True:
            await w.send_text(json.dumps(state(), ensure_ascii=False)); await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        task.cancel()

app.mount("/", StaticFiles(directory=ROOT / "web", html=True))
uvicorn.run(app, host="127.0.0.1", port=8790, log_level="warning")
