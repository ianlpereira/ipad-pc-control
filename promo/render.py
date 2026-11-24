"""Renderiza promo/reel.html em MP4 1080x1920 (Reels).

Uso:
    python promo/render.py                  # promo/out/reel.mp4 + capa.jpg
    python promo/render.py --stills 3 9.5   # só quadros soltos em promo/out/stills/ (com zonas seguras)

Requer: pip install playwright imageio-ffmpeg (navegador: Microsoft Edge instalado).
"""
import argparse
import subprocess
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FPS = 30
COVER_T = 2.9  # quadro da capa: título + iPad


def open_page(p, debug=False):
    browser = p.chromium.launch(channel="msedge", args=["--allow-file-access-from-files"])
    page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
    page.goto((HERE / "reel.html").as_uri() + ("?debug" if debug else ""))
    page.evaluate("window.ready")
    return browser, page


def frame(page, t, path=None, kind="jpeg"):
    page.evaluate(f"seek({t})")
    page.evaluate("new Promise(r => requestAnimationFrame(() => r()))")
    opts = {"type": kind, "path": path}
    if kind == "jpeg":
        opts["quality"] = 95
    return page.screenshot(**{k: v for k, v in opts.items() if v is not None})


def stills(times):
    (OUT / "stills").mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser, page = open_page(p, debug=True)
        for t in times:
            frame(page, t, str(OUT / "stills" / f"t{t:05.1f}.png"), "png")
            print("ok", t)
        browser.close()


def video():
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / "reel.mp4"
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
           "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "17",
           # JPEG vem em faixa cheia (yuvj420p); o Instagram espera faixa de TV (yuv420p)
           "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-color_range", "tv",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-r", str(FPS), "-movflags", "+faststart", str(dst)]
    with sync_playwright() as p:
        browser, page = open_page(p)
        duration = page.evaluate("window.DURATION")
        total = int(duration * FPS)
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        t0 = time.time()
        for i in range(total):
            ff.stdin.write(frame(page, i / FPS))
            if i % 150 == 0:
                print(f"{i}/{total} quadros ({time.time() - t0:.0f}s)", flush=True)
        ff.stdin.close()
        ff.wait()
        frame(page, COVER_T, str(OUT / "capa.jpg"))
        browser.close()
    print("pronto:", dst)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stills", nargs="*", type=float)
    a = ap.parse_args()
    stills(a.stills) if a.stills else video()
