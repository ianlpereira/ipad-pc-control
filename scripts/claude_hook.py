"""Hook do Claude Code -> Control Center (só biblioteca padrão).

Configurado por scripts/install_claude_hooks.py. O Claude Code passa o evento
como JSON na stdin. Regras de segurança:
- Se o servidor não responder, o hook sai sem decisão e o Claude Code segue
  o fluxo normal (prompt no terminal). Nunca permite nada por conta própria.
- Só fala com 127.0.0.1.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path


def server_url() -> str:
    port = 8765
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    try:
        port = int(json.loads(cfg.read_text(encoding="utf-8")).get("port", port))
    except (OSError, ValueError):
        pass
    port = int(os.environ.get("CC_PORT", port))  # override para testes
    return f"http://127.0.0.1:{port}"


def post(path: str, data: dict, timeout: float) -> dict:
    req = urllib.request.Request(server_url() + path, data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL fixa local
        body = r.read()
    return json.loads(body) if body else {}


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except ValueError:
        return 0
    event = data.get("hook_event_name")

    if event == "PermissionRequest":
        try:
            resp = post("/claude/permission", data, timeout=125)
        except Exception:  # noqa: BLE001 - servidor fora do ar: prompt normal
            return 0
        decision = resp.get("decision")
        if decision == "allow":
            out = {"behavior": "allow"}
        elif decision == "deny":
            out = {"behavior": "deny", "message": "Negado pelo iPad (Control Center)"}
        else:
            return 0
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}))
        return 0

    try:
        post("/claude/event", data, timeout=2)
    except Exception:  # noqa: BLE001 - painel desligado não pode atrapalhar o Claude Code
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
