"""Human/model-in-the-loop help channel.

The runner never guesses and never dies silently: when it is unsure or stuck it
writes `auto_help/request.json`, blocks, and waits. A `waiter.py` background job
exits the moment that file appears, and the harness turns that job completion
into a session notification -- so the agent is woken automatically, looks at the
saved screenshot, and writes `auto_help/reply.json`.

Layout of the two files:

  request.json  {"id", "reason", "detail", "screenshot", "ocr_text",
                 "question", "options", "title", "chapter_id", "ts"}
  reply.json    {"id", "action", "x", "y", "letter", "text", "keys", "note"}

Actions the runner understands:
  click      -- click (x, y)
  answer     -- choose option `letter` in the open quiz and submit
  scroll     -- wheel `notches` at (x, y) or the catalog pane
  keys       -- send a key chord such as "esc" / "ctrl+l"
  click_text -- OCR the screen for `text` and click it
  skip       -- ignore whatever blocked us and continue the loop
  abort      -- stop the run cleanly
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HELP_DIR = os.path.join(HERE, "auto_help")
REQ = os.path.join(HELP_DIR, "request.json")
REP = os.path.join(HELP_DIR, "reply.json")
STOP = os.path.join(HELP_DIR, "stop")


def _ensure_dir():
    os.makedirs(HELP_DIR, exist_ok=True)


def _read(path):
    """Tolerant read: PowerShell written JSON often carries a BOM."""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except Exception:
            continue
    return None


def _write(path, obj):
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:      # no BOM
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def request(reason, detail="", screenshot=None, ocr_text="", question="",
            options=None, title=None, chapter_id=None, extra=None):
    """Write a help request and return its id."""
    _ensure_dir()
    if os.path.exists(REP):
        os.remove(REP)
    rid = datetime.now().strftime("%H%M%S")
    payload = {
        "id": rid,
        "reason": reason,
        "detail": detail,
        "screenshot": screenshot,
        "ocr_text": ocr_text[:1500],
        "question": question,
        "options": options or [],
        "title": title,
        "chapter_id": chapter_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        payload.update(extra)
    _write(REQ, payload)
    return rid


def read_reply(want_id=None):
    rep = _read(REP)
    if not rep:
        return None
    if want_id and rep.get("id") not in (None, want_id):
        return None
    return rep


def wait_for_reply(want_id, timeout=1800, poll=3.0, on_wait=None):
    """Block until a reply arrives. Returns the reply dict, or None on timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rep = read_reply(want_id)
        if rep:
            return rep
        if os.path.exists(STOP):
            return {"action": "abort", "note": "stop flag present"}
        if on_wait:
            on_wait(int(time.time() - t0))
        time.sleep(poll)
    return None


def clear():
    for p in (REQ, REP):
        if os.path.exists(p):
            os.remove(p)


def pending():
    return _read(REQ)
