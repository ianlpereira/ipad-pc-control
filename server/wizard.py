"""Assistente de primeira configuração do Control Center.

    .venv\\Scripts\\python.exe -m server.wizard          (ou: start.bat --setup)

Roda sozinho no primeiro `start.bat` (quando ainda não existe config.json).
Detecta o que dá para detectar (IP, GPU, LibreHardwareMonitor, League of
Legends, Claude Code), pergunta o resto e grava o config.json. Rodar de novo
reaproveita o config.json atual como resposta padrão (com backup).

Também oferece, sempre perguntando antes:
- criar a regra do Firewall do Windows (pede admin pelo UAC);
- iniciar o servidor junto com o Windows (scripts/autostart.ps1);
- instalar os hooks do Claude Code (scripts/install_claude_hooks.py).
"""
from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from .config import CONFIG_PATH, DEFAULTS, ROOT, read_example, write_config

FIREWALL_RULE = "Control Center iPad"
LHM_RELEASES = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases"
DISCORD_APPS = "https://discord.com/developers/applications"
CREATE_NO_WINDOW = 0x08000000


class Abort(Exception):
    pass


# ----------------------------------------------------------------------------- entrada/saída
def title(text: str) -> None:
    print("\n" + "-" * 64)
    print(f"  {text}")
    print("-" * 64)


def info(text: str = "") -> None:
    print(f"  {text}" if text else "")


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        value = input(f"  > {prompt}{hint}: ").strip()
    except EOFError as e:
        raise Abort from e
    return value or default


def ask_yes(prompt: str, default: bool) -> bool:
    hint = "S/n" if default else "s/N"
    while True:
        try:
            value = input(f"  > {prompt} ({hint}): ").strip().lower()
        except EOFError as e:
            raise Abort from e
        if not value:
            return default
        if value in ("s", "sim", "y", "yes"):
            return True
        if value in ("n", "nao", "não", "no"):
            return False
        info("Responda s ou n.")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        value = ask(prompt, str(default))
        if value.isdigit() and lo <= int(value) <= hi:
            return int(value)
        info(f"Digite um número entre {lo} e {hi}.")


def ask_list(prompt: str, default: list[str]) -> list[str]:
    value = ask(prompt + " (separe por vírgula; '-' para nenhum)", ", ".join(default))
    if value.strip() == "-":
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def choose(prompt: str, options: list[str], default: int = 0) -> int:
    for i, opt in enumerate(options, 1):
        info(f"  {i}) {opt}")
    return ask_int(prompt, default + 1, 1, len(options)) - 1


