"""Simulação de teclado via SendInput (ctypes puro, sem hooks globais).

Uso:
    press_combo("ctrl+shift+esc")
    type_text("olá")
    run_macro([{"keys": "win+r"}, {"delay": 300}, {"text": "notepad"}, {"keys": "enter"}])

Obs.: por causa do UIPI do Windows, teclas enviadas por um processo comum
NÃO chegam em janelas rodando como administrador. Rode o servidor como
admin se precisar controlar apps elevados.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT precisa estar na union para o sizeof(INPUT) bater com o do Windows.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

# Nome -> (virtual key, é tecla estendida?)
VK: dict[str, tuple[int, bool]] = {
    "ctrl": (0xA2, False), "control": (0xA2, False), "rctrl": (0xA3, True),
    "shift": (0xA0, False), "rshift": (0xA1, False),
    "alt": (0xA4, False), "ralt": (0xA5, True), "altgr": (0xA5, True),
    "win": (0x5B, True), "cmd": (0x5B, True),
    "tab": (0x09, False), "enter": (0x0D, False), "return": (0x0D, False),
    "esc": (0x1B, False), "escape": (0x1B, False), "space": (0x20, False),
    "backspace": (0x08, False), "delete": (0x2E, True), "del": (0x2E, True), "insert": (0x2D, True),
    "home": (0x24, True), "end": (0x23, True), "pageup": (0x21, True), "pagedown": (0x22, True),
    "left": (0x25, True), "up": (0x26, True), "right": (0x27, True), "down": (0x28, True),
    "printscreen": (0x2C, True), "prtsc": (0x2C, True), "capslock": (0x14, False),
    "numlock": (0x90, True), "scrolllock": (0x91, False), "pause": (0x13, False), "apps": (0x5D, True),
    "media_next": (0xB0, True), "media_prev": (0xB1, True), "media_stop": (0xB2, True),
    "media_playpause": (0xB3, True), "volume_mute": (0xAD, True),
    "volume_down": (0xAE, True), "volume_up": (0xAF, True),
    ";": (0xBA, False), "=": (0xBB, False), ",": (0xBC, False), "-": (0xBD, False),
    ".": (0xBE, False), "/": (0xBF, False), "`": (0xC0, False), "[": (0xDB, False),
    "\\": (0xDC, False), "]": (0xDD, False), "'": (0xDE, False),
}
for i in range(1, 25):
    VK[f"f{i}"] = (0x6F + i, False)
for c in "0123456789":
    VK[c] = (ord(c), False)
for c in "abcdefghijklmnopqrstuvwxyz":
    VK[c] = (ord(c.upper()), False)
for i in range(10):
    VK[f"num{i}"] = (0x60 + i, False)


def _key_input(vk: int, extended: bool, up: bool) -> INPUT:
    flags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput enviou {sent}/{len(inputs)} eventos (erro {ctypes.get_last_error()})")


def parse_combo(combo: str) -> list[tuple[int, bool]]:
    keys = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part == "":  # permite "ctrl++" => tecla "+"
            part = "="
        if part not in VK:
            raise ValueError(f"tecla desconhecida: {part!r}")
        keys.append(VK[part])
    return keys


def press_combo(combo: str) -> None:
    """Pressiona todas as teclas em ordem e solta em ordem inversa."""
    keys = parse_combo(combo)
    downs = [_key_input(vk, ext, False) for vk, ext in keys]
    ups = [_key_input(vk, ext, True) for vk, ext in reversed(keys)]
    _send(downs + ups)


def tap_vk(name: str) -> None:
    vk, ext = VK[name]
    _send([_key_input(vk, ext, False), _key_input(vk, ext, True)])


def type_text(text: str) -> None:
    """Digita texto Unicode (acentos, emoji) independente do layout do teclado."""
    inputs: list[INPUT] = []
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        for up in (False, True):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = KEYBDINPUT(wVk=0, wScan=code,
                                  dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                                  time=0, dwExtraInfo=0)
            inputs.append(inp)
    if inputs:
        _send(inputs)


def run_macro(steps: list[dict]) -> None:
    """Executa passos: {"keys": "ctrl+c"} | {"text": "..."} | {"delay": ms}."""
    for step in steps:
        if "keys" in step:
            press_combo(step["keys"])
        elif "text" in step:
            type_text(step["text"])
        elif "delay" in step:
            time.sleep(min(float(step["delay"]), 10_000) / 1000)
        else:
            raise ValueError(f"passo de macro inválido: {step}")
        time.sleep(0.02)  # pequeno respiro para o app alvo processar
