"""Discord via RPC local (pipe \\\\.\\pipe\\discord-ipc-N).

Precisa de um app SEU no Discord Developer Portal (o dono do app pode
autorizar os scopes rpc.voice.*). config.json:
    "discord": {"client_id": "...", "client_secret": "...",
                "group_channel_id": "...", "redirect_uri": "http://localhost"}

Fluxo: handshake -> AUTHORIZE (popup no Discord, uma vez) -> troca do code por
token (HTTPS) -> AUTHENTICATE. O token fica em data/discord_token.json.
Frame do IPC: opcode (uint32 LE) + tamanho (uint32 LE) + JSON.
Usa create_pipe_connection do ProactorEventLoop (I/O sobreposto): leitura e
escrita no mesmo pipe sem uma travar a outra.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .notify import notifier
from ..config import ROOT
from ..protocol import CommandError, Dispatcher

log = logging.getLogger("discord")

OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2
SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
TOKEN_FILE = ROOT / "data" / "discord_token.json"


class _Proto(asyncio.Protocol):
    def __init__(self, owner: "Discord") -> None:
        self.owner, self.buf = owner, b""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.owner._bind(transport)
        self.owner._send(OP_HANDSHAKE, {"v": 1, "client_id": self.owner.client_id})

    def data_received(self, data: bytes) -> None:
        self.buf += data
        while len(self.buf) >= 8:
            op, ln = struct.unpack("<II", self.buf[:8])
            if len(self.buf) < 8 + ln:
                break
            raw, self.buf = self.buf[8:8 + ln], self.buf[8 + ln:]
            try:
                self.owner._on_frame(op, json.loads(raw))
            except ValueError:
                pass

    def connection_lost(self, exc: Exception | None) -> None:
        self.owner._on_lost()


class Discord:
    def __init__(self, cfg: dict) -> None:
        c = cfg.get("discord") or {}
        self.client_id = str(c.get("client_id") or "")
        self.client_secret = str(c.get("client_secret") or "")
        self.group_channel = str(c.get("group_channel_id") or "")
        self.redirect_uri = c.get("redirect_uri") or "http://localhost"
        self.configured = bool(self.client_id and self.client_secret)
        self._transport: asyncio.BaseTransport | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ready: asyncio.Future | None = None
        self.connected = False
        self.authorized = False
        self.error = ""
        self.me_id = ""
        self.channel: dict[str, Any] | None = None
        self.members: dict[str, dict[str, Any]] = {}
        self.speaking: set[str] = set()
        self.self_state = {"mute": False, "deaf": False}
        self._guild_names: dict[str, str] = {}
        self._want_authorize = False

    # ---------- transporte ----------
    def _send(self, op: int, payload: dict) -> None:
        if self._transport is None:
            raise CommandError("Discord desconectado")
        data = json.dumps(payload).encode("utf-8")
        self._transport.write(struct.pack("<II", op, len(data)) + data)  # type: ignore[attr-defined]

    async def cmd(self, command: str, args: dict | None = None, evt: str | None = None, timeout: float = 5.0) -> Any:
        nonce = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[nonce] = fut
        payload: dict[str, Any] = {"cmd": command, "args": args or {}, "nonce": nonce}
        if evt:
            payload["evt"] = evt
        try:
            self._send(OP_FRAME, payload)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(nonce, None)

    def _on_frame(self, op: int, msg: dict) -> None:
        if op == OP_CLOSE:
            self.error = str(msg.get("message") or "Discord fechou a conexão")
            return
        nonce = msg.get("nonce")
        if nonce and nonce in self._pending:
            fut = self._pending[nonce]
            if not fut.done():
                if msg.get("evt") == "ERROR":
                    fut.set_exception(CommandError(str((msg.get("data") or {}).get("message", "erro do Discord"))))
                else:
                    fut.set_result(msg.get("data"))
            return
        if msg.get("cmd") == "DISPATCH":
            self._on_event(str(msg.get("evt")), msg.get("data") or {})

    def _on_lost(self) -> None:
        self._transport = None
        self.connected = self.authorized = False
        self.channel, self.members = None, {}
        self.speaking.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CommandError("Discord desconectado"))

    # ---------- conexão / auth ----------
    async def run(self) -> None:
        """Supervisor: conecta, autentica e reconecta a cada 5 s se cair."""
        if not self.configured:
            return
        while True:
            try:
                if self._transport is None:
                    await self._connect()
            except Exception as e:  # noqa: BLE001
                if self.connected:
                    log.warning("Discord RPC: %s", e)
                self.error = str(e) if self.connected else self.error
            await asyncio.sleep(5)

    async def _connect(self) -> None:
        loop = asyncio.get_running_loop()
        last_err: Exception | None = None
        for i in range(10):
            try:
                await loop.create_pipe_connection(lambda: _Proto(self), rf"\\.\pipe\discord-ipc-{i}")  # type: ignore[attr-defined]
                break
            except OSError as e:
                last_err = e
        else:
            raise ConnectionError(f"Discord não está aberto ({last_err})")
        # connection_made guarda o transporte e manda o handshake; o READY chega como evento.

    def _bind(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    async def _after_ready(self) -> None:
        self.connected = True
        self.error = ""
        token = self._load_token()
        try:
            if token is None:
                if not self._want_authorize:
                    self.error = "Toque em Entrar no grupo para autorizar"
                    return
                token = await self._authorize()
            try:
                auth = await self.cmd("AUTHENTICATE", {"access_token": token["access_token"]})
            except CommandError:
                token = await self._refresh(token) if token.get("refresh_token") else None
                if token is None:
                    raise
                auth = await self.cmd("AUTHENTICATE", {"access_token": token["access_token"]})
            self.me_id = str(((auth or {}).get("user") or {}).get("id", ""))
            self.authorized = True
            self.error = ""
            log.info("Discord RPC autenticado")
            await self.cmd("SUBSCRIBE", evt="VOICE_CHANNEL_SELECT")
            await self.cmd("SUBSCRIBE", evt="VOICE_SETTINGS_UPDATE")
            vs = await self.cmd("GET_VOICE_SETTINGS")
            if isinstance(vs, dict):
                self.self_state = {"mute": bool(vs.get("mute")), "deaf": bool(vs.get("deaf"))}
            await self._load_channel()
        except Exception as e:  # noqa: BLE001
            self.authorized = False
            self.error = f"Autorização falhou: {e}"
            log.warning(self.error)

    async def _authorize(self) -> dict:
        self._want_authorize = False
        data = await self.cmd("AUTHORIZE", {"client_id": self.client_id, "scopes": SCOPES}, timeout=120)
        code = (data or {}).get("code")
        if not code:
            raise CommandError("o Discord não devolveu o código de autorização")
        return await self._token_request({"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": self.redirect_uri})

    async def _refresh(self, token: dict) -> dict | None:
        try:
            return await self._token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
        except CommandError:
            TOKEN_FILE.unlink(missing_ok=True)
            return None

    async def _token_request(self, form: dict) -> dict:
        form = {"client_id": self.client_id, "client_secret": self.client_secret, **form}
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post("https://discord.com/api/oauth2/token", data=form)
        if r.status_code != 200:
            raise CommandError(f"troca de token falhou ({r.status_code}): {r.text[:120]}")
        tok = r.json()
        tok["expires_at"] = time.time() + float(tok.get("expires_in", 0))
        TOKEN_FILE.parent.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(tok), encoding="utf-8")
        return tok

    @staticmethod
    def _load_token() -> dict | None:
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---------- canal de voz ----------
    async def _load_channel(self) -> None:
        old = (self.channel or {}).get("id")
        ch = await self.cmd("GET_SELECTED_VOICE_CHANNEL")
        if old:
            for evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE", "SPEAKING_START", "SPEAKING_STOP"):
                try:
                    await self.cmd("UNSUBSCRIBE", {"channel_id": old}, evt=evt)
                except CommandError:
                    pass
        self.members, self.speaking = {}, set()
        if not isinstance(ch, dict):
            self.channel = None
            return
        guild = ""
        gid = ch.get("guild_id")
        if gid:
            if gid not in self._guild_names:
                try:
                    g = await self.cmd("GET_GUILD", {"guild_id": gid})
                    self._guild_names[gid] = (g or {}).get("name", "")
                except CommandError:
                    self._guild_names[gid] = ""
            guild = self._guild_names[gid]
        self.channel = {"id": ch.get("id"), "name": ch.get("name") or "Chamada", "guild": guild}
        for vs in ch.get("voice_states") or []:
            self._upsert(vs)
        for evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE", "SPEAKING_START", "SPEAKING_STOP"):
            await self.cmd("SUBSCRIBE", {"channel_id": ch.get("id")}, evt=evt)

    def _upsert(self, vs: dict) -> dict:
        user = vs.get("user") or {}
        uid = str(user.get("id", ""))
        st = vs.get("voice_state") or {}
        avatar = user.get("avatar")
        m = {
            "id": uid,
            "name": vs.get("nick") or user.get("global_name") or user.get("username") or "?",
            "avatar": f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=64" if avatar else None,
            "mute": bool(st.get("self_mute") or st.get("mute")),
            "deaf": bool(st.get("self_deaf") or st.get("deaf")),
            "volume": int(round(vs.get("volume", 100))) if vs.get("volume") is not None else None,
            "is_self": uid == self.me_id,
        }
        self.members[uid] = m
        return m

    def _on_event(self, evt: str, data: dict) -> None:
        if evt == "READY":
            loop = asyncio.get_running_loop()
            loop.create_task(self._after_ready())
        elif evt == "VOICE_CHANNEL_SELECT":
            asyncio.get_running_loop().create_task(self._load_channel())
        elif evt == "VOICE_SETTINGS_UPDATE":
            self.self_state = {"mute": bool(data.get("mute")), "deaf": bool(data.get("deaf"))}
        elif evt == "VOICE_STATE_CREATE":
            m = self._upsert(data)
            if not m["is_self"]:
                notifier.notify("discord", f"{m['name']} entrou na call", (self.channel or {}).get("name", ""), "info")
        elif evt == "VOICE_STATE_UPDATE":
            self._upsert(data)
        elif evt == "VOICE_STATE_DELETE":
            uid = str((data.get("user") or {}).get("id", ""))
            m = self.members.pop(uid, None)
            self.speaking.discard(uid)
            if m and not m["is_self"]:
                notifier.notify("discord", f"{m['name']} saiu da call", "", "info", sound=False)
        elif evt == "SPEAKING_START":
            self.speaking.add(str(data.get("user_id")))
        elif evt == "SPEAKING_STOP":
            self.speaking.discard(str(data.get("user_id")))

    # ---------- estado / comandos ----------
    async def state(self) -> dict[str, Any]:
        ch = None
        if self.channel:
            members = sorted(self.members.values(), key=lambda m: (not m["is_self"], m["name"].lower()))
            ch = {**self.channel, "members": [{**m, "speaking": m["id"] in self.speaking} for m in members]}
        return {"configured": self.configured, "connected": self.connected, "authorized": self.authorized,
                "error": self.error, "channel": ch, "self": self.self_state}

    def _need_auth(self) -> None:
        if not self.configured:
            raise CommandError('configure "discord" no config.json')
        if not self.connected:
            raise CommandError("Discord fechado no PC")
        if not self.authorized:
            raise CommandError("autorize o app no Discord primeiro")

    async def join_group(self, _v: Any = None) -> None:
        if self.configured and self.connected and not self.authorized:
            self._want_authorize = True
            await self._after_ready()  # abre o popup de autorização no Discord
            if not self.authorized:
                raise CommandError(self.error or "autorização pendente")
        self._need_auth()
        if not self.group_channel:
            raise CommandError('defina "discord.group_channel_id" no config.json')
        await self.cmd("SELECT_VOICE_CHANNEL", {"channel_id": self.group_channel, "force": True}, timeout=10)

    async def leave(self, _v: Any = None) -> None:
        self._need_auth()
        await self.cmd("SELECT_VOICE_CHANNEL", {"channel_id": None})

    async def toggle_mute(self, _v: Any = None) -> None:
        self._need_auth()
        await self.cmd("SET_VOICE_SETTINGS", {"mute": not self.self_state["mute"]})

    async def toggle_deaf(self, _v: Any = None) -> None:
        self._need_auth()
        await self.cmd("SET_VOICE_SETTINGS", {"deaf": not self.self_state["deaf"]})

    async def user_volume(self, v: Any) -> None:
        self._need_auth()
        if not isinstance(v, dict) or not v.get("id"):
            raise CommandError("payload inválido")
        vol = max(0, min(200, int(v.get("volume", 100))))
        await self.cmd("SET_USER_VOICE_SETTINGS", {"user_id": str(v["id"]), "volume": vol})
        if str(v["id"]) in self.members:
            self.members[str(v["id"])]["volume"] = vol


_discord: Discord | None = None


def register(d: Dispatcher, cfg: dict) -> Discord:
    global _discord
    _discord = Discord(cfg)
    d.on("discord.join_group", _discord.join_group, push_state=True)
    d.on("discord.leave", _discord.leave, push_state=True)
    d.on("discord.mute", _discord.toggle_mute, push_state=True)
    d.on("discord.deaf", _discord.toggle_deaf, push_state=True)
    d.on("discord.user_volume", _discord.user_volume)
    return _discord
