"""Detect whether the platform accepted or rejected the last answer.

This is the missing verification. The platform shows a red "回答错误" next to the
submit button when the answer is wrong and keeps the quiz open, but OCR cannot read
it (small red text on a dim overlay -- 397 words were recognised on that screen and
not one of them was the feedback). So the signal is found by COLOUR: a red glyph
cluster in the feedback strip.

  correct -> the modal closes (nothing left on screen)
  wrong   -> a red marker sits left of the submit button, modal still open
  unknown -> modal state unreadable
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RED_MIN = 150          # red channel
RED_DOMINANCE = 55     # how far red must lead green/blue


def red_pixels(img_path, box=None, step=2):
    """Count strongly-red pixels, optionally inside `box` = (x, y, w, h)."""
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    px = im.load()
    W, H = im.size
    x0, y0, x1, y1 = 0, 0, W, H
    if box:
        bx, by, bw, bh = box
        x0, y0 = max(0, bx), max(0, by)
        x1, y1 = min(W, bx + bw), min(H, by + bh)
    n = 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            r, g, b = px[x, y]
            if r >= RED_MIN and r - g >= RED_DOMINANCE and r - b >= RED_DOMINANCE:
                n += 1
    return n


def verdict_for(img_path, quiz_still_open, feedback_box=None, min_red=25):
    """Return 'correct' | 'wrong' | 'unknown' plus a short reason."""
    if not quiz_still_open:
        return "correct", "quiz modal closed"

    red = red_pixels(img_path, feedback_box)
    if red >= min_red:
        return "wrong", f"modal still open with {red} red pixels (回答错误 marker)"
    return "unknown", f"modal still open, only {red} red pixels"


if __name__ == "__main__":
    import grab
    import mouse_click
    from auto_course import focus_browser
    mouse_click.set_dpi_aware()
    focus_browser()
    grab.grab("_fb_test.png")
    print("red pixels, whole screen:", red_pixels("_fb_test.png"))