# ----------------------------------------------------------------------------- detecção
def powershell(command: str, timeout: float = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return r.returncode, (r.stdout or "").strip()


def lan_ips() -> list[str]:
    try:
        import psutil
    except ImportError:
        return []
    ips = []
    for name, addrs in psutil.net_if_addrs().items():
        if any(x in name.lower() for x in ("loopback", "vethernet", "virtualbox", "vmware", "bluetooth")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                ips.append(a.address)
    return ips


def detect_gpus() -> list[str]:
    _rc, out = powershell("(Get-CimInstance Win32_VideoController).Name")
    return [line.strip() for line in out.splitlines() if line.strip()]


def gpu_token(name: str) -> str:
    """Trecho curto e distintivo do nome (ex.: "RX 9060 XT" -> "9060") para casar com o nome no LHM."""
    for part in reversed(name.replace("(TM)", " ").split()):
        if any(ch.isdigit() for ch in part):
            return part.lower()
    return name.lower()


def find_lhm() -> str:
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop", home / "Documents", home / "AppData" / "Local",
             Path(os.environ.get("ProgramFiles", r"C:\Program Files")), Path(r"C:\Tools"), Path("C:/")]
    for root in roots:
        if not root.is_dir():
            continue
        # Até 2 níveis abaixo (ex.: Downloads/LibreHardwareMonitor-net472/LibreHardwareMonitor.exe).
        for pattern in ("LibreHardwareMonitor.exe", "*/LibreHardwareMonitor.exe", "*/*/LibreHardwareMonitor.exe"):
            if root == Path("C:/") and pattern.count("/") > 1:
                continue
            try:
                for exe in root.glob(pattern):
                    return str(exe.parent)
            except OSError:
                continue
    winget = home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget.is_dir():
        for exe in winget.glob("LibreHardwareMonitor*/**/LibreHardwareMonitor.exe"):
            return str(exe.parent)
    return ""


def detect_lol() -> tuple[str, str]:
    """(pasta do League of Legends, RiotClientServices.exe) a partir do arquivo de instalação da Riot."""
    installs = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Riot Games" / "RiotClientInstalls.json"
    game_dir, riot_client = "", ""
    try:
        data = json.loads(installs.read_text(encoding="utf-8"))
        riot_client = data.get("rc_default") or data.get("rc_live") or ""
        for path in (data.get("associated_client") or {}):
            if "league of legends" in path.lower():
                game_dir = path.rstrip("/\\")
                break
    except (OSError, ValueError):
        pass
    if not game_dir:
        for drive in ("C:", "D:", "E:"):
            p = Path(f"{drive}/Riot Games/League of Legends")
            if p.is_dir():
                game_dir = p.as_posix()
                break
    return game_dir, riot_client


def firewall_rule_ports() -> list[str] | None:
    """Portas da regra do firewall do Control Center, ou None se a regra não existe."""
    rc, out = powershell(f"Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction Stop | "
                         "Get-NetFirewallPortFilter | ForEach-Object { $_.LocalPort }")
    if rc != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def run_elevated(command: str) -> bool:
    """Roda um comando PowerShell como administrador (o Windows mostra o UAC)."""
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    rc, _ = powershell("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                       f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'", timeout=180)
    return rc == 0


def public_networks() -> list[str]:
    _rc, out = powershell("Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public' | "
                          "ForEach-Object { $_.Name }")
    return [line.strip() for line in out.splitlines() if line.strip()]


# Times do Brasileirão (nome, id no ESPN). Usados se a consulta ao ESPN falhar.
TEAMS_FALLBACK = {
    "Série A": [("Athletico-PR", "3458"), ("Atlético-MG", "7632"), ("Bahia", "9967"), ("Botafogo", "6086"),
                ("Bragantino", "6079"), ("Chapecoense", "9318"), ("Corinthians", "874"), ("Coritiba", "3456"),
                ("Cruzeiro", "2022"), ("Flamengo", "819"), ("Fluminense", "3445"), ("Grêmio", "6273"),
                ("Internacional", "1936"), ("Mirassol", "9169"), ("Palmeiras", "2029"), ("Remo", "4936"),
                ("Santos", "2674"), ("São Paulo", "2026"), ("Vasco", "3454"), ("Vitória", "3457")],
    "Série B": [("América-MG", "6154"), ("Athletic", "20851"), ("Atlético-GO", "10357"), ("Avaí", "9966"),
                ("Botafogo-SP", "10281"), ("CRB", "9970"), ("Ceará", "9969"), ("Criciúma", "9971"),
                ("Cuiabá", "17313"), ("Fortaleza", "6272"), ("Goiás", "3395"), ("Juventude", "6270"),
                ("Londrina", "17333"), ("Náutico", "7633"), ("Novorizontino", "18127"), ("Operário-PR", "18187"),
                ("Ponte Preta", "3459"), ("São Bernardo", "11268"), ("Sport", "7635"), ("Vila Nova", "9973")],
}
ESPN_LEAGUES = {"Série A": "bra.1", "Série B": "bra.2"}


def fetch_teams() -> dict[str, list[tuple[str, str]]]:
    """Times atuais das Séries A e B direto do ESPN (a lista muda com acesso e rebaixamento)."""
    try:
        import httpx
        out = {}
        for label, slug in ESPN_LEAGUES.items():
            r = httpx.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams", timeout=5)
            r.raise_for_status()
            teams = [t["team"] for t in r.json()["sports"][0]["leagues"][0]["teams"]]
            out[label] = sorted(((t.get("shortDisplayName") or t["displayName"], str(t["id"])) for t in teams
                                 if not str(t.get("displayName", "")).startswith("TBD")), key=lambda x: x[0])
        if all(out.values()):
            return out
    except Exception:  # noqa: BLE001 - sem internet ou API mudou: usa a lista embutida
        pass
    return TEAMS_FALLBACK


def pick_team(current_id: str, current_name: str) -> tuple[str, str]:
    """Lista numerada dos times; devolve (nome, id do ESPN). 0 = outro time digitando o id."""
    groups = fetch_teams()
    flat: list[tuple[str, str]] = []
    default = 0
    for label, teams in groups.items():
        info()
        info(label + ":")
        cells = []
        for name, tid in teams:
            flat.append((name, tid))
            if tid == current_id:
                default = len(flat)
            cells.append(f"{len(flat):>3}) {name:<16}")
        for i in range(0, len(cells), 4):
            info("".join(cells[i:i + 4]))
    info()
    info("  0) Outro time (qualquer time do ESPN, digitando o id)")
    # Time atual fora da lista (ou nenhum): o padrão é 0, que pergunta o id já preenchido.
    n = ask_int("Número do seu time", default, 0, len(flat))
    if n:
        return flat[n - 1]
    info("Abra a página do time em espn.com.br; o id é o número no link:")
    info("  espn.com.br/futebol/time/_/id/819/flamengo -> 819")
    while True:
        tid = ask("Id do time no ESPN", current_id or "")
        if tid.isdigit():
            break
        info("O id é só números.")
    return (current_name if tid == current_id and current_name else "Futebol"), tid


# ----------------------------------------------------------------------------- etapas
def step_network(cfg: dict) -> None:
    title("1/6  Rede")
    ips = lan_ips()
    if ips:
        info("IP deste PC na rede: " + ", ".join(ips))
        info("Dica: reserve esse IP no roteador (reserva DHCP) para a URL do iPad nunca mudar.")
    else:
        info("Não encontrei um IP de rede local. Confira se o PC está conectado ao Wi-Fi/cabo.")
    cfg["port"] = ask_int("Porta do servidor", int(cfg.get("port") or 8765), 1024, 65535)

    info()
    info("Sem token, qualquer aparelho da mesma rede pode controlar o PC (inclusive desligar).")
    info("Numa rede doméstica só sua isso é aceitável. Com convidados no Wi-Fi, ative o token.")
    cfg["require_token"] = ask_yes("Exigir token de acesso?", bool(cfg.get("require_token")))
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(12)


def step_telemetry(cfg: dict) -> None:
    title("2/6  Telemetria (temperaturas e GPU)")
    info("CPU, RAM e rede funcionam sem nada extra. Temperaturas e GPU vêm do")
    info("LibreHardwareMonitor (gratuito), com 'Options > Remote Web Server > Run' ligado.")
    current = cfg.get("lhm_path") or ""
    found = current if current and Path(os.path.expanduser(current)).exists() else find_lhm()
    if found:
        info(f"LibreHardwareMonitor encontrado em: {found}")
        if ask_yes("Abrir o LibreHardwareMonitor automaticamente junto com o servidor?", True):
            cfg["lhm_path"] = found
        else:
            cfg["lhm_path"] = ""
    else:
        info("LibreHardwareMonitor não encontrado.")
        if ask_yes("Abrir a página de download agora?", False):
            webbrowser.open(LHM_RELEASES)
            info("Extraia o .zip numa pasta (ex.: Downloads\\LibreHardwareMonitor).")
        cfg["lhm_path"] = ask("Pasta do LibreHardwareMonitor (vazio = configurar depois)", "")

    gpus = detect_gpus()
    if len(gpus) > 1:
        info("Placas de vídeo encontradas:")
        idx = choose("Qual mostrar no painel?", gpus, 0)
        cfg["gpu_name_filter"] = gpu_token(gpus[idx])
    elif gpus:
        info(f"Placa de vídeo: {gpus[0]}")


def step_features(cfg: dict) -> None:
    title("3/6  Abas opcionais")
    info("Mídia, Mixer, Atalhos, Janelas e Sistema estão sempre ligadas.")
    feats = cfg.setdefault("features", {})

    # --- League of Legends
    info()
    lol = cfg.setdefault("lol", {})
    game_dir, riot_client = detect_lol()
    if game_dir:
        info(f"League of Legends encontrado em: {game_dir}")
    feats["lol"] = ask_yes("Ativar a aba League of Legends?", feats.get("lol", bool(game_dir)))
    if feats["lol"]:
        lol["dir"] = ask("Pasta do League of Legends", game_dir or lol.get("dir") or "C:/Riot Games/League of Legends")
        if riot_client:
            lol["riot_client"] = riot_client

    # --- Futebol
    info()
    fla = cfg.setdefault("flamengo", {})
    info("Placar ao vivo de um time de futebol (dados públicos do ESPN).")
    feats["football"] = ask_yes("Ativar a aba de futebol?", feats.get("football", False))
    if feats["football"]:
        old_id, old_label = str(fla.get("team_id") or "819"), fla.get("label") or ""
        name, team_id = pick_team(old_id, old_label)
        fla["team_id"] = team_id
        fla["label"] = ask("Nome da aba", old_label if team_id == old_id and old_label else name)

    # --- Discord
    info()
    dc = cfg.setdefault("discord", {})
    info("Aba Discord: entrar/sair do canal de voz do grupo, mute e volume de cada amigo.")
    info("Exige criar um app grátis no portal de desenvolvedores do Discord (uns 3 minutos).")
    feats["discord"] = ask_yes("Ativar a aba Discord?", feats.get("discord", bool(dc.get("client_id"))))
    if feats["discord"] and not (dc.get("client_id") and dc.get("client_secret")):
        info("1. New Application (qualquer nome)  2. OAuth2: copie Client ID e Client Secret")
        info("3. Em OAuth2 > Redirects, adicione http://localhost")
        if ask_yes("Abrir o portal do Discord agora?", True):
            webbrowser.open(DISCORD_APPS)
        dc["client_id"] = ask("Client ID (vazio = configurar depois)", dc.get("client_id", ""))
        dc["client_secret"] = ask("Client Secret", dc.get("client_secret", "")) if dc["client_id"] else ""
        dc.setdefault("redirect_uri", "http://localhost")
    if feats["discord"]:
        info("ID do canal de voz: Discord > Configurações > Avançado > Modo de desenvolvedor;")
        info("depois clique com o botão direito no canal > Copiar ID do canal.")
        dc["group_channel_id"] = ask("ID do canal de voz do grupo (vazio = depois)", dc.get("group_channel_id", ""))

    # --- Jogos
    info()
    info("Aba Jogos: reconhece sozinha os jogos da Steam, Epic, GOG e Xbox e abre/fecha pelo iPad.")
    feats["games"] = ask_yes("Ativar a aba Jogos?", feats.get("games", True))

    # --- Claude Code
    info()
    cl = cfg.setdefault("claude", {})
    has_claude = (Path.home() / ".claude").is_dir()
    if has_claude:
        info("Claude Code encontrado neste PC.")
    feats["claude"] = ask_yes("Ativar a aba Claude Code?", feats.get("claude", has_claude))
    if feats["claude"]:
        info("Para aprovar pedidos de permissão pelo iPad, informe o IP reservado do iPad.")
        cl["trusted_ips"] = ask_list("IP(s) do iPad", cl.get("trusted_ips") or [])
        cl["projects"] = ask_list("Pastas de projetos para o botão 'Abrir'", cl.get("projects") or [ROOT.as_posix()])


def step_launcher(cfg: dict, example: dict) -> None:
    title("4/6  Atalhos")
    info("A aba Atalhos tem páginas de botões (apps, pastas, sites, scripts, macros).")
    info("Os botões ficam no config.json, na chave \"launcher\" (exemplos no README).")
    if cfg.get("launcher") and cfg["launcher"] != example.get("launcher"):
        info("Mantendo os seus atalhos atuais.")
        return
    if not ask_yes("Começar com os atalhos de exemplo (Chrome, Spotify, Downloads, Ctrl+C...)?", True):
        cfg["launcher"] = [{"name": "Atalhos", "buttons": []}]


def step_windows(cfg: dict) -> None:
    title("5/6  Integração com o Windows")
    port = int(cfg["port"])

    # Firewall
    ports = firewall_rule_ports()
    if ports is not None and str(port) in ports:
        info(f'Firewall: a regra "{FIREWALL_RULE}" já libera a porta {port}.')
    else:
        info("O Firewall do Windows bloqueia o iPad até liberar a porta (só em redes Privadas).")
        if ask_yes(f"Liberar a porta {port} no firewall? (pede permissão de administrador)", True):
            cmd = (f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue; "
                   f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound -Protocol TCP "
                   f"-LocalPort {port} -Action Allow -Profile Private")
            run_elevated(cmd)
            if str(port) in (firewall_rule_ports() or []):
                info("Regra criada.")
            else:
                info("Não foi possível criar a regra (UAC recusado?). Veja o README para o comando manual.")

    public = public_networks()
    if public:
        info(f"Aviso: a rede {', '.join(public)} está como PÚBLICA. O iPad não vai conseguir conectar.")
        info("Mude para Privada em Configurações > Rede e Internet > (sua rede) > Tipo de perfil de rede.")

    # Inicialização automática
    info()
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    has_autostart = (startup / "PC Control Center.lnk").exists()
    if has_autostart:
        info("O servidor já inicia junto com o Windows.")
    elif ask_yes("Iniciar o servidor automaticamente quando você entrar no Windows?", True):
        script = ROOT / "scripts" / "autostart.ps1"
        rc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
                            ).returncode
        info("Pronto: atalho criado na pasta Inicializar." if rc == 0 else "Falhou; veja o README.")


