"""Answer the quiz that is on screen right now: click an option, then submit.

  python answer_now.py --letter A
  python answer_now.py --letter A --dry-run

Verifies each click by pixel change and reports whether the modal went away.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grab
import ocr_click
from auto_course import (SR, S, submit_point, detect_quiz, option_texts, quiz_option_rows,
                         quiz_question_text, taskpoint_done, refresh_scale, focus_browser,
                         DEBUG_DIR)

HERE = os.path.dirname(os.path.abspath(__file__))


def shot(tag, region=None):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    p = os.path.join(DEBUG_DIR, f"ans_{tag}.png")
    grab.grab(p, region)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--letter", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", type=float, default=1.6)
    a = ap.parse_args()
    letter = a.letter.strip().upper()

    # focus the browser FIRST, in this same process: DSH steals the foreground
    # constantly, and a screenshot of the wrong window looks like "no quiz".
    if not focus_browser():
        print("ABORT: could not bring the browser to the foreground")
        return 6

    img = os.path.join(HERE, "_ans_now.png")
    d = None
    opts, disc_x, hit, why = [], None, False, ""
    # several attempts: OCR of the dimmed overlay is flaky and the OCR (and the
    # focus) can take a moment to settle after bringing the window forward
    for attempt in range(4):
        grab.grab(img)
        d = ocr_click.run_ocr(img, os.path.join(HERE, "_ans_now.json"))
        opts, disc_x = quiz_option_rows(d["words"], img)
        hit, why = detect_quiz(d["words"])
        if hit and len(opts) >= 2:
            break
        time.sleep(1.2)

    print(f"attempts done; quiz={hit} ({why}) rows={[(o['letter'], o['cy']) for o in opts]}")
    if not hit or len(opts) < 2:
        # the disc detector is independent of OCR letters, so report what it sees
        import option_discs
        discs, real_x = option_discs.find_option_discs(img)
        print(f"disc detector: disc_x={real_x} rows={discs}")
        print("no quiz detected; nothing to do")
        return 2
    row = next((o for o in opts if o["letter"] == letter), None)
    if not row:
        print(f"option {letter} not on screen")
        return 3

    if a.dry_run:
        print(f"dry-run: would click {letter} at ({row['x']},{row['cy']})")
        return 0

    # --- click the option disc ---
    refresh_scale()
    region = SR(100, 380, 1250, 660)
    before = shot("opt_b", region)
    import mouse_click
    mouse_click.click(row["x"], row["cy"])
    time.sleep(1.2)
    after = shot("opt_a", region)
    diff = mouse_click.pixel_diff(before, after)
    print(f"select {letter}: changed={diff.get('changed_pixels')}px bbox={diff.get('diff_bbox')}")
    if not diff.get("changed_pixels"):
        print("ABORT: selecting the option produced no visible change")
        return 4

    # --- submit (operator-measured fraction; OCR/colour detection both failed) ---
    sx, sy = submit_point()
    sub = None
    before = shot("sub_b", region)
    mouse_click.click(sx, sy)
    time.sleep(2.2)
    after = shot("sub_a", region)
    diff = mouse_click.pixel_diff(before, after)
    print(f"submit: changed={diff.get('changed_pixels')}px bbox={diff.get('diff_bbox')}")

    # --- verify ---
    time.sleep(0.8)
    check = os.path.join(HERE, "_ans_check.png")
    grab.grab(check)
    d2 = ocr_click.run_ocr(check, os.path.join(HERE, "_ans_check.json"))
    still = detect_quiz(d2["words"])
    print(f"quiz still showing: {still[0]} ({still[1]})")
    print(f"task point done   : {taskpoint_done(d2['words'])}")
    return 0 if not still[0] else 5


if __name__ == "__main__":
    raise SystemExit(main())
