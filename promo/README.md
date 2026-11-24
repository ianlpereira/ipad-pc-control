# Vídeo promocional (Reels)

Gera um vídeo 1080x1920 (9:16, 30 fps, H.264) mostrando as abas do painel com dados fictícios.

## Como regenerar

Na raiz do projeto, com o `.venv` criado pelo `start.bat`:

```powershell
# 1. servidor de demonstração (porta 8790, estado falso). Precisa de promo/data/real_state.json
.\.venv\Scripts\python promo\demo_server.py promo\data\real_state.json

# 2. em outro terminal: captura as telas e as posições dos botões
python -m pip install playwright imageio-ffmpeg
python promo\capture.py

# 3. renderiza (cerca de 3 a 5 minutos)
python promo\render.py
```

Saída em `promo/out/`: `reel.mp4`, `capa.jpg` e `legenda.txt`. `python promo\render.py --stills 3 10.5` gera quadros soltos com as zonas que a interface do Instagram cobre marcadas em vermelho.

## Arquivos

- `demo_server.py`: serve `web/` com estado falso. `POST /demo` sobrescreve partes do estado.
- `capture.py`: tira as screenshots (Edge headless, 1024x768 @2x) e salva os retângulos dos botões em `data/scenes.json`.
- `reel.html` + `reel.js`: composição. Tudo é função do tempo (`window.seek(t)`): imagens, câmera, legendas, toques. Edite as trilhas `IMG`, `CAM`, `CAP` e `TAPS` em `reel.js` para mudar o roteiro.
- `render.py`: abre a composição no Edge via Playwright, tira um quadro por vez e envia ao ffmpeg.

As telas do LoL (`data/lol-*.png`) foram capturadas com o servidor real em partida; o demo não as recria sem ele.
`data/` e `out/` ficam fora do git.
