"""Walk the catalog in order and ask the PLATFORM whether each section is complete.

Why this exists, twice over:

1. The runner's "advanced to X" only proves its navigation check succeeded -- it does not
   prove the section was ever watched. A bug in those checks, plus a WRONG entry in
   chapter_ids.json for 18.3, did skip sections, so "the course is done" has to be
   measured, not inferred.
2. chapter_ids.json was maintained from OCR'd URLs and is wrong in places (18.3 held
   18.2's id, which made advance_to believe it was already on 18.3 and skip it). This walk
   reads each id from the CLIPBOARD (Ctrl+L, Ctrl+C) after clicking the row, so the
   (section -> id) mapping it writes is byte-exact.

The catalog shows ~20 rows around the current section, so the walk always clicks the next
unvisited row in order -- the window follows it forward.

  python verify_all.py --from 5.2        # resumable; never presses play

Output: verify_all.json  {"18.3": {"done": false, "chapterId": "1234567895", ...}, ...}
It also rewrites chapter_ids.json from what it measured (--no-learn to disable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import auto_course as ac

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "verify_all.json")


def key(num: str):
    c, i = num.split(".")
    return int(c), int(i)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="5.2")
    ap.add_argument("--no-learn", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    res = {}
    if os.path.exists(OUT):
        try:
            res = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            res = {}
    # throw away the failed URL-sweep entries (all done=None) so the walk redoes them
    res = {n: v for n, v in res.items() if v.get("done") is not None}
    start = key(a.start)

    ids_learned = 0
    rounds = 0
    refreshes = 0

    # The catalog only shows a window around the CURRENT section, so a walk can only move
    # forward from wherever the page is. Jump to the start of the range first (by URL: the
    # clipboard gave the exact template) and let the walk take over from there.
    table = ac.load_chapter_ids()
    anchor = next((n for n in sorted((n for n in table if key(n) >= start), key=key)), None)
    if anchor:
        url = ac.course_url(anchor)
        if url:
            landed_now = ac.parse_chapter_id(ac.read_url_from_clipboard() or "") or ""
            if landed_now != str(table.get(anchor)):
                import keyboard_type
                print(f"jumping to {anchor} (chapterId={table[anchor]}) before walking",
                      flush=True)
                if ac.focus_browser():
                    keyboard_type.chord("ctrl", "l")
                    time.sleep(0.4)
                    keyboard_type.type_text(url, per_char_delay=0.004)
                    time.sleep(0.3)
                    keyboard_type.key("enter")
                    time.sleep(5.0)
                    print(f"  now at chapterId="
                          f"{ac.parse_chapter_id(ac.read_url_from_clipboard() or '')}",
                          flush=True)

    while rounds < 60:
        rounds += 1
        try:
            words, _img = ac.capture_page(f"walk{rounds}")
        except ac.CaptureError as e:
            print(f"capture failed: {e}", flush=True)
            time.sleep(3)
            continue
        rows = [r for r in ac.catalog_rows(words)
                if ac.NUM_RE.match(r["num"]) and key(r["num"]) >= start
                and 1 <= int(r["num"].split(".")[1]) <= 5]
        rows.sort(key=lambda r: key(r["num"]))
        nxt = next((r for r in rows if r["num"] not in res), None)
        if not nxt:
            # The catalog window does not follow the page by itself -- only a reload
            # re-anchors it to the current section (the same mechanism the runner uses).
            # Without this the walk dies as soon as the visible window is fully visited.
            if refreshes < 1:
                refreshes += 1
                print(f"round {rounds}: window exhausted at "
                      f"{[r['num'] for r in rows][:4]}...; refreshing to re-anchor "
                      f"({refreshes})", flush=True)
                ac.refresh_page()
                continue
            print(f"round {rounds}: no unvisited row after {refreshes} refreshes "
                  f"({[r['num'] for r in rows][:8]}...); stopping", flush=True)
            break
        num = nxt["num"]
        ok, changed = ac.click_catalog_row(nxt, f"walk_{num.replace('.', '_')}")
        time.sleep(3.0)
        try:
            w2, _i2 = ac.capture_page(f"walk_{num.replace('.', '_')}")
        except ac.CaptureError as e:
            print(f"{num}: capture failed ({e})", flush=True)
            continue
        done = bool(ac.taskpoint_done(w2))
        url = ac.read_url_from_clipboard() or ""
        cid = ac.parse_chapter_id(url) or ""
        res[num] = {"done": done, "chapterId": cid, "clicked": bool(ok),
                    "changed_px": changed, "url_ok": bool(ac._sanitize_url(url))}
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        if cid and not a.no_learn:
            table = ac.load_chapter_ids()
            if table.get(num) != cid:
                table[num] = cid
                with open(ac.CHAPTER_IDS_FILE, "w", encoding="utf-8") as fh:
                    json.dump(table, fh, ensure_ascii=False, indent=1)
                ids_learned += 1
        print(f"{num}: done={done} chapterId={cid or '(unreadable)'} clicked={ok} "
              f"changed={changed}px", flush=True)
        if a.limit and len(res) >= a.limit:
            break

    done_nums = sorted((n for n, v in res.items() if v["done"]), key=key)
    bad = sorted((n for n, v in res.items() if not v["done"]), key=key)
    print(f"\nwalked {len(res)} sections; ids repaired: {ids_learned}", flush=True)
    print(f"INCOMPLETE ({len(bad)}): {bad}", flush=True)
    print(f"COMPLETE   ({len(done_nums)})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