def step_claude_hooks(cfg: dict) -> None:
    if not cfg.get("features", {}).get("claude"):
        return
    info()
    info("Os alertas do Claude Code precisam de hooks na configuração GLOBAL dele")
    info("(~/.claude/settings.json, com backup automático; seus hooks atuais são mantidos).")
    if ask_yes("Instalar os hooks do Claude Code agora?", False):
        script = ROOT / "scripts" / "install_claude_hooks.py"
        subprocess.run([sys.executable, str(script)])


def summary(cfg: dict) -> None:
    title("6/6  Pronto!")
    port = cfg["port"]
    query = f"?token={cfg['token']}" if cfg.get("require_token") else ""
    info(f"Configuração salva em {CONFIG_PATH}")
    info()
    info("No iPad (mesma rede Wi-Fi), abra no Safari:")
    for ip in lan_ips() or ["<ip-do-pc>"]:
        info(f"    http://{ip}:{port}/{query}")
    info()
    info("Depois toque em Compartilhar > Adicionar à Tela de Início para abrir em tela cheia.")
    info("Para refazer esta configuração: start.bat --setup")
    info("Ajustes finos (atalhos, volumes do modo jogo, alertas): edite o config.json.")


# ----------------------------------------------------------------------------- principal
def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_saved = False


