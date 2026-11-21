"""Instala (ou remove) os hooks do Control Center no Claude Code.

    .venv\\Scripts\\python.exe scripts\\install_claude_hooks.py          # instala
    .venv\\Scripts\\python.exe scripts\\install_claude_hooks.py --remove # remove

Edita ~/.claude/settings.json (configuração GLOBAL do Claude Code), sempre com
backup ao lado (settings.json.bak-AAAAMMDD-HHMMSS). Hooks que você já tenha
são preservados; só entradas que chamam claude_hook.py são adicionadas/removidas.
"""
import json
import shutil
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
HOOK = ROOT / "scripts" / "claude_hook.py"
MARK = "claude_hook.py"
EVENTS = {
    "SessionStart": 10, "UserPromptSubmit": 10, "Notification": 10,
    "Stop": 10, "SessionEnd": 10,
    "PermissionRequest": 130,  # segura até 120 s esperando o iPad (só com o modo remoto ligado)
}


def command() -> str:
    py = PYTHON if PYTHON.exists() else Path(sys.executable)
    return f'"{py.as_posix()}" "{HOOK.as_posix()}"'


def load() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    if SETTINGS.exists():
        backup = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print(f"backup: {backup}")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def strip_ours(groups: list) -> list:
    out = []
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
        if hooks:
            out.append({**g, "hooks": hooks})
    return out


def main() -> int:
    remove = "--remove" in sys.argv[1:]
    data = load()
    hooks = data.setdefault("hooks", {})
    for event, timeout in EVENTS.items():
        groups = strip_ours(hooks.get(event, []))
        if not remove:
            groups.append({"hooks": [{"type": "command", "command": command(), "timeout": timeout}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks")
    save(data)
    print("hooks removidos" if remove else f"hooks instalados: {', '.join(EVENTS)}\ncomando: {command()}")
    print("Abra uma sessão nova do Claude Code para valer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
