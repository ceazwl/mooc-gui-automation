"""Auto-calibrate the course page geometry for the CURRENT resolution and zoom.

Measures, from a live screenshot, where things are:
  * the catalog column (from the numbers/titles on the right)
  * the video player box (the largest solid non-background block in the left pane,
    measured by auto_course.video_rect -- the single implementation of that idea)

Writes screen_calib.json. auto_course prefers those numbers and falls back to
screen ratios when the file is missing or the screen size changed.

Deliberately NOT stored: the play button, the quiz region and the submit button.
Each was derived from the player box, then preferred over the live measurement, so
a stale value silently reintroduced a wrong coordinate -- the play button being the
one that cost a debugging session.

  python calibrate.py            # measure and save
  python calibrate.py --show     # also list the sections found
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grab
import mouse_click
import ocr_click
from auto_course import focus_browser

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "screen_calib.json")

NUM = re.compile(r"^\d{1,2}$")


def merge_pairs(ws, max_gap_ratio=0.06):
    """Join adjacent single digits that OCR split ("3" "1" -> "3.1")."""
    out = []
    used = set()
    for w in sorted(ws, key=lambda w: (w["y"], w["x"])):
        if id(w) in used or not NUM.match(w["text"].strip()):
            continue
        partner = None
        for x in ws:
            if id(x) in used or x is w or not NUM.match(x["text"].strip()):
                continue
            if abs(x["y"] - w["y"]) <= max(6, w["h"] * 0.5):
                gap = x["x"] - (w["x"] + w["w"])
                if -2 <= gap <= max_gap_ratio * 400:
                    partner = x
                    break
        if partner:
            used.add(id(partner))
            out.append({"num": f"{w['text'].strip()}.{partner['text'].strip()}",
                        "x": w["x"], "y": min(w["y"], partner["y"]),
                        "x1": partner["x"] + partner["w"],
                        "cy": (min(w["y"], partner["y"]) + max(w["y"] + w["h"], partner["y"] + partner["h"])) // 2})
        else:
            out.append({"num": w["text"].strip(), "x": w["x"], "y": w["y"],
                        "x1": w["x"] + w["w"], "cy": w["y"] + w["h"] // 2})
    return out


def find_catalog(ws, W, H):
    """Catalog = the right-most vertical band holding several section numbers."""
    cands = [w for w in ws if NUM.match(w["text"].strip()) and w["x"] > W * 0.45
             and w["y"] > H * 0.15]
    if len(cands) < 3:
        return None
    merged = merge_pairs(cands)
    merged = [m for m in merged if re.fullmatch(r"\d\.\d", m["num"])]
    if len(merged) < 3:
        # fall back to unmerged single digits (zoom levels split them)
        merged = [{"num": c["text"].strip(), "x": c["x"], "y": c["y"],
                   "x1": c["x"] + c["w"], "cy": c["y"] + c["h"] // 2} for c in cands]
    xs = [m["x"] for m in merged]
    ys = [m["cy"] for m in merged]
    gaps = [b - a for a, b in zip(sorted(ys), sorted(ys)[1:]) if 0 < b - a < H * 0.2]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else int(H * 0.04)
    return {
        "num_x_left": min(xs),
        "num_x_right": max(m["x1"] for m in merged),
        "first_y": min(ys),
        "last_y": max(ys),
        "row_pitch": median_gap,
        "samples": [(m["num"], m["x"], m["cy"]) for m in merged][:10],
    }


def find_player(img_path, W, H):
    """Player box, measured by the single implementation in auto_course.video_rect.

    The old independent scan here (row/column runs against a brightness threshold)
    reported NOT FOUND on the live page -- which is why screen_calib.json ends with
    "player": null, and why the play-button coordinate derived from it was absent
    and a stale hardcoded fraction was used instead.
    """
    import auto_course

    r = auto_course.video_rect(img_path)
    if not r:
        return None
    x, y, w, h = r
    return {"x0": x, "y0": y, "x1": x + w, "y1": y + h, "w": w, "h": h}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    mouse_click.set_dpi_aware()
    W, H = mouse_click.screen_size()
    print(f"screen {W}x{H}")
    print("focus browser:", focus_browser())
    time.sleep(0.8)

    img = os.path.join(HERE, "_calib.png")
    grab.grab(img)
    d = ocr_click.run_ocr(img, os.path.join(HERE, "_calib.json"))
    ws = d["words"]
    print(f"words: {len(ws)}")

    cat = find_catalog(ws, W, H)
    player = find_player(img, W, H)
    print("catalog:", json.dumps(cat, ensure_ascii=False) if cat else "NOT FOUND")
    print("player :", player if player else "NOT FOUND")

    calib = {
        "screen": [W, H],
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "catalog": cat,
        "player": player,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(calib, fh, ensure_ascii=False, indent=1)
    print(f"wrote {OUT}")
    if a.show and cat:
        for num, x, cy in cat["samples"]:
            print(f"   section {num} at x={x} cy={cy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