def run() -> int:
    global _saved
    example = read_example()
    existing: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except ValueError:
            print(f"config.json inválido; ele será substituído (backup em {CONFIG_PATH.name}.bak).")
    cfg = deep_merge(deep_merge({k: v for k, v in DEFAULTS.items() if k != "launcher"}, example), existing)
    if not existing:
        cfg["features"] = {}  # primeira vez: o padrão de cada aba vem da detecção

    print("\n" + "=" * 64)
    print("  PC Control Center - configuração inicial")
    print("=" * 64)
    info("Responda as perguntas ou só aperte Enter para aceitar o valor entre colchetes.")
    info("Ctrl+C cancela sem salvar nada.")
    if existing:
        info("Já existe um config.json: os valores atuais aparecem como padrão.")

    step_network(cfg)
    step_telemetry(cfg)
    step_features(cfg)
    step_launcher(cfg, example)

    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_name(f"config.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(CONFIG_PATH, backup)
        info(f"Backup do config anterior: {backup.name}")
    write_config(cfg)
    _saved = True

    step_windows(cfg)
    step_claude_hooks(cfg)
    summary(cfg)
    return 0


def main() -> int:
    if not sys.stdin.isatty():
        print("[setup] Sem terminal interativo; rode start.bat --setup numa janela de console.")
        return 1
    try:
        return run()
    except (KeyboardInterrupt, Abort):
        print("\n\n  Cancelado." + (" O config.json já tinha sido salvo." if _saved else " Nada foi salvo."))
        return 1


if __name__ == "__main__":
    sys.exit(main())
