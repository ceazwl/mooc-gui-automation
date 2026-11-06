"""Autonomous course-video runner: play -> wait for quiz -> answer -> next video.

The run is driven by the explicit state machine in runner_states.py; this module
provides the page reading, measurement and clicking it calls, and the entry point
`run()`.

Design rules learned the hard way:
  1. NEVER trust a detector you have not seen fire on a real screenshot. The
     offline suite (test_offline.py) keeps a real page as its fixture for that.
  2. Measure, do not assume. Every coordinate is computed from the live picture or
     the live page; a hardcoded fraction goes stale silently and a click that lands
     nowhere is indistinguishable from a click that was swallowed.
  3. Ask before clicking whether the click can be delivered at all (UIPI/integrity).
     At Low integrity SendInput succeeds and does nothing, and the resulting
     0-pixel diff wasted a whole session.
  4. Verify every action: focus the browser, click, and require a non-zero pixel
     change. Never click a window we do not own.
  5. Write progress to a log file so a long run can be audited without spending
     model tokens, and print only compact lines.

Usage:
  python auto_course.py --dry-run            # detect state, click nothing
  python auto_course.py                      # run until Ctrl-C / --max-cycles
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grab
import keyboard_type
import mouse_click
import ocr_click
import ocr_find
from win_focus import activate, windows

HERE = os.path.dirname(os.path.abspath(__file__))
u32 = ctypes.WinDLL("user32", use_last_error=True)
LOG = os.path.join(HERE, "auto_course.log")
STATE = os.path.join(HERE, "auto_course_state.json")
DEBUG_DIR = os.path.join(HERE, "_dbg", f"run-{os.getpid()}")   # this process only

BROWSER_PROC = "msedge"
NUM_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})$")
# The course's last section, given to me by the operator. The catalog cannot be
# trusted to say this: the scroll search declared "the course is complete" at 8.2
# while 8.3 -- and, after the operator's refresh trick revealed them, 9.1..9.5 and
# beyond -- still existed. So the end is a constant, and "no row after the cursor"
# means the CATALOG READ is incomplete, not that the course is over.
LAST_SECTION = "20.5"


# ---------------------------------------------------------------- logging
def log(msg: str, echo: bool = True) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if echo:
        print(line, flush=True)


# ---------------------------------------------------------------- browser
def browser_window():
    """The Edge window that holds the course page.

    Matched by process name AND by title: a Low-integrity process may not be able
    to read another process's image name, and then the only usable signal left is
    the window title ("... - Microsoft Edge"). DSH's own window is an Electron app
    and must never be mistaken for the browser -- it is the window that steals the
    foreground and gets clicked by accident.
    """
    cands = []
    for w in windows():
        proc = str(w.get("proc", "")).lower()
        title = str(w.get("title", "")).lower()
        if "webview" in proc or "harness" in title:
            continue
        if BROWSER_PROC in proc or "microsoft edge" in title or "chrome" in title:
            cands.append(w)
    return cands[0] if cands else None


def browser_rect():
    """The browser window's rectangle on screen, or None."""
    w = browser_window()
    if not w:
        return None
    r = ctypes.wintypes.RECT()
    if not u32.GetWindowRect(w["hwnd"], ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def focus_browser() -> bool:
    w = browser_window()
    if not w:
        log("ERROR: no Edge window found")
        return False
    hwnd = w["hwnd"]
    # Restore + maximize ALWAYS, not only when the window is behind: a MINIMIZED window
    # can still be reported as the foreground window, and then this function returned
    # True without ever showing the browser -- the screen showed whatever else was up
    # (measured: the DSH window, whose own text contains course-page marker words, so
    # capture_page saw "page=True dsh=True" and refused every retry).
    if u32.IsIconic(hwnd) or not u32.IsWindowVisible(hwnd):
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(0.3)
    u32.ShowWindow(hwnd, 3)              # SW_MAXIMIZE
    # ...and VERIFY it: measured at 06:33, the browser sat in a small floating window
    # while the harness filled the screen, so every capture contained both. SW_MAXIMIZE
    # alone was not enough there, so force the geometry if the window is still small.
    sw, sh = mouse_click.screen_size()
    r = browser_rect()
    if r and (r[2] < int(sw * 0.95) or r[3] < int(sh * 0.90)):
        log(f"  browser window is only {r[2]}x{r[3]} of {sw}x{sh}; forcing it to full screen")
        u32.SetWindowPos(hwnd, 0, 0, 0, sw, sh, 0x0040 | 0x0010)   # SWP_SHOWWINDOW|NOACTIVATE
        time.sleep(0.4)
    if u32.GetForegroundWindow() == hwnd:
        return True
    activate(hwnd)
    time.sleep(0.5)
    ok = u32.GetForegroundWindow() == hwnd
    if not ok:
        log(f"WARN: could not take foreground (fg={u32.GetForegroundWindow()} want={hwnd})")
    return ok


# ---------------------------------------------------------------- OCR state
def read_screen(tag="state"):
    """Capture + OCR only. Deliberately does NOT build a snapshot: snapshot()
    calls this, so doing both here caused unbounded recursion.

    Both artefacts live in _dbg/: a long run rewrites them once per poll, and the
    project root should not accumulate screenshots.
    """
    img = _dbg(f"{tag}.png")
    js = _dbg(f"{tag}.json")
    grab.grab(img)
    d = ocr_click.run_ocr(img, js)
    return d["words"], img


QUESTION_TYPES = ("判断题", "单选题", "多选题", "填空题", "问答题")


def detect_quiz(words) -> tuple[bool, str]:
    """Structural detection -- keyword counting alone already failed once."""
    joined = "".join(w["text"] for w in words)
    why = []
    for t in QUESTION_TYPES:
        if t in joined:
            why.append(t)
    letters = [w for w in words if w["text"].strip() in ("A", "B", "C", "D", "E")]
    if letters:
        why.append("options=" + "".join(sorted({w["text"].strip() for w in letters})))
    if "提交" in joined:
        why.append("提交")
    hit = any(t in joined for t in QUESTION_TYPES) and bool(letters)
    return hit, "; ".join(why) or "no signal"


def browser_tab_title():
    """The active tab's name, straight from the browser window title.

    Far more reliable than OCR-ing the page: the title is exact text, costs
    nothing, and works at any resolution or zoom.
    """
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        w = browser_window()
        if not w:
            return None
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(w["hwnd"], buf, 512)
        title = buf.value
        # "学生学习页面 和另外 3 个页面 - 个人 - Microsoft​ Edge"
        for sep in (" 和另外", " and ", " - 个人", " - Profile"):
            if sep in title:
                title = title.split(sep)[0]
                break
        return title.strip() or None
    except Exception:
        return None


def course_title(words):
    """Section title, best effort.

    NOTE: on this platform the browser tab title is the generic platform name
    ("学生学习页面"), so the section name is not available from the window title.
    The authoritative section identity is `read_chapter_id()`; this function is
    only used for log readability and as the last-resort cursor inference, so it
    must not return a digit from the "1 / 2 作业" tabs as the "title".
    """
    def cjk(t):
        return any("\u4e00" <= c <= "\u9fff" for c in t)

    w, h = mouse_click.screen_size()
    band = [x for x in words
            if SY(200) <= x["y"] <= SY(300) and SX(60) <= x["x"] <= SX(700)]
    if not band:      # layout reflowed (e.g. browser zoom); widen the search
        band = [x for x in words if h * 0.10 <= x["y"] <= h * 0.26 and x["x"] < w * 0.35]
    band = [x for x in band if cjk(x["text"])]
    if not band:
        return None
    mx = max(x["h"] for x in band)
    big = [x for x in band if x["h"] >= mx - 4]
    return "".join(x["text"] for x in big) or None


def taskpoint_done(words) -> bool:
    joined = "".join(w["text"] for w in words)
    if "任务点已完成" in joined:
        return True
    # OCR can split/garble it; require the two anchor tokens close together
    band = [w for w in words if w["y"] < SY(340)]
    for w in band:
        if "已完成" in w["text"]:
            near = [x for x in band if abs(x["y"] - w["y"]) <= 16 and x["x"] < w["x"]]
            if any("任务" in x["text"] or "点" in x["text"] for x in near):
                return True
    return False


# OCR misreads seen in the catalog's NUMBER column. '8' comes back as '&' (both
# glyphs have loops): measured on chapter 8, whose rows arrived as '&' '1' / '&' '2',
# so "8.1"/"8.2" matched no number and the whole chapter was invisible -- the run
# reported "no section after 7.5" with 8.1/8.2 sitting in the catalog, in the OCR
# text, and on screen.
_DIGIT_FIX = {"&": "8", "B": "8", "O": "0", "o": "0", "Q": "0", "D": "0",
              "l": "1", "I": "1", "|": "1", "i": "1",
              "S": "5", "s": "5", "Z": "2", "z": "2", "g": "9", "q": "9",
              "b": "6", "G": "6", "T": "7", "¥": "4", "A": "4"}


def _norm_digits(text: str) -> str:
    """Map OCR's letter/symbol confusions onto the digits they were meant to be."""
    return "".join(_DIGIT_FIX.get(c, c) for c in text.strip())


def _merge_digits(band):
    r"""Normalise the catalog's section numbers into "<chapter>.<item>".

    Windows OCR renders numbers inconsistently for the same page:
        "1.4"  -> '1' '．' '4'      (digit, full-width dot, digit)
        "31"   -> '3' '1'           (two bare adjacent digits, NO dot at all --
                                     section 3.x is drawn without a separator)
        "23"   -> '23'              (already one token)
        "8.1"  -> '&' '1'           (the 8 itself misread)
    Only the first shape used to be handled, so every 3.x row lost its second
    digit and was dropped by the number regex -- section 3.3 disappeared from the
    catalog entirely.

    Chapters 10 and up broke it again, and the operator's help request carried the
    proof: the catalog plainly showed
        "10，1人类相关研究中的道德问题"  (full-width COMMA as the separator)
        "10．4资料收集中的道德问题"       (full-width dot, two-digit chapter)
    and every one of those rows was invisible to a single-digit `\d\.\d` rule. The
    same run showed why the old junk rows "1.0"/"1.1" existed: the chapter HEADER
    "10" was being read as section 1.0.
    """
    dots = {"．", ".", "·", "。", "丶", "，", ",", "、", "｡"}

    def num_len(w):
        """1 or 2 for a number token, else 0 (chapter numbers reach 20 here)."""
        t = _norm_digits(w["text"])
        return len(t) if t.isdigit() and len(t) <= 2 else 0

    def is_digit(w):
        return num_len(w) == 1

    # Split digits are far apart at high resolution and can sit ~1-3px off in y
    # ("1" at 3030,591 and "1" at 3054,591 = 15px gap, which the old 12px ceiling
    # rejected). Scale the tolerances by screen size, but stay well under the row
    # pitch (~80px here) so two different rows can never merge.
    sw, sh = mouse_click.screen_size()
    max_gap = max(12, int(sw * 0.015))
    y_tol = max(8, int(sh * 0.012))

    out = []
    used = set()
    for w in band:
        if id(w) in used:
            continue
        text = w["text"].strip()

        # shape A0: the chapter number is split into TWO digit tokens and the separator
        # sits after them, its baseline ~19px lower:
        #     '1'(3030) '0'(3046) '，'(3062,y+19) '2'(3069)  ->  "10.2"
        # This must run BEFORE shape A, which would otherwise pair '1' with the separator
        # and emit "1.2" while silently dropping the '0'.
        if is_digit(w):
            d2 = next((x for x in band
                       if is_digit(x) and id(x) not in used
                       and abs(x["y"] - w["y"]) <= y_tol
                       and 0 <= x["x"] - (w["x"] + w["w"]) <= max_gap), None)
            if d2:
                sep = next((x for x in band
                            if x["text"].strip() in dots and id(x) not in used
                            and abs(x["y"] - w["y"]) <= y_tol * 3
                            and 0 <= x["x"] - (d2["x"] + d2["w"]) <= max_gap), None)
                if sep:
                    item = next((x for x in band
                                 if is_digit(x) and id(x) not in used
                                 and abs(x["y"] - w["y"]) <= y_tol * 2
                                 and 0 <= x["x"] - (sep["x"] + sep["w"]) <= max_gap), None)
                    if item:
                        used.update({id(d2), id(sep), id(item)})
                        out.append({"text": (f"{_norm_digits(text)}{_norm_digits(d2['text'])}"
                                             f".{_norm_digits(item['text'])}"),
                                    "x": w["x"], "y": min(w["y"], item["y"]),
                                    "w": item["x"] + item["w"] - w["x"],
                                    "h": max(w["h"], item["h"]), "line": ""})
                        continue

        # shape A: '<number><separator><number>' (1-2 digits each side)
        if num_len(w):
            dot = next((x for x in band
                        if x["text"].strip() in dots and id(x) not in used
                        and abs(x["y"] - w["y"]) <= y_tol * 2
                        and 0 <= x["x"] - (w["x"] + w["w"]) <= max_gap), None)
            if dot:
                nxt = next((x for x in band
                            if is_digit(x) and id(x) not in used
                            and abs(x["y"] - w["y"]) <= y_tol * 2
                            and 0 <= x["x"] - (dot["x"] + dot["w"]) <= max_gap), None)
                if nxt:
                    used.update({id(dot), id(nxt)})
                    out.append({"text": f"{_norm_digits(text)}.{_norm_digits(nxt['text'])}",
                                "x": w["x"], "y": min(w["y"], nxt["y"]),
                                "w": nxt["x"] + nxt["w"] - w["x"],
                                "h": max(w["h"], nxt["h"]), "line": ""})
                    continue
            # shape A2: a single token that already contains the separator, e.g. "10，1"
            fixed_any = _norm_digits(text)
            m = re.fullmatch(r"(\d{1,2})[.．·。丶，,、｡](\d{1,2})", fixed_any)
            if m:
                out.append({**w, "text": f"{m.group(1)}.{m.group(2)}"})
                continue

            # shape B: two bare adjacent numbers -> "<chapter>.<item>"
            nxt = next((x for x in band
                        if is_digit(x) and id(x) not in used
                        and abs(x["y"] - w["y"]) <= y_tol
                        and 0 <= x["x"] - (w["x"] + w["w"]) <= max_gap), None)
            if nxt:
                used.add(id(nxt))
                out.append({"text": f"{_norm_digits(text)}.{_norm_digits(nxt['text'])}",
                            "x": w["x"], "y": min(w["y"], nxt["y"]),
                            "w": nxt["x"] + nxt["w"] - w["x"],
                            "h": max(w["h"], nxt["h"]), "line": ""})
                continue

        # shape C: already "<d>.<d>" / "<dd>.<d>" / a bare number token
        fixed = _norm_digits(text)
        if re.fullmatch(r"\d{1,2}\.\d{1,2}", fixed):
            out.append({**w, "text": fixed})
            continue
        if re.fullmatch(r"\d{2}", fixed):
            out.append({**w, "text": f"{fixed[0]}.{fixed[1]}"})
            continue
        if re.fullmatch(r"\d{3}", fixed):
            # 3 digits: the chapter number reached two digits ("101" is 10.1, not 1.01)
            out.append({**w, "text": f"{fixed[:2]}.{fixed[2]}"})
            continue
        out.append(w)
    return out


SCREEN_CALIB_FILE = os.path.join(HERE, "screen_calib.json")
_calib_cache = {"mtime": 0, "data": None}


def screen_calib():
    """Measured layout for the current resolution/zoom, when available."""
    try:
        m = os.path.getmtime(SCREEN_CALIB_FILE)
    except OSError:
        return None
    if _calib_cache["data"] is None or _calib_cache["mtime"] != m:
        try:
            with open(SCREEN_CALIB_FILE, encoding="utf-8") as fh:
                _calib_cache["data"] = json.load(fh)
            _calib_cache["mtime"] = m
        except Exception:
            return None
    return _calib_cache["data"]


def catalog_band():
    """(x_min, y_min) for the catalog pane, from calibration or screen ratios.

    Ratios, not pixels: at a 200% browser zoom the page reflows and the catalog
    moved from x=1434 (baseline) to x=3026 -- no uniform scale factor covers that.
    """
    w, h = mouse_click.screen_size()
    cal = screen_calib() or {}
    cat = cal.get("catalog") or {}
    if cat.get("num_x_left"):
        # start left of the numbers so the titles are included too
        return max(0, int(cat["num_x_left"]) - int(w * 0.075)), int(h * 0.06)
    return int(w * 0.72), int(h * 0.04)


def catalog_num_column(words=None):
    """x range that holds the section numbers, measured from THIS frame when possible.

    Real numbers all start at the same left edge; the bottom-right corner of the
    page also produced number-looking tokens (x~3747) that became phantom rows, and
    the yellow task-point badges are single digits (they do not match "d.d"), so the
    column is the densest x cluster of "d.d" tokens.

    Measuring beats the calibrated constant: the catalog moved from x~3028 to
    x~2827 when the page layout changed, every row was then filtered out, and
    ADVANCE parked reporting "0 rows visible". The calibration is only the fallback.
    """
    w, _h = mouse_click.screen_size()
    if words:
        bx, _by = catalog_band()
        nums = [x for x in words
                if x["x"] >= bx and NUM_RE.match(x["text"].strip())]
        if len(nums) >= 3:
            xs = sorted(n["x"] for n in nums)
            best = []
            for x in xs:
                group = [v for v in xs if abs(v - x) <= 40]
                if len(group) > len(best):
                    best = group
            if len(best) >= 3:
                return min(best) - 5, max(best) + 60
    cat = (screen_calib() or {}).get("catalog") or {}
    if cat.get("num_x_left"):
        left = int(cat["num_x_left"])
        return left - int(w * 0.008), left + int(w * 0.006)
    return int(w * 0.78), int(w * 0.80)


def catalog_rows(words):
    """Rows of the right-hand catalog.

    Each row carries the bounding box of ALL its words (`x0..x1`, `y0..y1`), so a
    click can be aimed at the row's real extent instead of a guessed offset from
    the number token -- guessing left the pointer off the row and the click did
    nothing (44 px of change).
    """
    bx, by = catalog_band()
    sw, _sh = mouse_click.screen_size()
    # Merge FIRST, then measure the number column from this frame: OCR renders the
    # numbers as separate tokens ('6' at x=2827 and '1' at x=2857 on the live page),
    # so raw tokens do not match "d.d" at all, and the calibrated column (x~3028) had
    # gone stale -- every row was filtered out and ADVANCE parked reporting "0 rows".
    pane = [w_ for w_ in words if bx <= w_["x"] <= sw * 0.99 and w_["y"] >= by]
    band = _merge_digits(pane)
    lo, hi = catalog_num_column(band)
    # the title text ends not far right of the numbers; anything further out is
    # other UI (the yellow task-point badges produced phantom rows "1.6"/"0.0")
    title_right = hi + int(sw * 0.115)
    band = [w_ for w_ in band if w_["x"] <= title_right]
    # numbers live in their own column INSIDE the pane (titles extend further
    # right); filtering on the pane's left edge instead of this column silently
    # dropped every row.
    nums = [w_ for w_ in band
            if NUM_RE.match(w_["text"].strip()) and lo <= w_["x"] <= hi]
    out = []
    for n in nums:
        title = [w for w in band
                 if abs(w["y"] - n["y"]) <= 10 and w["x"] > n["x"] + n["w"] and w["text"].strip()]
        title.sort(key=lambda w: w["x"])
        t = "".join(w["text"] for w in title)
        if not t:
            continue
        row = [n] + title
        x0 = min(w["x"] for w in row)
        x1 = max(w["x"] + w["w"] for w in row)
        y0 = min(w["y"] for w in row)
        y1 = max(w["y"] + w["h"] for w in row)
        out.append({"num": n["text"].strip(), "title": t, "y": n["y"],
                    "x": n["x"], "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                    "cy": (y0 + y1) // 2, "cx": (x0 + x1) // 2})
    out.sort(key=lambda r: r["y"])
    return out


def quiz_banner_y(words):
    """y of the 判断题/单选题 banner -- the anchor for finding the option discs."""
    for t in QUESTION_TYPES:
        hits = [w for w in words if w["text"].strip() == t[0] and w["h"] > 8]
        for h in hits:
            # the banner's first char is followed by the rest of the type string
            for nxt in words:
                if abs(nxt["y"] - h["y"]) <= h["h"] and \
                        0 <= nxt["x"] - (h["x"] + h["w"]) <= h["w"] * 1.5:
                    return h["y"] + h["h"] // 2
    return None


def quiz_option_rows(words, img_path=None):
    """Option rows, located by the radio discs (pixels) rather than OCR letters.

    Windows OCR frequently misses the small option letters -- on one quiz screen
    it read only "B", which made the chooser pick a letter that was not on
    screen. The discs are geometric and always visible, so use them for the row
    positions and read the row text by proximity.
    """
    import option_discs

    discs, disc_x = ([], None)
    if img_path and os.path.exists(img_path):
        discs, disc_x = option_discs.find_option_discs(
            img_path, anchor_y=quiz_banner_y(words))
    if not discs:
        return [], None

    # drop banner text that sits above the options
    top = min(discs)
    if len(discs) > 1:
        top = discs[0] - SY(18)

    rows = []
    letters = "ABCDEFGH"
    x_max = (disc_x or SX(200)) + SX(900)     # quiz panel only; never the catalog
    left = (disc_x or SX(200)) - SX(40)
    bounds = []
    for i, cy in enumerate(discs):
        top = cy - SY(34) if i == 0 else (discs[i - 1] + cy) // 2
        bot = (cy + discs[i + 1]) // 2 if i + 1 < len(discs) else cy + SY(34)
        bounds.append((top, bot))
    for i, cy in enumerate(discs):
        top, bot = bounds[i]
        row_words = [w for w in words
                     if top <= (w["y"] + w["h"] // 2) <= bot
                     and left <= w["x"] <= x_max]
        row_words.sort(key=lambda w: w["x"])
        raw = "".join(w["text"] for w in row_words)
        text = re.sub(r"^[A-Ha-h]\s*", "", raw)      # leading option letter
        text = text.lstrip("、，,．.·:：")             # separator
        text = re.sub(r"[0-9．.]+$", "", text).strip()  # trailing catalog digits
        head = re.match(r"^\s*([A-Ha-h])", raw)
        letter = head.group(1).upper() if head else letters[i]
        rows.append({"letter": letter, "cy": cy, "x": disc_x or SX(200), "text": text})
    return rows, disc_x


def quiz_question_text(words, options=None):
    """Question stem: everything between the 判断题 banner and the first option.

    Fractions of the screen only -- the old version used baseline pixels
    (y<520, x<400, x<1400) which match nothing at 3840x2160, so the stem came back
    empty and the model was asked to answer with no question text at all.
    """
    sw, sh = mouse_click.screen_size()
    joined = "".join(w["text"] for w in words)
    kind = next((t for t in QUESTION_TYPES if t in joined), None)
    if not kind:
        return ""

    # the banner is the topmost occurrence of the type word, left of centre
    banner = [w for w in words
              if w["text"].strip()[:1] == kind[0] and w["y"] < sh * 0.55
              and w["x"] < sw * 0.35]
    if not banner:
        return ""
    top = min(w["y"] for w in banner)

    # the stem ends where the first option row begins
    letters = [w for w in words
               if w["text"].strip() in tuple("ABCDE") and w["y"] > top
               and w["x"] < sw * 0.35]
    if options:
        rows = [o["cy"] for o in options if o.get("cy")]
        first = min(rows) if rows else None
    else:
        first = min((w["y"] for w in letters), default=None)
    bottom = (first - sh * 0.012) if first else (top + sh * 0.12)

    band = [w for w in words
            if top + sh * 0.012 <= w["y"] <= bottom and w["x"] < sw * 0.55]
    band.sort(key=lambda w: (round(w["y"] / (sh * 0.02)), w["x"]))
    return "".join(w["text"] for w in band)


# ---------------------------------------------------------------- answering
# Answering is done by the model (llm_answer.py) with answers_known.json as a
# cache. There is deliberately NO rule table here any more: keyword rules were
# measured to produce wrong answers on 判断题 and, because a wrong answer was
# never detected, those answers were never corrected.

def load_known_answers() -> list:
    """The cache of questions whose answers the platform accepted."""
    try:
        import add_answer
        return add_answer._load()
    except Exception:
        return []


def lookup_known(question: str):
    """Match a question against the cache by a distinctive leading fragment.

    OCR mangles long stems, so compare the first ~14 normalised characters rather
    than the whole string.
    """
    q = re.sub(r"\s+", "", question or "")
    if len(q) < 6:
        return None, ""
    for entry in load_known_answers():
        key = re.sub(r"\s+", "", entry.get("match", ""))
        if len(key) < 6:
            continue
        probe = key[:14]
        if probe and (probe in q or q[:14] in key):
            return entry.get("answer"), str(entry.get("note", "cached"))
    return None, ""


def option_texts(words, options):
    """Map each option letter to the text rendered on its row (punctuation trimmed)."""
    out = {}
    for opt in options:
        row = [w for w in words
               if abs(w["y"] + w["h"] // 2 - opt["cy"]) <= 16
               and w["x"] > opt["x"] + 10 and w["x"] < 1400]
        row.sort(key=lambda w: w["x"])
        text = "".join(w["text"] for w in row)
        # the separator ("、" / "，") is its own OCR box; drop it for matching
        out[opt["letter"]] = text.lstrip("、，,．.·:：")
    return out

def choose_answer(kind: str, question: str, options: list[dict],
                  reject: tuple = ()) -> tuple[str | None, str]:
    """Pick an option letter using the model, with the curated table as a cache.

    NO rule-based answering. The operator's instruction is explicit: never answer
    from heuristics. The earlier keyword rules (an absolutist-phrase table plus a
    longest-option guess) produced wrong answers -- a 判断题 that read
    "科研选题要避免...病态科学研究" was answered 错 by rule when it is 对 -- and,
    worse, a wrong answer was never noticed, so nothing ever corrected it.

    Layers:
      1. answers_known.json -- cache of questions already ANSWERED CORRECTLY
      2. DeepSeek API       -- the decision layer
    If both fail the caller must hand over to the help channel; never guess.

    `reject` holds letters the platform has already refused for this question, so a
    retry cannot come back with the same wrong answer (the model is told what was
    rejected, and a repeat is asked again once, then refused rather than submitted).
    """
    letters = {o["letter"] for o in options}
    rejects = [r for r in reject if r]

    known, why = lookup_known(question)
    if known and known not in rejects:
        if known in letters:
            return known, f"cache: {why}"
        return None, f"cached answer {known!r} not among on-screen options {sorted(letters)}"

    def ask(extra_reject: list) -> tuple[str | None, str]:
        try:
            import llm_answer
            res = llm_answer.llm_answer(question, options, kind=kind, reject=extra_reject)
        except Exception as e:
            res = {"answer": None, "error": f"{type(e).__name__}: {e}"}
        if res and res.get("answer"):
            note = (f"llm({res.get('model')}) conf={res.get('confidence')} "
                    f"why={res.get('reason')}")
            return res["answer"], note
        err = (res or {}).get("error") or "no usable answer"
        return None, f"model could not answer ({err}); refusing to guess by rule"

    pick, note = ask(rejects)
    if pick and pick in rejects:
        log(f"  model returned the rejected {pick}; asking again, ruling it out")
        pick, note = ask(rejects + [pick])
    if pick and pick in rejects:
        return None, (f"the model insists on {pick}, which the platform already "
                      f"rejected ({rejects}); refusing to submit it again")
    return pick, note


# ---------------------------------------------------------------- actions
def verified_click(x, y, label, wait=1.5, region=None, min_change=150):
    """Click and require the screen to actually change."""
    if not focus_browser():
        log(f"ABORT {label}: browser not foreground, refusing to click")
        return False, 0
    injectable, why = injectable_at(x, y)
    if not injectable:
        # without this the click is silently discarded and the 0-pixel diff looks
        # like a mis-aimed click, which is how a whole session got spent guessing
        log(f"ABORT {label}: the synthetic click would be dropped - {why}")
        return False, 0
    before = _dbg(f"{label}_b.png")
    after = _dbg(f"{label}_a.png")
    grab.grab(before, region)
    mouse_click.click(x, y)
    time.sleep(wait)
    grab.grab(after, region)
    d = mouse_click.pixel_diff(before, after)
    changed = d.get("changed_pixels", 0) if d.get("same_size") else 0
    ok = changed >= min_change
    log(f"{'OK  ' if ok else 'FAIL'} {label}: click=({x},{y}) changed={changed}px bbox={d.get('diff_bbox')}")
    return ok, changed


URL_BAND = (100, 70, 1400, 40)


def url_band(left_only=True):
    """Address-bar capture box, as a fraction of the screen.

    The baseline constant (100,70,1400,40) scaled uniformly returns ZERO words at
    3840x2160 -- the bar is at ~5-9% of the height there, not 5.3-8.6%. Fractions
    plus a full-width bar read it reliably.

    `left_only` cuts the bar at 55% width: OCR of the FULL bar is lossy on a long
    URL (measured: the whole "ng.com/mycourse/studentstudy?chapterId" chunk was
    dropped, so the section id vanished and a click that HAD navigated looked like a
    failure), while the left half reads cleanly. The chapterId sits early in the URL.
    """
    w, h = mouse_click.screen_size()
    width = int(w * 0.55) if left_only else w
    return (0, int(h * 0.048), width, int(h * 0.042))


def parse_chapter_id(text):
    """Pull the section id out of OCR'd URL text.

    Two shapes, because OCR of a long URL is lossy:
      * "chapterld=1234567890"          -- token survived (OCR renders I as l/1/|)
      * "...chaoxi=1234567891&courseld=" -- the token was dropped; the id is then the
        first long number before the courseId parameter.
    """
    m = re.search(r"chapter[Iil1|]d[^0-9]{0,6}(\d{5,})", text, re.I)
    if m:
        return m.group(1)
    head = re.split(r"course\s*[Iil1|]?d", text, flags=re.I)[0]
    nums = re.findall(r"(\d{8,12})", head)
    return nums[0] if nums else None


def read_chapter_id():
    """Extract chapterId from the address bar -- the authoritative section id.

    Page titles proved unusable for verifying navigation: a stale OCR title can
    coincidentally equal the section we are heading to, which made a successful
    click look like a failure (run 5 stopped on 3.4 for exactly that reason).
    The URL carries chapterId, and OCR renders it as "chapterld"/"chapter1d".

    Tries the left half of the bar first (much better OCR), then the full bar, and
    parses both tolerantly -- a navigation that happened must never be reported as
    a failure because a token was lost.
    """
    for left_only in (True, False):
        p = _dbg("url_left.png" if left_only else "url.png")
        grab.grab(p, url_band(left_only))
        d = ocr_click.run_ocr(p, _dbg("url_left.json" if left_only else "url.json"))
        joined = "".join(w["text"] for w in d["words"])
        remember_url(joined)
        cid = parse_chapter_id(joined)
        if cid:
            return cid
    return None


CHAPTER_IDS_FILE = os.path.join(HERE, "chapter_ids.json")
LAST_URL_FILE = os.path.join(HERE, "last_url.txt")


def remember_url(text: str) -> None:
    """Keep the last course URL we read, so the browser can be reopened on it.

    Measured need: the operator closed the browser mid-run and the runner could only
    park ("could not capture the course page"). The URL carries the course/class/cpi/enc
    parameters, so saving it means a dead browser can be relaunched on the right page.

    It must be VALIDATED before saving: the caller hands over OCR text, and OCR turns
    "mooc1" into "moocl", "chapterId" into "chapterld", and "=" into a full-width "：".
    Saving that garbage overwrote a good URL and made every later navigation fail
    (measured: course_url() then returned None for 79 sections in a row).
    """
    if "chapter" not in text.lower() or "http" not in text.lower():
        return
    m = re.search(r"https?://\S+", text)
    if not m:
        return
    clean = _sanitize_url(m.group(0))
    if not clean:
        return                      # never replace a good URL with an OCR-garbled one
    try:
        with open(LAST_URL_FILE, "w", encoding="utf-8") as fh:
            fh.write(clean)
    except OSError:
        pass


def read_url_from_clipboard():
    """The exact address-bar URL, via Ctrl+L / Ctrl+C -- no OCR involved.

    OCR of the address bar is lossy in exactly the characters that matter
    ("chapterId" -> "chapterld"), and the clipboard gives the byte-exact string. It also
    verifies which section the page is really on.
    """
    import subprocess

    if not focus_browser():
        return None
    try:
        import keyboard_type
        keyboard_type.chord("ctrl", "l")
        time.sleep(0.4)
        keyboard_type.chord("ctrl", "c")
        time.sleep(0.4)
        keyboard_type.key("esc")
    except Exception as e:
        log(f"  clipboard read failed: {type(e).__name__}: {e}")
        return None
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                       capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip() or None


def _sanitize_url(url: str) -> str:
    """Trim what OCR glued onto the URL (e.g. a stray '銆' mojibake) and validate it.

    Measured 12:00: the remembered URL was cut mid-parameter ("&enc=d3c75d銆") because it
    came from OCR of the address bar. `start msedge <broken url>` produced no window at
    all, so the relaunch failed exactly when it was needed.
    """
    m = re.match(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", url or "")
    if not m:
        return ""
    clean = m.group(0)
    if not re.search(r"chapter[Iil1]d=\d{4,}", clean, re.I):
        return ""
    if not re.search(r"enc=[0-9a-fA-F]{16,}", clean):
        return ""                      # a truncated enc cannot load the page
    return clean


def course_url(num: str | None = None):
    """The course URL for section `num`, from the remembered URL + the learned ids."""
    try:
        with open(LAST_URL_FILE, encoding="utf-8") as fh:
            url = fh.read().strip()
    except OSError:
        return None
    url = _sanitize_url(url)
    if not url:
        return None
    cid = load_chapter_ids().get(num) if num else None
    if cid:
        url = re.sub(r"(chapter[Iil1]d=)\d+", r"\g<1>" + cid, url, flags=re.I)
    return url


def relaunch_browser(num: str | None = None, wait=20.0) -> bool:
    """Start Edge again when no browser window exists.

    Two attempts, best first:
      1. `msedge --restore-last-session` -- brings back the operator's own previous tabs,
         including the logged-in course page, so no URL has to be rebuilt from OCR;
      2. `msedge <course url>` -- only when the remembered URL is provably complete
         (see _sanitize_url).

    Only fires when there is genuinely no browser window, so it can never open a second
    one behind a live session.
    """
    import subprocess

    if browser_window():
        return False

    def launch(args, label):
        log(f"  no browser window; relaunching Edge ({label})")
        try:
            subprocess.Popen(["cmd", "/c", "start", ""] + args, cwd=HERE, close_fds=True)
        except Exception as e:
            log(f"  relaunch failed ({label}): {type(e).__name__}: {e}")
            return False
        deadline = time.time() + wait
        while time.time() < deadline:
            if browser_window() is not None:
                time.sleep(2.0)          # let the tab start loading
                log(f"  browser relaunch succeeded ({label})")
                return True
            time.sleep(1.0)
        log(f"  browser relaunch ({label}) did not produce a window within {wait:.0f}s")
        return False

    if launch(["msedge", "--restore-last-session"], "restore the previous session"):
        return True
    url = course_url(num)
    if url:
        return launch(["msedge", url], f"the course URL {url[:70]}...")
    log("  cannot relaunch the browser: no complete remembered course URL")
    return False


def load_chapter_ids() -> dict:
    if os.path.exists(CHAPTER_IDS_FILE):
        try:
            return json.load(open(CHAPTER_IDS_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def remember_chapter_id(num: str, cid: str):
    if not num or not cid:
        return
    ids = load_chapter_ids()
    if ids.get(num) != cid:
        ids[num] = cid
        with open(CHAPTER_IDS_FILE, "w", encoding="utf-8") as fh:
            json.dump(ids, fh, ensure_ascii=False, indent=1, sort_keys=True)
        log(f"  learned chapterId for {num}: {cid}")


HELP_DIR = os.path.join(HERE, "auto_help")
WAITING_FLAG = os.path.join(HELP_DIR, "waiting")


def ask_for_help(reason, st=None, args=None, detail="", question="", options=None):
    """Park on a blocker and hand control to the agent/human.

    This is the general escape hatch: whenever the mechanical rules cannot
    explain what is on screen, the runner stops guessing, writes a request with
    the screenshot, and waits for a decision.

    It also maintains auto_help/waiting for as long as it blocks, so the
    supervisor can tell "waiting for help" apart from "deadlocked" -- without
    that flag the supervisor's stall watchdog killed the very process that was
    correctly waiting for a decision, which is what made one run spin forever.
    """
    import help_channel

    img = (st or {}).get("img")
    words = (st or {}).get("words") or []
    text = "".join(w["text"] for w in words)
    rid = help_channel.request(
        reason=reason,
        detail=detail,
        screenshot=img,
        ocr_text=text,
        question=question or (st or {}).get("question", ""),
        options=(st or {}).get("options") or (options or []),
        title=(st or {}).get("title"),
        chapter_id=read_chapter_id(),
    )
    log(f"HELP REQUEST #{rid}: {reason} -- {detail}")
    log(f"  screenshot: {img}")
    log(f"  waiting for a decision (python control.py reply ...)")

    def mark_waiting(secs):
        try:
            with open(WAITING_FLAG, "w", encoding="utf-8") as fh:
                fh.write(f"{rid} {secs}\n")
        except OSError:
            pass
        if secs and secs % 120 == 0:
            log(f"  still waiting for help ({secs}s)")

    mark_waiting(0)

    def on_wait(secs):
        mark_waiting(secs)

    try:
        rep = help_channel.wait_for_reply(
            rid, timeout=args.help_timeout if args else 1800, on_wait=on_wait)
    finally:
        try:
            os.remove(WAITING_FLAG)
        except OSError:
            pass
    if not rep:
        log("  no reply in time; aborting the run so nothing is guessed")
        return {"action": "abort"}
    log(f"  reply received: {rep}")
    help_channel.clear()
    return rep


def apply_help_reply(rep, st=None):
    """Carry out the decision we were given. Returns True if the run may continue."""
    action = (rep or {}).get("action", "abort")
    if action == "abort":
        return False
    if action == "skip":
        log("  decision: skip this blocker and continue")
        return True
    if action == "click":
        x, y = int(rep["x"]), int(rep["y"])
        log(f"  decision: click ({x},{y})")
        focus_browser()
        mouse_click.click(x, y)
        time.sleep(1.5)
        return True
    if action == "keys":
        import keyboard_type
        keys = str(rep.get("keys", "")).strip()
        log(f"  decision: send keys {keys!r}")
        focus_browser()
        if "+" in keys:
            keyboard_type.chord(*[k.strip() for k in keys.split("+")])
        else:
            keyboard_type.key(keys)
        time.sleep(1.5)
        return True
    if action == "scroll":
        n = int(rep.get("notches", -3))
        x = int(rep.get("x", 1750))
        y = int(rep.get("y", 700))
        log(f"  decision: scroll {n} notches at ({x},{y})")
        focus_browser()
        mouse_click.scroll(n, x, y)
        time.sleep(1.0)
        return True
    if action == "click_text":
        want = str(rep.get("text", ""))
        log(f"  decision: find {want!r} on screen and click it")
        focus_browser()
        words, _img = read_screen("help_click")
        hits = ocr_find.find_phrase(words, want, fuzzy=0.7)
        if not hits:
            log(f"  {want!r} not found; cannot carry out the decision")
            return False
        mouse_click.click(hits[0].cx, hits[0].cy)
        time.sleep(1.5)
        return True
    if action == "answer":
        letter = str(rep.get("letter", "")).upper()
        log(f"  decision: answer {letter}")
        import answer_now  # reuse the verified option+submit path
        sys.argv = ["answer_now.py", "--letter", letter]
        rc = answer_now.main()
        return rc == 0
    log(f"  unknown action {action!r}; aborting")
    return False


def autocalibrate(force=False, max_age=86400):
    """Measure the current layout and cache it in screen_calib.json.

    Runs automatically when the cache is missing, stale, or the screen size
    changed -- otherwise any resolution or browser-zoom change silently breaks
    every region and the run starts reading the wrong part of the screen.
    """
    import calibrate as cal_mod

    w, h = mouse_click.screen_size()
    cal = screen_calib()
    if cal and not force:
        # a cached calibration WITHOUT a measured player is not good enough: the
        # player box is what every play click is aimed with, so retry it
        if cal.get("screen") == [w, h] and cal.get("player") and \
                (time.time() - _calib_cache.get("mtime", 0)) < max_age:
            return cal

    log(f"calibrating layout for {w}x{h} ...")
    try:
        img = os.path.join(HERE, "_auto_calib.png")
        words, img = capture_page("calib", tries=3)
        cat = cal_mod.find_catalog(words, w, h)
        player = None
        for attempt in range(3):
            player = cal_mod.find_player(img, w, h)
            if player:
                break
            # a white slide hides the player from the scan (that is exactly the 5.3
            # case); a fresh frame usually shows picture content again
            time.sleep(2.5)
            try:
                words, img = capture_page("calib", tries=2)
            except Exception:
                break
        data = {"screen": [w, h],
                "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "catalog": cat, "player": player}
        # NOTE: no play_button / quiz_region / submit_button here any more. Each was
        # derived from the player box, and each was then preferred over the live
        # measurement -- so a stale value silently reintroduced a wrong coordinate.
        # The player box itself is measured from the picture for every play click.
        with open(SCREEN_CALIB_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        _calib_cache["data"] = data
        _calib_cache["mtime"] = time.time()
        log(f"  calibrated: catalog={'ok' if cat else 'MISS'} "
            f"player={'ok' if player else 'MISS'} ({player})")
        return data
    except Exception as e:
        log(f"  calibration failed: {type(e).__name__}: {e}")
        return cal


TIME_TOKEN = re.compile(r"\d{1,2}[:：]\d{2}")


def find_timecode(words, within=None):
    """The player's "M:SS / M:SS" label, found by OCR anywhere on screen.

    This replaces player-box detection entirely: the play/pause button always sits
    just left of the timecode, so locating the text is enough and needs no
    resolution- or zoom-dependent geometry.

    OCR returns the whole label as ONE token ("8:58 / 14:00") at some zooms and as
    separate ones at others, so match on a substring and pick the widest token
    that looks like a timecode.

    `within` = the player box: the search MUST be limited to it. Without that the
    Windows taskbar CLOCK ("23:20", x=3722) is a perfect timecode match, and the
    play button derived from it lands in the taskbar -- which is how a run can end up
    clicking the system tray.

    When the label is split into numeric PIECES with no colon inside any single
    token (measured: '8' '：' '38' '/' '11' '．' '1' on one row), no token matches and
    the timecode looked absent -- which meant a PAUSED video with its control bar up
    was reported as playing, and the run polled it for 25 minutes instead of
    pressing play. So rebuild the label from the numeric pieces of one visual row.
    """
    def inside(w):
        if within is None:
            return True
        x, y, ww, hh = within
        cx = w["x"] + w["w"] / 2
        cy = w["y"] + w["h"] / 2
        return (x - 40 <= cx <= x + ww + 40) and (y - 40 <= cy <= y + hh + 40)

    cands = [w for w in words
             if TIME_TOKEN.search(w["text"]) and w["h"] > 8 and inside(w)]
    if cands:
        # the label is the widest such token; the "倍速" control and other numerals
        # are smaller or lack the colon pattern
        cands.sort(key=lambda w: -w["w"])
        return cands[0]

    numeric = [w for w in words
               if re.fullmatch(r"[\d:：/．.、,，]{1,8}", w["text"].strip())
               and w["h"] > 8 and inside(w)]
    numeric.sort(key=lambda w: w["y"] + w["h"] / 2)
    rows = []
    for w in numeric:
        centre = w["y"] + w["h"] / 2
        if rows and abs(centre - rows[-1][0]) <= 14:      # the ':' sits ~7px lower
            rows[-1][1].append(w)
        else:
            rows.append((centre, [w]))
    for _centre, row in rows:
        if len(row) < 3:
            continue
        row.sort(key=lambda w: w["x"])
        # keep only the CONTIGUOUS run from the left: a stray numeral elsewhere on the
        # same visual row (a badge, a comment count) must not join the label and drag
        # its left edge -- and with it the play button's position -- off the player
        run = [row[0]]
        for w in row[1:]:
            if w["x"] - (run[-1]["x"] + run[-1]["w"]) <= 40:
                run.append(w)
            else:
                break
        if len(run) < 3:
            continue
        text = "".join(w["text"] for w in run)
        if not TIME_TOKEN.search(text):
            continue
        x0 = min(w["x"] for w in run)
        y0 = min(w["y"] for w in run)
        x1 = max(w["x"] + w["w"] for w in run)
        y1 = max(w["y"] + w["h"] for w in run)
        log(f"  timecode rebuilt from pieces: {text!r} at x={x0}")
        return {"text": text, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
    return None


def find_play_button(words, within=None):
    """Play/pause button = a point just left of the timecode, same row."""
    tc = find_timecode(words, within=within)
    if not tc:
        return None
    w, _h = mouse_click.screen_size()
    offset = int(w * 0.030)
    return (max(0, tc["x"] - offset), tc["y"] + tc["h"] // 2)


def find_submit_button(words):
    """The quiz's 提交 button, located by its text."""
    hits = ocr_find.find_phrase(words, "提交", fuzzy=0.7)
    if hits:
        return hits[0].cx, hits[0].cy
    return None


def play_button_point(words=None, img=None):
    """Where to click to start playback: measured, never assumed.

    Order:
      1. the centre of the MEASURED video box -- this player draws its big poster
         play button there, which is the common case (a freshly loaded section);
      2. just left of the timecode -- the control bar's button, when the video was
         paused mid-way and the bar happens to be up.
    There is deliberately NO hardcoded screen fraction: the old (0.231W, 0.389H)
    pointed at the lecturer's face, and a blind fallback click into the video body
    toggles playback OFF. Returning None makes the caller hand over with a
    screenshot, which is recoverable; a wrong click is not.
    """
    if img:
        pt = poster_play_point(img)
        if pt:
            return pt, "centre of the measured video box"
    if words:
        pt = find_play_button(words, within=img and video_rect(img) or None)
        if pt:
            return pt, "left of the timecode (control bar)"
    return None, "no play button found (no video box measured, no timecode)"


def submit_point(words=None):
    """Submit button for the quiz overlay.

    Measured by the operator at 3840x2160: (2624,1972) == (0.683W, 0.913H), and
    verified to actually submit. OCR and colour detection both failed here (the
    label is small white text on a dim layer, and the blue-pill colour test hit
    other page elements), so the fraction is the reliable source.

    NOTE: a stale `submit_button` in screen_calib.json used to win over this and
    silently reintroduced the old, wrong coordinate -- calibration no longer
    supplies it.
    """
    if words:
        pt = find_submit_button(words)
        if pt:
            return pt
    w, h = mouse_click.screen_size()
    return int(w * 0.683), int(h * 0.913)


# markers that only the course page has
PAGE_MARKERS = ("章节详情", "返回课程", "任务点", "写笔记", "倍速", "目录")
# markers that mean we captured DSH itself, which must never be acted on
DSH_MARKERS = ("新会话", "插件", "工作区", "发送消息或创建任务", "标准模式", "后台任务")


class CaptureError(RuntimeError):
    """Raised when the browser page could not be captured -- never act on a guess."""


# labels that identify the course tab in the tab strip
COURSE_TAB_HINTS = ("学生学习", "学生学情", "学习页面")


def course_tab_active() -> bool:
    """Is the ACTIVE tab the course page? (window title read, costs nothing)"""
    t = browser_tab_title() or ""
    return any(h in t for h in ("学习", "学生"))


def focus_course_tab(tries=2) -> bool:
    """Bring the Edge window forward AND make the course tab the active one.

    focus_browser() only raises the window. If another tab is active -- the operator
    opened a top-up page, say -- every capture then reads the wrong page and the
    runner dies with "could not capture the course page" while the browser looks
    perfectly healthy on screen. The tab strip is at the top of the window and its
    labels are OCR-readable, so click the course tab.
    """
    if not focus_browser():
        return False
    if course_tab_active():
        return True
    for _ in range(max(1, tries)):
        words, _img = read_screen("tabs")
        band = [w for w in words if w["y"] < SY(70)]
        hit = None
        for hint in COURSE_TAB_HINTS:
            hits = ocr_find.find_phrase(band, hint, fuzzy=0.6)
            if hits:
                hit = hits[0]
                break
        if not hit:
            log("  no course tab found in the tab strip; is the course still open?")
            return False
        log(f"  switching to the course tab at ({hit.cx},{hit.cy})")
        mouse_click.click(hit.cx, hit.cy)
        time.sleep(2.5)
        if course_tab_active():
            return True
    return course_tab_active()


def scroll_page_to_top():
    """Put the course page back at the top.

    A wheel event aimed at the catalog pane scrolls the whole PAGE when the catalog
    itself has nothing to scroll. Measured: after a failed 6.2 search the page ended
    up scrolled so far that the quiz's 判断题 banner was above the viewport -- the
    quiz was then not recognised at all and the run polled a finished section.
    Home is handled by the document itself and does not depend on where the pointer
    happens to be, unlike a wheel event.
    """
    import keyboard_type

    if not focus_browser():
        return False
    try:
        keyboard_type.key("home")
        time.sleep(0.5)
        keyboard_type.key("home")
        time.sleep(0.6)
    except Exception as e:
        log(f"  could not send Home to the page: {type(e).__name__}: {e}")
        return False
    return True


def page_looks_scrolled(words) -> bool:
    """A 提交 button with no 题型 banner means the quiz is cut off above the fold."""
    joined = "".join(w["text"] for w in words)
    return "提交" in joined and quiz_banner_y(words) is None


# The operator measured the browser's reload button at (117,120) on 3840x2160; used
# only if the keyboard shortcut is unavailable, because Ctrl+R cannot land on the
# wrong window.
RELOAD_BUTTON = (0.0305, 0.0556)


def refresh_page(wait=9.0):
    """Reload the course page. Primary: Ctrl+R (a keyboard event).

    The operator measured two things this buys us:
      * a reload re-anchors the catalog to the current section -- measured: on 8.3 the
        visible rows jumped to 8.2/8.3/8.4/8.5/9.1..9.5, revealing sections that the
        scroll search never reached (which is why the run wrongly declared "the course
        is complete" at 8.2);
      * it clears the "clicked play and nothing happens" block: measured on a video
        stuck at 6:12, where the centre play button was detected (+56) and clicking it
        still did nothing.
    """
    import keyboard_type

    if not focus_browser():
        return False
    try:
        keyboard_type.chord("ctrl", "r")
        log("  refreshed the page with Ctrl+R")
    except Exception as e:
        log(f"  Ctrl+R unavailable ({type(e).__name__}: {e}); clicking the reload button")
        w, h = mouse_click.screen_size()
        mouse_click.click(int(w * RELOAD_BUTTON[0]), int(h * RELOAD_BUTTON[1]))
    time.sleep(wait)
    return True


TAB_REF_DIR = os.path.join(HERE, "testdata")
TAB_SLOT = (0.0104, 0.008, 0.0151, 0.025)     # dx after the favicon, y, w, h (screen fractions)


def tab_icon_candidates(im, n=14):
    """Up to `n` well-separated high-contrast patches in the tab strip (x positions).

    Two failed anchors before this one, both measured:
      * the reddest patch was a DIFFERENT tab's icon (x=768);
      * then the browser's title bar itself measured red everywhere, because Edge tints
        the title bar to the page's theme colour -- so "redness" found a flat tint, not
        an icon, and returned the same patch at every offset.
    What actually identifies an icon (favicon or speaker) is CONTRAST: a glyph on a flat
    bar. So score by the patch's luminance spread -- and scan the WHOLE strip: the course
    tab is not at a fixed place. Measured: with the other tabs closed it is the first tab
    (x≈150), and an x-range starting at 0.20W found no candidates at all.
    """
    from PIL import ImageStat

    W, H = im.size
    step = max(2, int(W * 0.001))
    scored = []
    for x in range(int(W * 0.005), int(W * 0.70), step):
        c = im.crop((x, int(H * 0.008), x + max(8, int(W * 0.0104)),
                     int(H * 0.030))).convert("L")
        scored.append((ImageStat.Stat(c).stddev[0], x))
    scored.sort(reverse=True)
    out = []
    for spread, x in scored:
        if spread < 25:
            break
        if all(abs(x - k) > 80 for k in out):
            out.append(x)
        if len(out) >= n:
            break
    return out


def tab_favicon_box(im):
    """Bounding box of the 超星 favicon in the tab strip, or None.

    Located by its STRONG RED pixels rather than by average redness: Edge tints the whole
    title bar to the page's theme colour, so a pale pink bar can be "redder" on average
    than the icon. The book icon is saturated (r-g large), the tint is not.

    Returns (x1_right_edge, y0, y1) -- the slot that matters is the one just RIGHT of the
    icon, and using the right edge keeps it correct whatever the icon's width is (it is
    40px in one layout and 70px in another).
    """
    W, H = im.size
    y0, y1 = int(H * 0.006), int(H * 0.034)
    px = im.convert("RGB").load()
    cols = []
    for x in range(int(W * 0.005), int(W * 0.70)):
        n = 0
        for y in range(y0, min(y1, H)):
            r, g, b = px[x, y]
            if r > 170 and r - g > 55 and r - b > 25:
                n += 1
        cols.append((x, n))
    runs = []
    start = None
    for x, n in cols:
        if n >= 2 and start is None:
            start = x
        elif n < 2 and start is not None:
            runs.append((start, x - 1))
            start = None
    if start is not None:
        runs.append((start, cols[-1][0]))
    runs = [r for r in runs if r[1] - r[0] >= 12]          # an icon, not a stray pixel
    if not runs:
        return None
    # The strong-red test catches only the icon's saturated core (its gradient fades out):
    # measured, the core is x 96..127 while the whole book icon ends at ~145. Walking right
    # "while there is ink" does not work -- the pink title bar itself counts as ink and the
    # walk ran to the end of the scan range. So use the measured width of this icon from
    # its core's left edge (49px at 3840 wide, i.e. a fraction of the window).
    x0, _x1 = max(runs, key=lambda r: r[1] - r[0])
    return x0 + int(W * 0.0128), y0, y1


def tab_favicon_boxes(im):
    """Every candidate tab-icon slot in the strip (one per 超星 tab).

    Measured 02:47: with TWO course tabs open, the single-box version anchored on the
    wrong tab (x=623) and its slot matched neither reference ("weak idle match d=24"), so
    the audio signal went dark exactly when it was needed -- and the run fell back to
    "a timecode is visible, so it must be paused", which pauses a playing video.
    """
    W, H = im.size
    y0, y1 = int(H * 0.006), int(H * 0.034)
    px = im.convert("RGB").load()
    runs = []
    start = None
    for x in range(int(W * 0.005), int(W * 0.70)):
        n = 0
        for y in range(y0, min(y1, H)):
            r, g, b = px[x, y]
            if r > 170 and r - g > 55 and r - b > 25:
                n += 1
        if n >= 2 and start is None:
            start = x
        elif n < 2 and start is not None:
            runs.append((start, x - 1))
            start = None
    if start is not None:
        runs.append((start, int(W * 0.70) - 1))
    runs = [r for r in runs if r[1] - r[0] >= 12]
    return [(x0 + int(W * 0.0128), y0, y1)
            for x0, _x1 in sorted(runs, key=lambda r: -(r[1] - r[0]))]


def tab_icon_slot(im, x_right, y0=None, y1=None):
    """The icon slot just right of a tab icon whose right edge is `x_right`."""
    W, H = im.size
    _dx, _yf, wf, hf = TAB_SLOT
    yy0 = int(H * 0.006) if y0 is None else y0
    yy1 = int(H * 0.034) if y1 is None else y1
    x0 = x_right + 2
    return im.convert("L").crop((x0, yy0, x0 + int(W * wf), yy0 + (yy1 - yy0)))


def classify_tab_slot(slot):
    """Compare a tab icon slot against the two measured references.

    idle  -> the slot holds the title's first characters ('学生')
    playing -> it holds the audio indicator (a speaker with sound waves)
    Returns (True | False | None, detail). Text darkness cannot decide this: measured
    dark fraction is LOWER while playing (0.079) than idle (0.098), and the speaker is
    an outline, not a solid cone (zero solid-interior pixels in both states).
    """
    from PIL import Image, ImageChops, ImageStat

    refs = {}
    for name, flag in (("tab_idle.png", False), ("tab_playing.png", True)):
        path = os.path.join(TAB_REF_DIR, name)
        if os.path.exists(path):
            refs[path] = (flag, Image.open(path).convert("L"))
    if len(refs) < 2:
        return None, "tab reference fixtures missing"
    if slot is None:
        return None, "no slot"
    slot = slot.resize(next(iter(refs.values()))[1].size)
    scores = []
    for path, (flag, ref) in refs.items():
        scores.append((ImageStat.Stat(ImageChops.difference(slot, ref)).mean[0], flag, path))
    scores.sort()
    best, second = scores[0], scores[1]
    detail = (f"best {'playing' if best[1] else 'idle'} d={best[0]:.0f}, "
              f"other d={second[0]:.0f}")
    # the two references are 30 apart; measured noise on a real slot is 0~19 (the tab
    # re-renders while focus changes), and a wrong window measured 186. So: accept a
    # clear winner, and answer "unknown" rather than guessing in between.
    if best[0] > 24 or (second[0] - best[0]) < 8:
        return None, f"tab icon does not match either reference ({detail})"
    return best[1], detail


def shot_is_browser(img_path) -> bool:
    """Does this screenshot really show the browser? (address bar readable)

    The tab strip signal is worthless if the shot is of another window -- and that
    happens in practice: the harness takes the foreground while a command runs, so the
    foreground check can pass and the grab still catches the wrong window. Measured: a
    slot from the wrong window sat 186 away from both references, and the strip came
    back with no high-contrast patches at all.
    """
    from PIL import Image

    w, h = mouse_click.screen_size()
    p = _dbg("urlcheck.png")
    Image.open(img_path).crop((0, int(h * 0.045), int(w * 0.6), int(h * 0.095))).save(p)
    d = ocr_click.run_ocr(p, _dbg("urlcheck.json"))
    joined = "".join(x["text"] for x in d["words"]).lower()
    return any(k in joined for k in ("http", "chaoxing", "mooc", "study"))


def audio_playing(shot=None):
    """Is the course tab producing sound (i.e. is the video playing)?

    The operator's rule: the speaker indicator appears on the tab exactly while the
    video plays. Measured on this machine: idle slot = '学生', playing slot = speaker.
    Returns True / False / None -- None whenever it cannot be certain, so the caller
    falls back to the pixel checks instead of acting on a guess.
    """
    from PIL import Image

    w = browser_window()
    if not w or u32.GetForegroundWindow() != w["hwnd"]:
        return None, "another window is in the foreground; the tab strip is not readable"
    p = screen_shot(shot or "tabicon")
    if not shot_is_browser(p):
        return None, "the screenshot is not the browser (no address bar); ignoring it"
    im = Image.open(p)
    boxes = tab_favicon_boxes(im)
    if not boxes:
        return None, "no tab favicon found in the tab strip (strong-red scan)"
    idle_weak = []
    for x_right, y0, y1 in boxes:
        slot = tab_icon_slot(im, x_right, y0, y1)
        playing, detail = classify_tab_slot(slot)
        if playing is True:
            return True, f"tab icon right edge x={x_right}; {detail}"
        if playing is False:
            try:
                d = float(detail.split("d=")[1].split(",")[0])
            except Exception:
                d = 999.0
            if d <= 12:
                # an "idle" verdict must be TIGHT: a wrong idle answer makes the caller
                # click the middle of a PLAYING video, which pauses it
                return False, f"tab icon right edge x={x_right}; {detail}"
            idle_weak.append(f"x={x_right}: {detail}")
        else:
            idle_weak.append(f"x={x_right}: {detail}")
    return None, ("no tab slot matched a reference confidently ("
                  + "; ".join(idle_weak[:4]) + ")")


def capture_page(tag="page", tries=6):
    """Focus the browser and capture, verifying the shot really is the course page.

    Focus stealing is the recurring failure: DSH takes the foreground between the
    focus call and the grab, the screenshot silently shows DSH, and downstream
    detection reads nonsense -- a real calibration run "measured" the catalog from
    DSH's own sidebar. So validate strictly and retry; if it never succeeds, raise
    instead of returning a bad frame (that is what caused cursor=4.3 to be saved
    on a page that was actually 3.1).
    """
    import keyboard_type

    last_words, last_img, last_whys = [], "", []
    scrolled_back = False
    for i in range(tries):
        focus_course_tab()
        time.sleep(0.5)
        last_words, last_img = read_screen(tag)
        joined = "".join(w["text"] for w in last_words)
        good = any(m in joined for m in PAGE_MARKERS)
        bad = any(m in joined for m in DSH_MARKERS)
        if good and bad:
            # Both windows are on screen (measured: the harness filled the screen with a
            # small browser window floating on top). The frame is still usable -- but only
            # the words INSIDE the browser's own rectangle belong to the page, so keep
            # those and ignore the rest. Without this the run parked six times in a row on
            # "page=True dsh=True" while the quiz was plainly visible.
            r = browser_rect()
            if r:
                inside = [x for x in last_words
                          if r[0] <= x["x"] <= r[0] + r[2] and r[1] <= x["y"] <= r[1] + r[3]]
                j2 = "".join(x["text"] for x in inside)
                if any(m in j2 for m in PAGE_MARKERS) and \
                        not any(m in j2 for m in DSH_MARKERS):
                    log(f"  the harness window is also visible; keeping only the "
                        f"{len(inside)}/{len(last_words)} words inside the browser rect {r}")
                    if page_looks_scrolled(inside) and not scrolled_back:
                        scroll_page_to_top()
                        scrolled_back = True
                        continue
                    return inside, last_img
        if good and not bad:
            if page_looks_scrolled(last_words) and not scrolled_back:
                log("  a 提交 button is visible but no 题型 banner: the page is "
                    "scrolled, sending Home and re-reading")
                scroll_page_to_top()
                scrolled_back = True
                continue
            return last_words, last_img
        last_whys.append(f"try{i + 1}: page={good} dsh={bad} words={len(last_words)}")
        if bad:
            # nudge the shell: Alt+Tab is honoured even when SetForegroundWindow
            # is refused, and it moves away from DSH
            try:
                keyboard_type.chord("alt", "tab")
                time.sleep(0.7)
            except Exception:
                pass
        time.sleep(0.4)
    raise CaptureError("could not capture the course page; " + "; ".join(last_whys))


def infer_cursor_from_chapter(chapter_id=None, words=None):
    """Work out which section we are on, so a run can resume with no saved cursor.

    `control.py start` passed an empty cursor and `resolve_next('')` failed, which
    produced a help request ("could not find a section after ''") on a perfectly
    healthy page. Match the live chapterId against the learned id table, then fall
    back to matching the page title against the catalog rows.
    """
    if chapter_id is None:
        focus_browser()
        time.sleep(0.4)
        chapter_id = read_chapter_id()
    ids = load_chapter_ids()
    if chapter_id:
        for num, cid in ids.items():
            if cid == chapter_id:
                return num

    if words is None:
        try:
            words, _img = capture_page("infer")
        except CaptureError:
            return None

    title = course_title(words)
    if title:
        t = re.sub(r"\s+", "", title)
        for r in catalog_rows(words):
            rt = re.sub(r"\s+", "", r["title"]).rstrip("0123456789")
            if len(rt) >= 3 and (rt[:4] in t or t[:4] in rt):
                return r["num"]

    # Deliberately NO "first row" fallback: guessing 1.1 would replay the entire
    # course from the start. Returning None makes the caller ask for help instead.
    log("infer_cursor_from_chapter: could not determine the current section "
        f"(chapterId={chapter_id}, title={title!r})")
    return None


def click_catalog_row(row, label, wait=3.0):
    """Click inside a catalog row using its real bounding box.

    The click must land LEFT of the number column (the row's clickable area is the
    whole row, and x0 is the number itself). Aiming right of the numbers hit the
    content pane instead: the click registered 122k px of change but chapterId
    never moved, so nothing had actually navigated.
    """
    x = max(0, int(row.get("x0", row["x"])) - SX(60))
    y = row.get("cy", row["y"])
    return verified_click(x, y, label, wait=wait,
                          region=SR(1400, 300, 520, 520), min_change=60)


def read_chapter_id_stable(timeout=25.0, poll=2.5):
    """Read chapterId, retrying until the page settles.

    A just-clicked section needs several seconds before the address bar is both
    updated and OCR-readable (measured: unreadable for ~8s, readable after). A
    single read made successful navigations look like failures.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        cid = read_chapter_id()
        if cid:
            return cid
        time.sleep(poll)
    return None


def row_title(words, row):
    """The catalog row's title text: words on the same line, right of the number."""
    y = row.get("y", 0)
    right = row.get("x", 0) + row.get("w", 0)
    same = [w for w in words if abs(w["y"] - y) <= 22 and w["x"] >= right]
    same.sort(key=lambda w: w["x"])
    text = "".join(w["text"] for w in same)
    # drop the leading number (it is often still glued to the title)
    return re.sub(r"^\s*\d{1,2}\s*[.．，,、]?\s*\d{0,2}\s*", "", text)


def _text_key(s):
    return "".join(c for c in s if c.isalnum() or "\u4e00" <= c <= "\u9fff")


def page_heading(words):
    """The section heading in the content pane's top-left (measured band)."""
    sw, sh = mouse_click.screen_size()
    band = [w for w in words
            if w["x"] < sw * 0.30 and sh * 0.11 < w["y"] < sh * 0.20]
    band.sort(key=lambda w: (w["y"], w["x"]))
    return "".join(w["text"] for w in band)


def content_is_section(words, row) -> bool:
    """Does the page show this catalog row's section? (heading vs row title)

    Needed because the URL is not always a usable navigation signal: measured at 06:53,
    the platform had ALREADY moved to 11.4 by itself after the post-video refresh (the
    URL had become 11.4's id, which nothing had learned yet), so the runner's click
    changed nothing, `cid != start_cid` was false, and it parked declaring "not
    navigated" while the page plainly showed 11.4.

    The comparison cannot be a substring test: OCR of the large heading scrambles the
    character ORDER (the true heading "利益冲突如何披露和处理" came back as
    "益中突如何披处利露和冫理"), so compare the character SETS instead -- measured, that
    gives 10/12 overlap for the right row and 3/12 for its neighbours.
    """
    t = set(_text_key(row_title(words, row)))
    h = set(_text_key(page_heading(words)))
    if len(t) < 4 or len(h) < 4:
        return False
    # A parenthesised number is what tells otherwise identical titles apart
    # ("…实践伦理原则（1）/（2）/（3）"), and OCR keeps it, so it must agree. Without this
    # the run "advanced" 20.2 -> 20.3 -> 20.4 without navigating at all (measured at
    # 04:56: three sections in 30 seconds, all "already on … by content") and then
    # declared the course finished while 20.2-20.4 had never been played.
    def paren(s):
        return re.findall(r"[（(]\s*(\d{1,2})\s*[）)]", s)

    tp, hp = paren(row_title(words, row)), paren(page_heading(words))
    if tp or hp:
        return tp == hp and len(t & h) / len(h) >= 0.6
    return len(t & h) / len(h) >= 0.6


def advance_to(nxt, cursor, attempts=3):
    """Click a catalog row and confirm the section really changed.

    Verification is by chapterId ONLY. Two alternatives were tried and both had to go:

    * a title-change fallback once declared success on DSH's own sidebar text ("D新会话"),
      which marched the run through the catalog without moving the page;
    * a "page heading vs row title" check (added for 11.4, where the platform had already
      advanced so the URL looked unchanged). It is not safe: the heading band does not
      always contain the title (on 20.2-20.5 it read the "观看时不可离开…" notice), and the
      titles of 20.2/20.3/20.4 differ only by a parenthesised digit. Measured at 04:56 it
      "advanced" 20.2 -> 20.3 -> 20.4 in 30 seconds without navigating and the run then
      declared the whole course finished while those three had never been played.

    What makes chapterId trustworthy is the LEARNED id table (chapter_ids.json): a target
    whose id is known is confirmed exactly, and a changed URL is learned. The 11.4 stall is
    covered by seeding that table, not by guessing from pixels.
    """
    want_cid = load_chapter_ids().get(nxt["num"])
    start_cid = read_chapter_id_stable(timeout=12)
    if want_cid and start_cid == want_cid:
        log(f"  already on {nxt['num']} (chapterId={start_cid}); no click needed")
        return True

    for i in range(attempts):
        ok, changed = click_catalog_row(nxt, f"goto_{nxt['num'].replace('.', '_')}")
        focus_browser()
        cid = read_chapter_id_stable(timeout=25)

        if cid and want_cid and cid == want_cid:
            log(f"  confirmed by chapterId={cid}")
            return True
        if cid and start_cid and cid != start_cid:
            log(f"  chapterId changed {start_cid} -> {cid}; navigated")
            remember_chapter_id(nxt["num"], cid)
            return True
        if cid and not want_cid and i == attempts - 1:
            log(f"  chapterId is unreadable/unchanged ({cid}) and {nxt['num']} has no known "
                f"id; NOT claiming success -- seed chapter_ids.json")
        log(f"  attempt {i + 1}/{attempts}: chapterId still {cid} "
            f"(changed={changed}px) -- not navigated")
        time.sleep(1.5)

    log("  navigation could not be confirmed by chapterId")
    return False


# Every fixed coordinate in this file was first measured on a 1920x1200 screen.
# They are expressed in that baseline and scaled to whatever the display is now,
# so changing resolution (or attaching a virtual display) does not misplace
# clicks. Anything measured from the live screen (catalog rows, option discs) is
# already resolution independent.
BASE_W, BASE_H = 1920, 1200
_SCALE = {"x": 1.0, "y": 1.0, "w": 1.0, "h": 1.0, "sx": 1.0, "sy": 1.0}


def refresh_scale():
    """Recompute the baseline -> current-display scale factors."""
    w, h = mouse_click.screen_size()
    _SCALE["x"] = w / BASE_W
    _SCALE["y"] = h / BASE_H
    _SCALE["w"] = w / BASE_W
    _SCALE["h"] = h / BASE_H
    # the page is scaled uniformly by the browser; use one factor for sizes so
    # text boxes do not stretch when the aspect ratio differs
    _SCALE["sx"] = _SCALE["x"]
    _SCALE["sy"] = _SCALE["y"]
    return _SCALE


def S(x, y):
    """Scale a baseline point to the current display."""
    return int(round(x * _SCALE["x"])), int(round(y * _SCALE["y"]))


def SR(x, y, w, h):
    """Scale a baseline (x, y, w, h) capture region."""
    return (int(round(x * _SCALE["x"])), int(round(y * _SCALE["y"])),
            int(round(w * _SCALE["w"])), int(round(h * _SCALE["h"])))


def SV(x, y):
    """Scale a baseline (x, y) distance/offset pair."""
    return int(round(x * _SCALE["sx"])), int(round(y * _SCALE["sy"]))


def SX(v):
    """Scale a baseline x coordinate (single axis)."""
    return int(round(v * _SCALE["x"]))


def SY(v):
    """Scale a baseline y coordinate (single axis)."""
    return int(round(v * _SCALE["y"]))


# ------------------------------------------------------------ video geometry
# The player is MEASURED from the picture, never assumed from a pixel constant.
# The old hardcoded fraction (0.231W, 0.389H) was measured on a different layout
# and aimed ~590 px left / ~490 px above the real button -- straight at the
# lecturer's face, where this player does nothing at all. A play click that lands
# nowhere looks exactly like a click that was swallowed, which cost a whole
# debugging session.
VIDEO_SEARCH = (0.02, 0.03, 0.75, 0.98)   # screen fractions worth scanning
GRID_CELL = 8                             # measurement resolution of the block scan
# last successful measurement, reused only when the content defeats the scan
_VIDEO_RECT = {"size": None, "box": None}


def _dbg(name):
    """A debug artefact path inside this run's own scratch dir.

    Per-process: a shared _dbg/ meant that anything else cleaning it up (an offline
    test, an operator) deleted a file out from under a live run, and the run died
    with FileNotFoundError on its own screenshot.
    """
    os.makedirs(DEBUG_DIR, exist_ok=True)
    return os.path.join(DEBUG_DIR, name)


def purge_debug():
    """Delete the previous run's artefacts. Names are fixed, so this is bounded."""
    import glob
    import shutil
    shutil.rmtree(os.path.join(HERE, "_dbg"), ignore_errors=True)
    for pat in ("_auto_*.png", "_auto_*.json", "_diag_*.png", "_ans_*.png", "_fb_test.png"):
        for p in glob.glob(os.path.join(HERE, pat)):
            try:
                os.remove(p)
            except OSError:
                pass


def _blocks(solid, gw, gh, min_area=64):
    """Connected components of a boolean grid -> [(area, (x0, y0, x1, y1)), ...]."""
    from collections import deque

    seen = [[False] * gw for _ in range(gh)]
    out = []
    for sy in range(gh):
        for sx in range(gw):
            if not solid[sy][sx] or seen[sy][sx]:
                continue
            q = deque([(sx, sy)])
            seen[sy][sx] = True
            x0 = x1 = sx
            y0 = y1 = sy
            area = 0
            while q:
                x, y = q.popleft()
                area += 1
                if x < x0:
                    x0 = x
                elif x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                elif y > y1:
                    y1 = y
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < gw and 0 <= ny < gh and solid[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        q.append((nx, ny))
            if area >= min_area:
                out.append((area, (x0, y0, x1, y1)))
    return out


def _calibrated_rect(size):
    """The player box a previous run measured, for this screen size."""
    cal = screen_calib() or {}
    p = cal.get("player") or {}
    if p and list(cal.get("screen") or []) == list(size):
        try:
            return (int(p["x0"]), int(p["y0"]), int(p["w"]), int(p["h"]))
        except Exception:
            return None
    return None


def video_rect(img_path, min_area_cells=400):
    """Bounding box (x, y, w, h) of the video player, measured from a screenshot.

    The player is the one large block of non-page-background pixels in the left
    pane: the page is white, the video is a picture. Measuring it means the answer
    survives any window size, resolution or browser zoom.

    The centre of this box is where the platform draws its poster play button
    (verified: measured centre (1480,1328) vs the button's real position
    (1477,1332) on the live 5.2 page).

    A fresh measurement is only accepted when it AGREES with the layout reference
    (the calibration a run measured while the player was visible, else this run's
    last good box). Otherwise the reference wins, because the alternatives are worse:
      * a mostly white slide leaves only a graphic inside the slide as a
        "non-background block" (measured on 6.4: the red "?" -- 13% of the player,
        centre white -- so the play-button test passed and four clicks went into
        the picture);
      * a dimmed overlay returns the whole search window (measured: (72,64,2808,1232),
        browser toolbar included);
      * and a bad box must never become the reference itself, or the next good
        measurement is rejected in favour of it (observed: a cascading lock-in).
    """
    from PIL import Image

    im = Image.open(img_path)
    size = im.size
    cal = _calibrated_rect(size)
    mem = _VIDEO_RECT["box"] if _VIDEO_RECT["size"] == size else None
    # the calibration is the stable thing to VALIDATE against (it was measured with
    # the player visible); the memory is the better click target, because the player
    # box is not identical in every section (measured 2544x1424 on 5.2 against
    # 2208x1240 on 6.4) and a 138 px offset can miss a button
    ref = cal or mem
    box = measure_video_rect(im, min_area_cells)

    if box and ref:
        ratio = (box[2] * box[3]) / max(1, ref[2] * ref[3])
        # every corner must be inside the reference (with a small margin): a box that
        # merely contains the player's centre is not the player
        inside = (ref[0] - 80 <= box[0] and box[0] + box[2] <= ref[0] + ref[2] + 80
                  and ref[1] - 80 <= box[1] and box[1] + box[3] <= ref[1] + ref[3] + 80)
        if not (0.5 <= ratio <= 2.0 and inside):
            log(f"  video box candidate {box} ({ratio:.2f}x of the known player, "
                f"inside={inside}) looks like content, not the player -> keeping "
                f"{mem or cal}")
            return mem or cal

    if box:
        _VIDEO_RECT.update(size=size, box=box)
        return box
    return mem or cal


def measure_video_rect(im, min_area_cells=400):
    """The pixel scan behind video_rect(); `im` is a PIL image of the screen."""
    from PIL import Image

    im = im.convert("L")
    W, H = im.size
    gh, gw = H // GRID_CELL, W // GRID_CELL
    if gh < 16 or gw < 16:
        return None
    # page background is pure white / the light pane grey; everything else is the
    # video. BOX downscaling turns "how much of this cell is not background" into
    # a single pixel value, which is what makes the scan cheap.
    fg = im.point(lambda v: 0 if v > 245 else 255)
    fg = fg.crop((0, 0, gw * GRID_CELL, gh * GRID_CELL)).resize((gw, gh), Image.BOX)
    px = fg.load()
    x0f, y0f, x1f, y1f = VIDEO_SEARCH
    solid = [[False] * gw for _ in range(gh)]
    for y in range(gh):
        for x in range(gw):
            if not (int(gw * x0f) <= x < int(gw * x1f) and int(gh * y0f) <= y < int(gh * y1f)):
                continue
            solid[y][x] = px[x, y] > 234       # >= ~92% of the cell is not background

    comps = _blocks(solid, gw, gh, min_area=min_area_cells)
    # A thin frame of page chrome (drop shadows, the header bar) is one connected
    # component with a huge bounding box and almost no fill; a video block is
    # dense. Without this filter the frame got glued to the player and the
    # "centre" landed 240 px above the real button.
    def dense(c):
        area, (x0, y0, x1, y1) = c
        box = (x1 - x0 + 1) * (y1 - y0 + 1)
        return area / box >= 0.35

    comps = [c for c in comps if dense(c)]
    if not comps:
        return None
    comps.sort(key=lambda c: -c[0])
    bx0, by0, bx1, by1 = comps[0][1]
    # a bright frame inside the video can cut the block in two; glue back only the
    # pieces that are MOSTLY inside the chosen box, so neighbouring chrome cannot
    # drag the box away from the player
    for area, (cx0, cy0, cx1, cy1) in comps[1:]:
        ox = max(0, min(bx1, cx1) - max(bx0, cx0) + 1)
        oy = max(0, min(by1, cy1) - max(by0, cy0) + 1)
        own = (cx1 - cx0 + 1) * (cy1 - cy0 + 1)
        if own and (ox * oy) / own >= 0.6:
            bx0, by0 = min(bx0, cx0), min(by0, cy0)
            bx1, by1 = max(bx1, cx1), max(by1, cy1)

    box = (bx0 * GRID_CELL, by0 * GRID_CELL,
           (bx1 - bx0 + 1) * GRID_CELL, (by1 - by0 + 1) * GRID_CELL)
    x, y, w, h = box
    # sanity: a player is a wide block inside the left pane, not a stray strip
    if w < W * 0.15 or h < H * 0.10 or w > W * 0.74:
        return None
    if not (0.9 <= w / max(h, 1) <= 3.2):
        return None
    return box


def poster_play_point(img_path):
    """The centre of the measured player, where the poster's play button is drawn."""
    r = video_rect(img_path)
    if not r:
        return None
    return (r[0] + r[2] // 2, r[1] + r[3] // 2)


def screen_shot(tag):
    """Full screenshot saved inside _dbg/ (the video area is measured from it).

    JPEG on purpose: this is one of three captures per poll cycle, and a full-screen
    PNG is ~2.3 MB -- over a multi-hour run that is needless write churn. The motion
    crop that is actually compared is saved as PNG by video_crop(); the OCR page
    capture keeps PNG as well, where quality matters.
    """
    p = _dbg(f"{tag}.jpg")
    grab.grab(p)
    return p


def video_crop(img_path, rect, tag, div=4):
    """Save a downscaled crop of the player and return its path.

    Motion is judged on the measured player box: sampling the pane blind kept
    landing on page background (motion always zero) or missing the video entirely.
    """
    from PIL import Image

    im = Image.open(img_path)
    if rect:
        x, y, w, h = rect
        im = im.crop((x, y, x + w, y + h))
    else:
        w, h = im.size
        im = im.crop((int(w * 0.04), int(h * 0.15), int(w * 0.48), int(h * 0.72)))
    small = im.resize((max(1, im.width // div), max(1, im.height // div)))
    p = _dbg(f"vid_{tag}.png")
    small.save(p)
    return p


def video_is_moving(prev_path, cur_path, min_change=300) -> bool:
    """Compare two crops of the player area: is the frame still changing?

    A missing file is reported as "no motion", not raised: the artefacts are
    diagnostics, and a run must not die because one of them was cleaned up by
    something else. The state machine re-checks motion on the next cycle anyway.
    """
    try:
        d = mouse_click.pixel_diff(prev_path, cur_path)
    except OSError as e:
        log(f"  WARNING: cannot compare frames ({e}); assuming nothing changed")
        return False
    if not d.get("same_size"):
        return True
    return d.get("changed_pixels", 0) >= min_change


def injectable_at(x, y):
    """UIPI preflight: can a synthetic click reach (x, y) at all?

    At Low integrity (DSH's workspace-write mode) every SendInput click into Edge
    (Medium) is discarded by UIPI. The click reports success and exactly 0 pixels
    change -- indisinguishable from a click that landed on the wrong spot. So ask
    before clicking, and never let this failure hide inside a retry loop again.
    """
    try:
        ok, why = mouse_click.can_inject_into(int(x), int(y))
    except Exception as e:
        return True, f"preflight unavailable ({type(e).__name__}: {e}); trying anyway"
    return bool(ok), why



def centre_play_button(img_path, rect):
    """Is the player showing its big centre play button? -> (bool, detail)

    This is the difference between "start it" and "leave it alone": a click on the
    player body while a video is PLAYING pauses it, and a still slide inside a
    playing video looks exactly like a paused one to a motion test alone.

    The button is a disc drawn OVER whatever is behind it, so the one thing always
    true of it is that it is BRIGHTER than the ring around it. Five measured frames:

        frame                          disc-ring  disc contrast (std)
        5.2 poster (white disc/photo)     +56.2          26.6
        7.1 poster (white disc/pale)      +15.8          17.6
        7.3 poster (GREY disc/black)      +36.2          67.7
        6.4 white slide, centre           -12.2          70.0
        6.4 graphic inside the slide       +5.3           3.3

    So: brighter than the ring by more than 10, with SOME internal contrast (the play
    triangle, > 8). That is deliberately all -- brightness or "whiteness" thresholds
    kept being wrong, because the poster's palette changes from section to section
    (white on a photo, white on pale blue, grey on black) and each new palette broke
    the previous tuning. The two slide cases are excluded by the contrast sign and by
    the graphic being flat.

    A wrong "yes" clicks the player, which pauses a playing video -- bounded, because
    the next cycle sees the real button and presses play again. A wrong "no" stalls a
    never-started video for as long as the stall timer allows (measured twice: 25
    minutes each). Both are bounded, and the thresholds sit in the gap between the two
    measured clusters.
    """
    from PIL import Image, ImageStat

    im = Image.open(img_path).convert("RGB")
    x, y, w, h = rect
    cx, cy = x + w // 2, y + h // 2
    r = int(min(w, h) * 0.03) + 4
    ring = int(r * 2.2)
    grey = im.convert("L")
    disc = grey.crop((cx - r, cy - r, cx + r, cy + r))
    around = grey.crop((cx - ring, cy - ring, cx + ring, cy + ring))
    disc_mean = ImageStat.Stat(disc).mean[0]
    ring_mean = ImageStat.Stat(around).mean[0]
    inner = ImageStat.Stat(disc).stddev[0]
    detail = (f"disc {disc_mean:.0f} vs ring {ring_mean:.0f} "
              f"(+{disc_mean - ring_mean:.0f}), disc contrast={inner:.0f}")
    ok = (disc_mean - ring_mean > 10) and inner > 8
    return ok, detail


def video_is_advancing(rect, wait=12.0):
    """Read the player's clock twice and report whether it MOVES.

    Hovering reveals the control bar on this platform, so a visible timecode alone
    does not mean "paused" -- but a clock that changes proves playback, and a frozen
    clock proves it is paused. That is ground truth, unlike guessing from the poster:
    three separate 25-minute stalls came from guessing (a white slide, a low-contrast
    button, a grey button on black), each with a different palette.

    Returns (advancing | None, detail, last_timecode_dict).
    """
    import mouse_click

    if not rect:
        return None, "no player box to hover", None
    # the bottom strip, not the centre: measured on 7.3, hovering the centre does NOT
    # reveal the control bar while hovering the bottom of the player does
    px, py = rect[0] + rect[2] // 2, rect[1] + rect[3] - 40
    mouse_click.position_target(px, py)
    time.sleep(1.5)
    words, _img = read_screen("clock1")
    tc1 = find_timecode(words, within=rect)
    if not tc1:
        return None, "no timecode even after hovering the player", None
    time.sleep(wait)
    # the bar auto-hides after a few seconds of a still pointer, and SetCursorPos to
    # the SAME point generates no move event, so nudge the pointer to bring it back
    mouse_click.position_target(px + 12, py - 4)
    time.sleep(1.5)
    words2, _img2 = read_screen("clock2")
    tc2 = find_timecode(words2, within=rect)
    if not tc2:
        return None, "the timecode disappeared on the second read", tc1
    moved = tc2["text"] != tc1["text"]
    return moved, f"{tc1['text']!r} -> {tc2['text']!r} after {wait:.0f}s", tc2


def play_click_target(rect=None):
    """Where to start playback: the MIDDLE of the video (the operator's instruction).

    Do NOT aim at the play button. This player toggles playback when the video body is
    clicked, and it draws its big play button exactly at the video's centre -- so the
    centre is both the button and a large forgiving target, and it cannot be confused
    with the control bar's button (which shares a row with the timecode and needed a
    measured offset that kept being wrong).

    Falls back to the middle of the screen when the player box could not be measured.
    """
    if rect:
        return rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    w, h = mouse_click.screen_size()
    return w // 2, h // 2


def ensure_playing(max_tries=4, args=None, words=None):
    """Start playback if it is not running. Returns (ok, reason).

    Two rules, both learned the hard way:
      * a click on the player body while a video is PLAYING pauses it, so a click
        is only sent when the picture has been static for two windows AND the
        player's centre play button (or the control bar's timecode) is visible;
      * a still slide inside a playing video is otherwise indistinguishable from a
        paused video, so "static" alone is never enough to click.
    The click target is the centre of the MEASURED video box. The old hardcoded
    screen fraction aimed at the lecturer's face, and the blind control-bar
    fallback aimed into the video body -- both look identical to a lost click.
    """
    refreshed = False          # the "blocked -> reload once" retry has been spent
    a = screen_shot("play_a")
    time.sleep(2.5)
    b = screen_shot("play_b")
    rect = video_rect(b) or video_rect(a)
    if rect:
        log(f"  video box measured at {rect} (screen {mouse_click.screen_size()})")
    else:
        log("  WARNING: could not measure the video box")
    cb = video_crop(b, rect, "b")
    if video_is_moving(video_crop(a, rect, "a"), cb):
        return True, "already playing (picture is moving)"

    time.sleep(2.5)
    c = screen_shot("play_noop")
    cc = video_crop(c, rect, "noop")
    if video_is_moving(cb, cc):
        return True, "already playing (picture is moving)"
    cb = cc

    if not rect:
        return False, ("cannot measure the video box, so the play button cannot be "
                       "aimed; refusing to click into the video body blindly")

    button, detail = centre_play_button(c, rect)
    tc = find_timecode(words or [], within=rect)

    # The operator's signal comes FIRST: the speaker indicator shows on the course tab
    # exactly while the video plays. It is direct and it is cheap, and putting the pixel
    # heuristics first was actively harmful -- measured: a playing video showing a still
    # frame had a visible timecode, so the run "pressed play" and paused it.
    focus_browser()
    playing, why_tab = audio_playing()
    log(f"  tab audio indicator: {playing} ({why_tab})")
    if playing is True:
        return True, f"confirmed playing by the tab's audio indicator ({why_tab})"

    # A visible timecode is NOT proof of "paused": the control bar stays up while the
    # pointer hovers the player -- and this code leaves the pointer there after its own
    # click. Measured 02:46: the run advanced 6:30 -> 6:35 in 27 seconds because it kept
    # clicking a PLAYING video. So when the timecode is readable and the tab did not say
    # "paused", read the clock twice, 3s apart: an advancing clock is ground truth.
    if tc and playing is not False:
        t1 = tc["text"]
        time.sleep(3.0)
        w2, _i2 = read_screen("clock_quick")
        tc2 = find_timecode(w2, within=rect)
        if tc2 and tc2["text"] != t1:
            log(f"  the clock advanced {t1} -> {tc2['text']}; the video is playing")
            return True, f"confirmed playing by the clock ({t1} -> {tc2['text']})"
        if tc2:
            log(f"  the clock is frozen at {t1}; the video really is paused")

    if playing is False or button or tc:
        # start it by clicking the MIDDLE of the video (operator's instruction); the
        # timecode is only evidence that the bar is up, never a click target
        x = play_click_target(rect)
        how = "middle of the video"
    else:
        # nothing readable anywhere: hover the bottom strip and watch the clock
        advancing, why, seen = video_is_advancing(rect)
        log(f"  clock probe: {why}")
        if advancing is True:
            return True, f"confirmed playing by the control-bar clock ({why})"
        if advancing is False and seen:
            log("  the clock did not move -> the video is paused; pressing play")
            x = play_click_target(rect)
            how = "middle of the video (clock frozen)"
        else:
            log(f"  static and nothing readable ({why}); NOT clicking -- that click "
                f"would pause it if it is playing")
            return True, f"no play button, no clock, no tab signal ({why})"

    log(f"  play affordance: button={button} ({detail}) timecode={tc and tc['text']} "
        f"-> target ({x[0]},{x[1]}) [{how}]")

    clicked = 0
    focus_blocked = 0
    for i in range(max_tries):
        ok, why = injectable_at(*x)
        if not ok:
            log(f"  BLOCKED: {why}")
            log("  -> this process cannot inject input into the browser. Restart the "
                "runner in DSH danger-full-access mode (Medium integrity), or the "
                "clicks are discarded by UIPI and nothing will ever move.")
            return False, f"synthetic clicks are dropped by UIPI ({why})"
        if not focus_browser():
            log("  browser is not foreground; refusing to click play")
            focus_blocked += 1
            time.sleep(1.5)
            continue
        clicked += 1
        log(f"  picture static -> clicking play at {x} [{how}] (attempt {i + 1})")
        mouse_click.click(*x)
        time.sleep(2.5)
        shot = screen_shot(f"play_c{i + 1}")
        cc = video_crop(shot, rect, f"c{i + 1}")
        if video_is_moving(cb, cc):
            log(f"  playback confirmed by motion after a click at {x}")
            return True, f"playback started by a click at {x} [{how}]"
        still_there, detail2 = centre_play_button(shot, rect)
        if not still_there:
            log(f"  the play button is gone ({detail2}) -> playing")
            return True, f"play button gone after a click at {x} [{how}]"
        cb = cc

    if not clicked and focus_blocked:
        # TRANSIENT, not a blocker: another window owns the foreground (measured: the
        # Windows Quick Settings flyout, '快速设置'). Waiting is the right action --
        # escalating to STUCK asked the operator for a decision nobody needed, and
        # pressing Esc to steal focus back would interfere with their machine.
        return True, ("could not bring the browser to the foreground (another window "
                      "owns it); leaving playback alone and retrying later")

    # The operator's rule for exactly this case: "if it is really blocked, refresh the
    # page". Measured 11:58: the run parked at 17.5 with "clicked the play button but the
    # picture never moved" -- the same blocked player that a reload had already fixed
    # once (a video stuck at 6:12). So reload once and give playback one more chance
    # before reporting failure.
    if not refreshed:
        refreshed = True
        log("  the video would not start; refreshing the page and retrying once")
        if refresh_page():
            try:
                _w, img2 = capture_page("play_reload")
                rect2 = video_rect(img2) or rect
                focus_browser()
                tg = play_click_target(rect2)
                log(f"  after the reload: clicking the middle of the video at {tg}")
                mouse_click.click(*tg)
                time.sleep(3.0)
                shot2 = screen_shot("play_reload2")
                if video_is_moving(video_crop(cb, rect, "reload_a"),
                                   video_crop(shot2, rect2, "reload_b")):
                    log("  playback confirmed after the page reload")
                    return True, "playback started after refreshing the page"
                return False, ("the video will not start even after refreshing the page "
                               "(clicked the middle of the video twice)")
            except Exception as e:
                log(f"  reload path failed: {type(e).__name__}: {e}")

    log("  WARNING: picture never started moving")
    return False, ("clicked the play button but the picture never moved (the page "
                   "may have blocked it, or the video is buffering)")


# ---------------------------------------------------------------- main loop
def save_state(st):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)


def run(args):
    """Drive the runner as an explicit state machine (see runner_states.py).

    The previous linear loop had no state for "the answer was wrong", so a wrong
    answer was logged as VERIFIED and the section was abandoned still wrong. Each
    state here is small and names itself in the log, so any stall is attributable.
    """
    import runner_states as rs
    import runner_states_impl as impl

    purge_debug()
    log(f"=== auto_course start (dry_run={args.dry_run}) ===")

    # Input preflight, once, before anything else. At Low integrity (DSH
    # workspace-write) SendInput into Edge is discarded by UIPI: every click then
    # "succeeds" while nothing moves, which is unfalsifiable from the logs and
    # wasted an entire session. Refuse to pretend instead.
    sw, sh = mouse_click.screen_size()
    injectable, why = injectable_at(sw // 2, sh // 2)
    log(f"input preflight: {'ok' if injectable else 'BLOCKED'} - {why}")
    if not injectable:
        log("REFUSING TO RUN: no synthetic click can reach the browser. Start the "
            "runner where DSH grants danger-full-access (Medium integrity).")
        st = {}
        try:
            with open(STATE, encoding="utf-8") as fh:
                st = json.load(fh) or {}
        except Exception:
            st = {}
        st["stopped"] = "input blocked"
        if args.start_after:
            st["cursor"] = args.start_after
        save_state(st)                 # keep the cursor: this is not progress
        return 3

    ctx = rs.Ctx(ac=sys.modules[__name__], cursor=args.start_after or "",
                 poll=args.poll, stuck_after=args.stuck_after,
                 help_timeout=args.help_timeout)
    ctx.stuck_since = time.time()      # never measurable as a stall before step 1

    # Clear a previous stop marker. The supervisor refuses to restart a run whose
    # state says `stopped`, and a stale one would end the next run immediately.
    try:
        with open(STATE, encoding="utf-8") as fh:
            st = json.load(fh) or {}
    except Exception:
        st = {}
    if st.pop("stopped", None) is not None:
        save_state(st)
        log("cleared a stale 'stopped' marker from the state file")

    handlers = {
        rs.State.INIT: impl.st_init,
        rs.State.OBSERVE: impl.st_observe,
        rs.State.PLAY: impl.st_play,
        rs.State.QUIZ: impl.st_quiz,
        rs.State.VERIFY: impl.st_verify,
        rs.State.ADVANCE: impl.st_advance,
        rs.State.STUCK: impl.st_stuck,
    }
    if args.dry_run:
        # genuinely read-only: INIT and OBSERVE only, so a probe of the live page
        # never clicks. The claim in --help used to be false -- ADVANCE and QUIZ
        # still acted, and a "dry run" navigated the browser.
        handlers = {rs.State.INIT: impl.st_init, rs.State.OBSERVE: impl.st_observe}
        log("dry-run: only INIT/OBSERVE will run; no state may act on the page")

    steps = 0
    max_steps = args.max_cycles * 40
    while ctx.state not in (rs.State.DONE, rs.State.ABORT):
        steps += 1
        if steps > max_steps:
            log(f"step budget exhausted ({max_steps})")
            break
        if args.dry_run and steps > 3:
            log("dry-run: stopping after a few read-only states")
            break

        # a state that keeps running without progress is a stall
        if ctx.cursor != ctx.last_cursor:
            ctx.last_cursor, ctx.stuck_since = ctx.cursor, time.time()
        if time.time() - ctx.stuck_since > ctx.stuck_after:
            ctx.stuck_reason = (f"no section advanced for "
                                f"{int(time.time() - ctx.stuck_since)}s "
                                f"(state {ctx.state.value}, cursor {ctx.cursor or '-'})")
            ctx.log(ctx.stuck_reason)
            ctx.state = rs.State.STUCK

        handler = handlers.get(ctx.state)
        if handler is None:
            if args.dry_run:
                log(f"dry-run: state {ctx.state.value} would act on the page; stopping")
                break
            log(f"[{ctx.state.value}] no handler; aborting")
            break
        nxt = handler(ctx)
        if nxt is not None and nxt != ctx.state:
            ctx.go(nxt)

    log(f"=== finished in state {ctx.state.value} after {steps} steps ===")
    log("state history: " + " -> ".join(f"{a}->{b}" for _t, a, b in ctx.history[-14:]))
    if ctx.state == rs.State.DONE:
        # tell the supervisor this is the end of the course, not a crash: it used
        # to relaunch a finished run until max-restarts
        save_state({"cursor": ctx.cursor, "section": ctx.cursor, "stopped": "done"})
        return 0
    if ctx.state == rs.State.ABORT:
        return 9
    return 0


def order_key(num: str):
    """Sort key for section numbers ("3.4" -> (3, 4))."""
    try:
        a, b = str(num).split(".")
        return (int(a), int(b))
    except Exception:
        return (999, 999)


def expected_next(cursor: str):
    """The section number that should follow `cursor`, computed from the cursor.

    The cursor is authoritative; the catalog is only used to locate the row. This
    survives OCR dropping rows (a missing 6.2 previously broke the chain).
    """
    m = re.fullmatch(r"(\d+)\.(\d+)", str(cursor or ""))
    if not m:
        return None
    return f"{int(m.group(1))}.{int(m.group(2)) + 1}"


def ensure_row_in_catalog(num, direction="forward", tries=3):
    """Bring row `num` into view -- by REFRESHING, never by scrolling.

    The wheel is gone on purpose: the operator saw the page being scrolled during runs.
    A wheel aimed at the catalog pane bubbles up to the whole page whenever the catalog
    itself has nothing to scroll, and the old code sent up to 36 notches per search.
    The operator measured a better mechanism: a page reload re-anchors the catalog to
    the current section (on 8.3 the visible rows became 8.2/8.3/8.4/8.5/9.1..9.5), which
    is exactly what a search needs -- so refresh and re-read instead.
    """
    for i in range(max(1, tries)):
        if i:
            refresh_page()
        if not focus_browser():
            return None
        words, _img = read_screen("find")
        rows = catalog_rows(words)
        hit = next((r for r in rows if r["num"] == num), None)
        if hit:
            return hit
    return None


def scroll_catalog_for_next(cursor, tries=3):
    """Find the smallest row numbered after `cursor`, by refreshing the page.

    `expected_next()` can only guess x.(y+1), which does not exist at a CHAPTER
    boundary: after 7.5 the next section is 8.1. Returns
    (row | None, largest row number seen) -- the second value is what tells "the read is
    incomplete" apart from anything else, since the end of the course is the hardcoded
    LAST_SECTION.
    """
    best_max = ""
    for i in range(max(1, tries)):
        if i:
            refresh_page()
        if not focus_browser():
            return None, best_max
        words, _img = read_screen("findnext")
        rows = catalog_rows(words)
        for r in rows:
            if r.get("num") and order_key(r["num"]) > order_key(best_max or "0.0"):
                best_max = r["num"]
        after = [r for r in rows
                 if order_key(r["num"]) > order_key(cursor)
                 and order_key(r["num"]) <= order_key(LAST_SECTION)]
        after.sort(key=lambda r: order_key(r["num"]))
        if after:
            return after[0], best_max
    return None, best_max


def at_catalog_end(cursor, max_seen) -> bool:
    """True only at the hardcoded last section.

    Kept as the single place that answers "is the course over": the catalog itself
    cannot answer it (see LAST_SECTION), so `max_seen` is only logged, never trusted.
    """
    return is_last_section(cursor)


def is_last_section(cursor) -> bool:
    """True when `cursor` is the course's final section (operator-supplied constant)."""
    return bool(cursor) and order_key(cursor) >= order_key(LAST_SECTION)


def resolve_next(st, cursor):
    """Pick the row to advance to, scrolling the catalog if necessary.

    Returns (row | None, max_row_number_seen). The second value lets the caller tell
    "nothing after the cursor exists -- the course is finished" apart from "the
    catalog could not be read -- ask for help". Prefers a visible row later than the
    cursor, then searches for the number the cursor implies, then by order. Never
    returns an earlier section.
    """
    rows = st.get("catalog") or []
    last = cursor or ""
    ordered = sorted((r for r in rows if r.get("num")), key=lambda r: order_key(r["num"]))
    max_seen = ordered[-1]["num"] if ordered else ""
    # never advance past the operator-supplied last section
    after = [r for r in ordered
             if order_key(r["num"]) > order_key(last) and order_key(r["num"]) <= order_key(LAST_SECTION)] \
        if last else [r for r in ordered if order_key(r["num"]) <= order_key(LAST_SECTION)]
    if after:
        return after[0], max_seen
    want = expected_next(last)
    if want:
        log(f"  {want} not in the visible catalog ({len(rows)} rows); scrolling to find it")
        hit = ensure_row_in_catalog(want)
        if hit:
            return hit, max_seen
        log(f"  {want} not found even after scrolling")
    # a chapter boundary: the next section is not last+1, so look for any later row
    hit, scrolled_max = scroll_catalog_for_next(last)
    if hit:
        log(f"  next section by order is {hit['num']}")
        return hit, max(max_seen, scrolled_max, key=order_key)
    log(f"  no row numbered after {last} anywhere in the catalog "
        f"(largest seen: {scrolled_max or '-'})")
    return None, max(max_seen, scrolled_max, key=order_key) if (max_seen or scrolled_max) else ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--poll", type=float, default=15.0,
                    help="seconds between page reads while the video plays")
    ap.add_argument("--help-timeout", type=float, default=1800.0,
                    help="how long to wait for an agent/human decision before aborting")
    ap.add_argument("--stuck-after", type=float, default=1500.0,
                    help="seconds without advancing a section before asking for help")
    ap.add_argument("--max-cycles", type=int, default=60)
    ap.add_argument("--start-after", default="",
                    help="section number already finished, e.g. 2.3 (resume cursor)")
    a = ap.parse_args()
    try:
        raise SystemExit(run(a))
    except KeyboardInterrupt:
        log("interrupted by user")
        raise SystemExit(130)
