"""Keyboard injection (ctypes SendInput) for the OCR -> click -> verify loop.

Two text paths:
  - keybd_scan():  KEYEVENTF_SCANCODE + KEYEVENTF_UNICODE, per character. This is
                   what a physical keyboard produces, so it works in apps that
                   ignore plain WM_CHAR.
  - keybd_vk():    virtual-key down/up, for shortcuts (ctrl+a, esc, ...).
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
KEYEVENTF_SCANCODE = 0x0008

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "capslock": 0x14, "printscreen": 0x2C,
    # The whole alphabet and digit row on purpose: Ctrl+R (reload) was missing and
    # chord("ctrl", "r") raised KeyError('r') -- a missing table entry looked like a
    # dead key combination. F5 is reload as well, hence its inclusion.
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46, "g": 0x47,
    "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E,
    "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54, "u": 0x55,
    "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _U(ctypes.Union):
    # The union must be 8-byte aligned, so on x64 INPUT is 40 bytes (4 TYPE +
    # 4 padding + 32 union). A bare KEYBDINPUT member yields only 32 and
    # SendInput rejects it with ERROR_INVALID_PARAMETER (87).
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def _send(*events) -> int:
    n = len(events)
    arr = (INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def _unicode_ev(ch: str, up: bool) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD,
                 ki=KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, None))


def type_text(text: str, per_char_delay: float = 0.012) -> None:
    """Type `text` as Unicode scancode events (works for CJK input)."""
    for ch in text:
        _send(_unicode_ev(ch, False))
        time.sleep(0.004)
        _send(_unicode_ev(ch, True))
        time.sleep(per_char_delay)


def key(vk_name: str, presses: int = 1) -> None:
    vk = VK.get(vk_name.lower())
    if vk is None:
        raise KeyError(f"unknown key {vk_name!r}")
    for _ in range(presses):
        _send(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, 0, 0, None)))
        time.sleep(0.02)
        _send(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)))
        time.sleep(0.03)


def chord(*keys: str) -> None:
    """Press several keys together, e.g. chord('ctrl', 'a')."""
    codes = [VK[k.lower()] for k in keys]
    for c in codes:
        _send(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(c, 0, 0, 0, None)))
        time.sleep(0.02)
    for c in reversed(codes):
        _send(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(c, 0, KEYEVENTF_KEYUP, 0, None)))
        time.sleep(0.02)


def paste(text: str) -> None:
    """Put `text` on the clipboard and paste it (best for long strings)."""
    import pyperclip
    pyperclip.copy(text)
    time.sleep(0.1)
    chord("ctrl", "v")
