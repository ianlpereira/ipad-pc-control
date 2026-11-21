"""Claude: alertas e aprovações do Claude Code (hooks), uso de tokens e atalhos.

Fluxo dos hooks (instalados por scripts/install_claude_hooks.py):
  Claude Code -> scripts/claude_hook.py -> POST /claude/event       (sessões, alertas)
                                        -> POST /claude/permission  (aprovar pelo iPad)

Aprovar pelo iPad só acontece com o "modo remoto" ligado na aba Claude.
Com ele desligado (padrão), o hook de permissão responde na hora sem decisão
e o Claude Code mostra o prompt normal no PC, sem nenhum atraso.
Decisões só são aceitas de IPs em config "claude.trusted_ips".
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .notify import notifier
from ..protocol import CommandError, Dispatcher

log = logging.getLogger("claude")

CLAUDE_HOME = Path.home() / ".claude"
PERMISSION_TIMEOUT = 120.0
STATUS_MSG = {"permission_prompt": "Pedindo permissão", "idle_prompt": "Esperando sua resposta"}


def _project(cwd: str | None) -> str:
    return Path(cwd).name if cwd else "?"


def _summarize(tool: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input)[:300]
    for key in ("command", "file_path", "url", "pattern", "path", "prompt"):
        if tool_input.get(key):
            extra = f"\n{tool_input['description']}" if key == "command" and tool_input.get("description") else ""
            return (str(tool_input[key]) + extra)[:300]
    return json.dumps(tool_input, ensure_ascii=False)[:300]


class Usage:
    """Soma tokens dos transcripts locais (~/.claude/projects/**/*.jsonl), de forma incremental."""

    def __init__(self) -> None:
        self._offsets: dict[Path, int] = {}
        self._seen: set[tuple[str, str]] = set()
        self._by_day: dict[str, dict[str, int]] = {}   # "YYYY-MM-DD" -> {projeto: tokens}
        self.snapshot: dict[str, Any] = {"today": None, "week": None, "top": []}
        self._lock = threading.Lock()

    def scan(self) -> None:
        root = CLAUDE_HOME / "projects"
        if not root.is_dir():
            return
        cutoff = time.time() - 8 * 86400
        for f in root.rglob("*.jsonl"):
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    continue
                start = self._offsets.get(f, 0)
                if st.st_size <= start:
                    continue
                with f.open("rb") as fh:
                    fh.seek(start)
                    data = fh.read()
                end = data.rfind(b"\n") + 1  # só linhas completas
                self._offsets[f] = start + end
                for raw in data[:end].splitlines():
                    self._line(raw)
            except OSError:
                continue
        self._summarize()

    def _line(self, raw: bytes) -> None:
        if b'"usage"' not in raw:
            return
        try:
            o = json.loads(raw)
        except ValueError:
            return
        m = o.get("message") or {}
        u = m.get("usage")
        if o.get("type") != "assistant" or not isinstance(u, dict):
            return
        key = (str(m.get("id")), str(o.get("requestId")))
        if key in self._seen:  # o Claude Code grava a mesma mensagem várias vezes
            return
        self._seen.add(key)
        total = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                   "cache_creation_input_tokens", "cache_read_input_tokens"))
        try:
            ts = datetime.fromisoformat(str(o.get("timestamp")).replace("Z", "+00:00")).astimezone()
        except ValueError:
            return
        day = ts.strftime("%Y-%m-%d")
        proj = _project(o.get("cwd"))
        bucket = self._by_day.setdefault(day, {})
        bucket[proj] = bucket.get(proj, 0) + total

    def _summarize(self) -> None:
        today = datetime.now().date()
        days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        week: dict[str, int] = {}
        for d in days:
            for p, n in self._by_day.get(d, {}).items():
                week[p] = week.get(p, 0) + n
        top = sorted(week.items(), key=lambda kv: kv[1], reverse=True)[:3]
        with self._lock:
            self.snapshot = {
                "today": sum(self._by_day.get(days[0], {}).values()),
                "week": sum(week.values()),
                "top": [{"project": p, "tokens": n} for p, n in top],
            }


class Claude:
    def __init__(self, cfg: dict) -> None:
        c = cfg.get("claude") or {}
        self.trusted_ips: set[str] = set(c.get("trusted_ips") or [])
        self.projects: list[str] = [os.path.expandvars(os.path.expanduser(p)) for p in c.get("projects") or []]
        self.app_id: str = c.get("app_id") or "Claude_pzs8sxrjxfjjc!Claude"
        self.remote: bool = False
        self.sessions: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self.usage = Usage()
        self._hooks_cache: tuple[float, bool] = (0.0, False)
        threading.Thread(target=self._usage_loop, name="claude-usage", daemon=True).start()

    # ---------- uso ----------
    def _usage_loop(self) -> None:
        while True:
            try:
                self.usage.scan()
            except Exception:  # noqa: BLE001
                log.exception("falha ao ler uso do Claude")
            time.sleep(60)

    def hooks_installed(self) -> bool:
        ts, val = self._hooks_cache
        if time.monotonic() - ts < 30:
            return val
        try:
            val = "claude_hook.py" in (CLAUDE_HOME / "settings.json").read_text(encoding="utf-8")
        except OSError:
            val = False
        self._hooks_cache = (time.monotonic(), val)
        return val

    # ---------- estado ----------
    async def state(self) -> dict[str, Any]:
        now = time.time()
        for sid in [s for s, v in self.sessions.items() if now - v["ts"] > 24 * 3600]:
            del self.sessions[sid]
        sessions = sorted(self.sessions.values(), key=lambda s: s["ts"], reverse=True)[:8]
        perms = [{k: p[k] for k in ("id", "project", "tool", "summary", "ts")} for p in self.pending.values()]
        launch = [{"id": f"p{i}", "label": "▶ " + Path(p).name} for i, p in enumerate(self.projects)]
        launch += [{"id": "app", "label": "✳️ App do Claude"}, {"id": "web", "label": "🌐 claude.ai"}]
        return {
            "hooks": self.hooks_installed(), "remote": self.remote,
            "sessions": sessions, "permissions": perms,
            "usage": self.usage.snapshot, "launch": launch,
        }

    # ---------- eventos dos hooks ----------
    def on_event(self, data: dict[str, Any]) -> None:
        sid = str(data.get("session_id") or "?")
        ev = data.get("hook_event_name")
        proj = _project(data.get("cwd"))
        s = self.sessions.setdefault(sid, {"id": sid, "project": proj, "status": "done", "message": "", "ts": time.time()})
        s["project"], s["ts"] = proj, time.time()
        if ev == "SessionStart":
            s.update(status="done", message="Sessão iniciada")
        elif ev == "UserPromptSubmit":
            s.update(status="working", message=str(data.get("prompt") or "")[:120])
        elif ev == "Notification":
            kind = data.get("notification_type") or ""
            msg = str(data.get("message") or STATUS_MSG.get(kind, "Precisa de você"))
            s.update(status="waiting", message=msg[:120])
            notifier.notify("claude", f"Claude precisa de você — {proj}", msg[:120],
                            "warn" if kind == "permission_prompt" else "info")
        elif ev == "Stop":
            last = str(data.get("last_assistant_message") or "")
            s.update(status="done", message=last[:120] or "Tarefa concluída")
            notifier.notify("claude", f"Claude terminou — {proj}", last[:120], "ok")
        elif ev == "SessionEnd":
            self.sessions.pop(sid, None)

    async def on_permission(self, data: dict[str, Any]) -> dict[str, Any]:
        """Chamado pelo hook PermissionRequest. Responde {} (sem decisão) ou {"decision": ...}."""
        if not self.remote:
            return {}
        pid = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        proj = _project(data.get("cwd"))
        tool = str(data.get("tool_name") or "?")
        self.pending[pid] = {"id": pid, "project": proj, "tool": tool,
                             "summary": _summarize(tool, data.get("tool_input")), "ts": time.time(), "future": fut}
        notifier.notify("claude", f"Claude pede permissão — {proj}", f"{tool}: {self.pending[pid]['summary'][:80]}", "alert")
        try:
            decision = await asyncio.wait_for(fut, PERMISSION_TIMEOUT)
            return {"decision": decision}
        except asyncio.TimeoutError:
            return {}  # sem resposta no iPad: o Claude Code mostra o prompt normal
        finally:
            self.pending.pop(pid, None)

    # ---------- comandos do iPad ----------
    def _check_peer(self, peer: str | None) -> None:
        if not self.trusted_ips:
            raise CommandError('configure "claude.trusted_ips" no config.json com o IP do iPad')
        if peer not in self.trusted_ips:
            raise CommandError(f"este dispositivo ({peer}) não está em claude.trusted_ips")

    async def decide(self, v: Any, peer: str | None = None) -> None:
        self._check_peer(peer)
        try:
            pid, decision = str(v).split(":", 1)
        except ValueError:
            raise CommandError("payload inválido") from None
        if decision not in ("allow", "deny"):
            raise CommandError("decisão inválida")
        p = self.pending.get(pid)
        if p is None or p["future"].done():
            raise CommandError("esse pedido já expirou")
        p["future"].set_result(decision)

    async def set_remote(self, v: Any, peer: str | None = None) -> bool:
        self._check_peer(peer)
        self.remote = (not self.remote) if v is None else bool(v)
        return self.remote

    def launch(self, v: Any) -> None:
        target = str(v)
        if target == "app":
            os.startfile(f"shell:AppsFolder\\{self.app_id}")  # noqa: S606
        elif target == "web":
            os.startfile("https://claude.ai")  # noqa: S606
        elif target.startswith("p") and target[1:].isdigit() and int(target[1:]) < len(self.projects):
            path = self.projects[int(target[1:])]
            if not Path(path).is_dir():
                raise CommandError(f"pasta não encontrada: {path}")
            subprocess.Popen(["wt.exe", "-d", path, "cmd", "/k", "claude"])  # noqa: S603, S607
        else:
            raise CommandError("atalho desconhecido")


_claude: Claude | None = None


def register(d: Dispatcher, cfg: dict) -> Claude:
    global _claude
    _claude = Claude(cfg)
    d.on("claude.permission.decide", _claude.decide, with_peer=True)
    d.on("claude.remote", _claude.set_remote, with_peer=True)
    d.on("claude.launch", _claude.launch)
    return _claude
