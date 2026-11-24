"""Captura as telas do painel (servidor demo) para o vídeo promocional.

Uso (com promo/demo_server.py rodando na porta 8790):
    python promo/capture.py

Gera promo/data/<cena>.png (1024x768 @2x) e promo/data/scenes.json com o
retângulo (em px CSS do painel) de cada botão visível, para posicionar os toques.
"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

DATA = Path(__file__).resolve().parent / "data"
URL = "http://127.0.0.1:8790/"

def mixer_apps(spotify, chrome_muted):
    return [{"key": "spotify.exe", "name": "Spotify", "volume": spotify, "muted": False},
            {"key": "discord.exe", "name": "Discord", "volume": 0.9, "muted": False},
            {"key": "chrome.exe", "name": "Google Chrome", "volume": 0.4, "muted": chrome_muted},
            {"key": "leagueclient.exe", "name": "League of Legends", "volume": 0.7, "muted": False},
            {"key": "steam.exe", "name": "Steam", "volume": 0.3, "muted": True}]


# nome: (aba, overrides do estado, JS extra)
SCENES = {
    "media": ("media", {}, ""),
    "media-tv": ("media", {"audio": {"device": "tv"}}, ""),
    "mixer": ("mixer", {}, ""),
    "mixer-b": ("mixer", {"audio": {"apps": mixer_apps(0.88, False)}}, ""),
    "mixer-c": ("mixer", {"audio": {"apps": mixer_apps(0.88, True)}}, ""),
    "fla": ("fla", {}, ""),
    "discord": ("discord", {}, ""),
    "discord-b": ("discord", {"discord": {"channel": {"id": "1", "name": "Sala do grupo", "guild": "Amigos", "members": [
        {"id": str(i), "name": n, "avatar": "", "mute": m, "deaf": False, "volume": 100, "is_self": i == 1, "speaking": sp}
        for i, (n, m, sp) in enumerate([("Você", False, True), ("Lucas", False, False), ("Bia", True, False),
                                        ("Rafa", False, True)], 1)]}}}, ""),
    "claude": ("claude", {}, ""),
    "claude-b": ("claude", {"claude": {"permissions": [], "sessions": [
        {"id": "a", "project": "meu-site", "status": "working", "message": "Rodando npm run build", "ts": time.time() - 3},
        {"id": "b", "project": "api-financas", "status": "working", "message": "Rodando os testes", "ts": time.time() - 200},
        {"id": "c", "project": "ipad-pc-control", "status": "done", "message": "Pronto: README atualizado",
         "ts": time.time() - 900}]}}, ""),
    "games": ("games", {}, ""),
    "launcher": ("launcher", {}, ""),
    "system": ("system", {}, ""),
}

DUMP = """() => {
  const out = [];
  for (const el of document.querySelectorAll('button, input, [role=button], .tab, .chip, .btn, .ch, .member, .dc-m')) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > innerHeight) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    out.push({id: el.id, cls: el.className && el.className.baseVal === undefined ? el.className : '',
              text: (el.innerText || el.value || el.title || '').trim().slice(0, 40),
              x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)});
  }
  return out;
}"""


def post(overrides):
    req = urllib.request.Request(URL + "demo", data=json.dumps(overrides).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    meta = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1024, "height": 768}, device_scale_factor=2)
        page.goto(URL)
        page.wait_for_timeout(1500)
        for name, (tab, overrides, js) in SCENES.items():
            post(overrides)
            page.evaluate(f"localStorage.setItem('cc_tab', '{tab}')")
            page.reload()
            page.wait_for_timeout(3000)
            if js:
                page.evaluate(js)
                page.wait_for_timeout(1200)
            page.screenshot(path=str(DATA / f"{name}.png"))
            meta[name] = page.evaluate(DUMP)
            print("ok", name, len(meta[name]))
        post({})
        browser.close()
    (DATA / "scenes.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
