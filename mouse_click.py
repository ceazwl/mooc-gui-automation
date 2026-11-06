"""Mouse control for the OCR -> click loop, using ctypes only (no extra deps).

On this host `SetCursorPos` fails and `SendInput(MOUSEEVENTF_ABSOLUTE)` does not
move the pointer, but `ClipCursor` to a 1px rect warps the pointer reliably and
`SendInput` button events are delivered to the focused/under-cursor window. So a
click is: warp onto the target, then inject button down/up.

Usage:
  python mouse_click.py --x 950 --y 1095
  python mouse_click.py --x 950 --y 1095 --double
  python mouse_click.py --x 950 --y 1095 --button right
  python mouse_click.py --x 950 --y 1095 --move-only
  python mouse_click.py --x 950 --y 1095 --verify-ref before.png --verify-out after.png
  python mouse_click.py --x 950 --y 1095 --no-warp     # only SendInput absolute move
"""
from __future__ import annotations

import argparse
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    # Must be 8-byte aligned: on x64 INPUT is 40 bytes (4 type + 4 padding +
    # 32 union). MOUSEINPUT is 32, the pad keeps the union (and INPUT) correct.
    _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def set_dpi_aware() -> None:
    """Must run before any coordinate is read, or metrics come back scaled."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    user32.SetProcessDPIAware()


def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pt.x, pt.y


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen() -> tuple[int, int, int, int]:
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def _send(*events: INPUT) -> int:
    n = len(events)
    arr = (INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def _ev(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data, flags, 0, None))


def warp(x: int, y: int, attempts: int = 3) -> tuple[int, int]:
    """Move the pointer by clipping it into a 1px rect, then release the clip."""
    for _ in range(attempts):
        r = RECT(x, y, x + 1, y + 1)
        if not user32.ClipCursor(ctypes.byref(r)):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.04)
        user32.ClipCursor(None)
        time.sleep(0.04)
        cx, cy = get_cursor_pos()
        if (cx, cy) == (x, y):
            return cx, cy
    return get_cursor_pos()


def set_cursor_pos(x: int, y: int) -> tuple[int, int]:
    """Plain SetCursorPos. Fails at Low integrity, works at Medium."""
    user32.SetCursorPos(x, y)
    time.sleep(0.03)
    return get_cursor_pos()


def position_target(x: int, y: int, method: str = "auto") -> tuple[int, int, str]:
    """Put the pointer on (x, y) using whichever mechanism this integrity allows.

    At Low integrity SetCursorPos and SendInput(MOVE|ABSOLUTE) are both blocked
    and only the ClipCursor trick works; at Medium integrity SetCursorPos works
    and ClipCursor does not. `auto` tries them in that order.
    """
    if method == "setcursorpos":
        lx, ly = set_cursor_pos(x, y)
        return lx, ly, "SetCursorPos"
    if method == "clip":
        lx, ly = warp(x, y)
        return lx, ly, "ClipCursor"
    if method == "sendinput":
        move_via_sendinput(x, y)
        time.sleep(0.05)
        lx, ly = get_cursor_pos()
        return lx, ly, "SendInput"
    landed = set_cursor_pos(x, y)
    if landed == (x, y):
        return (*landed, "SetCursorPos")
    landed = warp(x, y)
    if landed == (x, y):
        return (*landed, "ClipCursor")
    move_via_sendinput(x, y)
    time.sleep(0.05)
    return (*get_cursor_pos(), "SendInput")


def to_absolute(x: int, y: int) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    return (max(0, min(65535, int(round((x - vx) * 65535 / max(vw - 1, 1))))),
            max(0, min(65535, int(round((y - vy) * 65535 / max(vh - 1, 1))))))


def move_via_sendinput(x: int, y: int) -> None:
    ax, ay = to_absolute(x, y)
    _send(_ev(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))


MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


def scroll(notches: int, x: int | None = None, y: int | None = None) -> None:
    """Scroll the wheel at (x, y) (defaults to the current pointer position).

    Positive notches scroll up, negative scroll down -- the sign convention
    Windows uses for MOUSEEVENTF_WHEEL.
    """
    set_dpi_aware()
    if x is not None and y is not None:
        position_target(x, y)
        time.sleep(0.08)
    elif x is not None:
        move_via_sendinput(x, y)
        time.sleep(0.08)
    step = WHEEL_DELTA * (1 if notches > 0 else -1)
    for _ in range(abs(notches)):
        _send(_ev(MOUSEEVENTF_WHEEL, 0, 0, step & 0xFFFFFFFF))
        time.sleep(0.06)


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(x: int, y: int, button: str = "left", clicks: int = 1,
          hold: float = 0.0, settle: float = 0.06, use_warp: bool = True,
          method: str = "auto") -> tuple[int, int]:
    """Position the pointer on (x, y) and inject `clicks` button presses."""
    set_dpi_aware()
    down, up = _BUTTONS[button]
    if use_warp:
        # NOTE: index instead of unpacking. On this interpreter
        # `landed, _, _ = position_target(...)` binds the *last* element to
        # `landed` (verified via dis); indexing is unambiguous.
        res = position_target(x, y, method)
        landed = (res[0], res[1])
    else:
        move_via_sendinput(x, y)
        time.sleep(settle)
        landed = get_cursor_pos()
    time.sleep(settle)
    for i in range(clicks):
        _send(_ev(down))
        if hold > 0:
            time.sleep(hold)
        _send(_ev(up))
        if i < clicks - 1:
            time.sleep(0.07)
    return landed


_RID = {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium+",
        0x3000: "High", 0x4000: "System", 0x5000: "Protected"}


def integrity_level(pid: int | None = None) -> tuple[int, str] | None:
    """Return (rid, name) for a process token, or None if it cannot be read."""
    import os
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    h = k32.OpenProcess(0x1000, False, os.getpid() if pid is None else pid)
    if not h:
        return None
    tok = ctypes.c_void_p()
    if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
        k32.CloseHandle(h)
        return None
    size = wintypes.DWORD()
    adv.GetTokenInformation(tok, 25, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    rid = None
    if adv.GetTokenInformation(tok, 25, buf, size.value, ctypes.byref(size)):
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid = ctypes.cast(sid_ptr, ctypes.POINTER(ctypes.c_ubyte))
        n = sid[1]
        rid = adv.GetSidSubAuthority(sid_ptr, n - 1).contents.value
    k32.CloseHandle(tok)
    k32.CloseHandle(h)
    return (rid, _RID.get(rid, hex(rid))) if rid is not None else None


def target_window_integrity(x: int, y: int) -> dict:
    """Describe the window under (x, y) and its process integrity."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return {"hwnd": 0, "pid": 0, "integrity": "no window"}
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    lvl = integrity_level(pid.value)
    title = ctypes.create_unicode_buffer(200)
    user32.GetWindowTextW(hwnd, title, 200)
    return {"hwnd": hwnd, "pid": pid.value,
            "integrity": lvl[1] if lvl else "unreadable (higher integrity?)",
            "title": title.value}


