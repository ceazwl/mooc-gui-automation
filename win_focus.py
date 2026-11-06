"""List visible browser windows and optionally bring one to the foreground by title match."""
import argparse
import ctypes
import sys
import time
from ctypes import wintypes

u32 = ctypes.WinDLL("user32", use_last_error=True)


def set_dpi_aware():
    """Without this, GetWindowRect returns scaled logical px, not screen px."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass
    u32.SetProcessDPIAware()


set_dpi_aware()

BROWSERS = ("chrome", "msedge", "firefox", "iexplore", "brave", "opera")


def title_of(h):
    t = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(h, t, 512)
    return t.value


def rect_of(h):
    r = wintypes.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def class_of(h):
    c = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, c, 256)
    return c.value


def proc_name(pid):
    """Image name of a pid, with no third-party dependency.

    psutil is NOT installed in every interpreter this project runs under (the DSH
    bundled python has no psutil), and a ModuleNotFoundError raised inside the
    EnumWindows callback is swallowed by ctypes: windows() then returned an EMPTY
    list, focus_browser() said "no Edge window found", and every click was
    refused. So ask psutil only if it is there, and use the Win32 API otherwise.
    """
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        pass
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    except Exception:
        pass
    finally:
        k32.CloseHandle(h)
    return "?"


def windows(min_area=80000):
    out = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        # include minimized windows: a minimized browser has a tiny/offscreen
        # rect and IsWindowVisible still reports True, so filtering on "visible
        # and titled" alone silently hides windows we need to restore.
        if not u32.IsWindowVisible(h) and not u32.IsIconic(h):
            return True
        t = title_of(h)
        if not t:
            return True
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        name = proc_name(pid.value).lower().replace(".exe", "")
        if name in ("", "?"):
            # the process name is unreadable (a Low-integrity process can be denied
            # even read access): fall back to the window class, which still
            # identifies a browser. Only used then -- every Electron app (DSH
            # included) shares the Chrome_WidgetWin class, so trusting the class
            # when the name IS known drags in the harness itself.
            cls = class_of(h).lower()
            is_browser = cls.startswith(("chrome_widgetwin", "mozillawindowclass", "ieframe"))
        else:
            is_browser = any(b in name for b in BROWSERS)
        if not is_browser:
            return True
        l, tp, r, b = rect_of(h)
        minimized = bool(u32.IsIconic(h))
        area = 0 if minimized else max(0, r - l) * max(0, b - tp)
        if not minimized and area < min_area:
            return True
        out.append({"hwnd": h, "pid": pid.value, "proc": name or cls,
                    "title": t, "rect": (l, tp, r, b), "area": area,
                    "minimized": minimized})
        return True

    u32.EnumWindows(CB(cb), 0)
    out.sort(key=lambda w: -w["area"])
    return out


def activate(hwnd, allow_taskbar_click=True):
    """Restore + foreground a window, working around the foreground lock.

    SetForegroundWindow is refused when another process owns the foreground
    (DSH does, constantly), so escalation: attach-thread trick, then a real
    click on the window's taskbar button, which the shell always honours.
    """
    u32.ShowWindow(hwnd, 9)          # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if u32.GetForegroundWindow() == hwnd:
        return True

    fg = u32.GetForegroundWindow()
    tid_fg = u32.GetWindowThreadProcessId(fg, None)
    tid_us = ctypes.WinDLL("kernel32").GetCurrentThreadId()
    u32.AttachThreadInput(tid_us, tid_fg, True)
    u32.BringWindowToTop(hwnd)
    u32.SetForegroundWindow(hwnd)
    u32.AttachThreadInput(tid_us, tid_fg, False)
    time.sleep(0.25)
    if u32.GetForegroundWindow() == hwnd:
        return True

    if allow_taskbar_click:
        try:
            import mouse_click
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = proc_name(pid.value).lower()
            key = "msedge" if "msedge" in name else "chrome" if "chrome" in name else None
            if key:
                sb = taskbar_buttons()
                bx = sb.get(key)
                if bx:
                    x, y = bx
                    mouse_click.click(x, y)
                    time.sleep(0.7)
                    u32.ShowWindow(hwnd, 9)
                    u32.SetForegroundWindow(hwnd)
                    time.sleep(0.3)
        except Exception:
            pass

    if u32.GetForegroundWindow() != hwnd:
        # Last resort, and the most reliable one: a real click on the window's OWN
        # tab strip. Windows activates the window under a click, and empty tab-strip
        # space does nothing else. Measured while the Windows taskbar itself held the
        # foreground (after dismissing the Quick Settings flyout): both
        # SetForegroundWindow attempts and the taskbar-button click failed, and this
        # one worked.
        try:
            import mouse_click
            mouse_click.set_dpi_aware()
            l, t, r, b = rect_of(hwnd)
            mouse_click.click(l + int((r - l) * 0.55), t + int((b - t) * 0.02))
            time.sleep(0.5)
        except Exception:
            pass
    return u32.GetForegroundWindow() == hwnd


def taskbar_buttons():
    """Map browser process name -> x centre of its taskbar button, if visible."""
    sb = u32.FindWindowW("Shell_TrayWnd", None)
    if not sb:
        return {}
    r = wintypes.RECT()
    u32.GetWindowRect(sb, ctypes.byref(r))
    y = r.top + (r.bottom - r.top) // 2
    found = {}
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        tid = u32.GetWindowThreadProcessId(h, None)
        cls = ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(h, cls, 128)
        if cls.value not in ("MSTaskSwWClass", "MSTaskListWClass"):
            return True
        num = ctypes.c_int()
        u32.SendMessageW(h, 0x0418, 0, ctypes.byref(num))     # TB_BUTTONCOUNT
        for i in range(max(0, num.value)):
            # TBBUTTON is 32 bytes on x64; iBitmap is the first int
            buf = ctypes.create_string_buffer(64)
            if not u32.SendMessageW(h, 0x0417, i, ctypes.byref(buf)):  # TB_GETBUTTON
                continue
            bitmap = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
            if bitmap < 0:
                continue
            rc = wintypes.RECT()
            u32.SendMessageW(h, 0x0419, i, ctypes.byref(rc))  # TB_GETITEMRECT
            label = ""
            try:
                # the button's window cannot be resolved reliably from TB_GETBUTTON's
                # dwData, so use the tooltip text instead (no extra dependency)
                tip = ctypes.create_unicode_buffer(256)
                u32.SendMessageW(h, 0x0435, i, ctypes.byref(tip))  # TB_GETBUTTONTEXT
                label = tip.value
            except Exception:
                pass
            low = label.lower()
            for key, name in (("msedge", "edge"), ("chrome", "chrome")):
                if name in low and key not in found:
                    found[key] = (r.left + rc.left + (rc.right - rc.left) // 2, y)
        return True

    u32.EnumChildWindows(sb, CB(cb), 0)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--activate", help="substring of the window title to bring forward")
    ap.add_argument("--index", type=int, default=0, help="which match to activate")
    args = ap.parse_args()

    ws = windows()
    if not ws:
        print("no visible browser windows found")
        return 1
    for i, w in enumerate(ws):
        print(f"[{i}] {w['proc']:8} hwnd={w['hwnd']:>8} rect={w['rect']} {w['title'][:80]!r}")

    if args.activate:
        hits = [w for w in ws if args.activate.lower() in w["title"].lower()]
        if not hits:
            print(f"no window title contains {args.activate!r}")
            return 2
        w = hits[min(args.index, len(hits) - 1)]
        ok = activate(w["hwnd"])
        print(f"activated hwnd={w['hwnd']} ok={ok} title={w['title'][:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