def can_inject_into(x: int, y: int) -> tuple[bool, str]:
    """UIPI check: a Low-integrity process cannot inject input into Medium+ windows."""
    ours = integrity_level()
    tgt = target_window_integrity(x, y)
    if ours is None or not tgt.get("pid"):
        return True, "integrity unknown; not checked"
    tgt_lvl = integrity_level(tgt["pid"])
    if tgt_lvl is None:
        return (False, f"target pid {tgt['pid']} integrity is unreadable, which means it is "
                       f"higher than ours ({ours[1]}); UIPI will drop the synthetic click")
    if tgt_lvl[0] > ours[0]:
        return (False, f"we are {ours[1]} (0x{ours[0]:x}) but the target is {tgt_lvl[1]} "
                       f"(0x{tgt_lvl[0]:x}); UIPI will drop the synthetic click")
    return True, f"we are {ours[1]}, target is {tgt_lvl[1]}: injection allowed"


def pixel_diff(a_path: str, b_path: str, threshold: int = 16) -> dict:
    from PIL import Image, ImageChops
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        return {"same_size": False, "size_a": a.size, "size_b": b.size}
    diff = ImageChops.difference(a, b).convert("L")
    hist = diff.histogram()
    changed = sum(hist[threshold:])
    total = a.size[0] * a.size[1]
    return {
        "same_size": True,
        "changed_pixels": changed,
        "total_pixels": total,
        "changed_ratio": round(changed / total, 6),
        "diff_bbox": diff.point(lambda v: 255 if v >= threshold else 0).getbbox(),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Click a screen position (ctypes + SendInput).")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.add_argument("--button", choices=sorted(_BUTTONS), default="left")
    p.add_argument("--double", action="store_true")
    p.add_argument("--clicks", type=int, default=0)
    p.add_argument("--hold", type=float, default=0.0)
    p.add_argument("--settle", type=float, default=0.06)
    p.add_argument("--move-only", action="store_true")
    p.add_argument("--method", choices=("auto", "setcursorpos", "clip", "sendinput"),
                   default="auto", help="how to position the pointer (default: auto)")
    p.add_argument("--no-warp", action="store_true", help="use SendInput absolute move instead")
    p.add_argument("--verify-ref", help="screenshot taken before the click")
    p.add_argument("--verify-out", help="path for the screenshot taken after the click")
    p.add_argument("--verify-wait", type=float, default=0.6)
    p.add_argument("--check", action="store_true",
                   help="report whether UIPI will let this click through, then exit")
    args = p.parse_args()

    set_dpi_aware()

    if args.check:
        ours = integrity_level()
        tgt = target_window_integrity(args.x, args.y)
        ok, why = can_inject_into(args.x, args.y)
        print(f"ours       : {ours}")
        print(f"under point: {tgt}")
        print(f"injectable : {ok} - {why}")
        return 0 if ok else 3

    set_dpi_aware()
    before = get_cursor_pos()
    clicks = args.clicks or (2 if args.double else 1)

    injectable, why = can_inject_into(args.x, args.y)
    print(f"uipi       : {'ok' if injectable else 'BLOCKED'} - {why}")

    if args.move_only:
        if args.no_warp:
            move_via_sendinput(args.x, args.y)
            time.sleep(args.settle)
            landed, how = get_cursor_pos(), "SendInput"
        else:
            lx, ly, how = position_target(args.x, args.y, args.method)
            landed = (lx, ly)
    else:
        landed = click(args.x, args.y, button=args.button, clicks=clicks, hold=args.hold,
                       settle=args.settle, use_warp=not args.no_warp, method=args.method)
        how = args.method

    print(f"target=({args.x},{args.y}) landed={tuple(landed)} via={how} cursor_before={before} "
          f"button={args.button} clicks={clicks} screen={screen_size()}")

    if args.verify_ref and args.verify_out:
        time.sleep(args.verify_wait)
        import grab
        grab.grab(args.verify_out)
        print("verify:", pixel_diff(args.verify_ref, args.verify_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
