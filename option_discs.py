"""Detect the quiz option discs by image analysis instead of OCR.

The options sit in the dimmed overlay as bright white radio circles at a fixed
x, stacked at regular y intervals. Windows OCR misses these letters often
(it read only "B" on one screen), so the discs are located from pixels: a
column slice at the disc x is scanned for runs of bright pixels.

Reported as a list of (cy) row centres, top to bottom.
"""
from __future__ import annotations

from PIL import Image


def bright_runs(values, thresh=170, min_len=6, max_len=40):
    """Yield (start, end) index ranges where values exceed thresh."""
    runs = []
    start = None
    for i, v in enumerate(values):
        if v >= thresh:
            if start is None:
                start = i
        else:
            if start is not None:
                if min_len <= i - start <= max_len:
                    runs.append((start, i - 1))
                start = None
    if start is not None and min_len <= len(values) - start <= max_len:
        runs.append((start, len(values) - 1))
    return runs


def _best_chain(centres, min_n=2, lo=60, hi=100, tol=8):
    """Option discs form a consecutive stack with near-identical spacing.

    A banner line of text can also produce a bright run, so keep only the
    longest run of evenly spaced discs (2+ for 判断题, 4+ for 单选题).
    """
    best = []
    n = len(centres)
    for i in range(n):
        chain = [centres[i]]
        gaps = []
        for j in range(i + 1, n):
            gap = centres[j] - chain[-1]
            if gaps:
                if abs(gap - gaps[-1]) > tol or not (lo <= gap <= hi):
                    break
            elif not (lo <= gap <= hi):
                break
            gaps.append(gap)
            chain.append(centres[j])
        if len(chain) > len(best):
            best = chain
    return best if len(best) >= min_n else []


def find_disc_pair(img_path, min_dia=18, max_dia=46, min_gap=None, max_gap=None,
                   y0=None, y1=None, max_surround=160):
    """Find the two option radio discs as LARGE bright circles.

    Bright-run chains proved too fragile: text glyphs also produce bright runs, and
    on a real quiz screen 6 runs were found in one column (the question text) so
    the chain never matched a two-disc pattern. The discs are instead identified by
    size -- they are the only near-solid bright discs of ~25-40px in the overlay --
    and by appearing as a vertically spaced pair.

    Two more filters were needed after a real failure (5.5): on a page whose task
    point was ALREADY complete, the blue "1 / 2 作业" tab pill and the green
    任务点已完成 check on the white page above the player were a bigger, better
    spaced "pair" than the real options, so every answer was aimed at page chrome
    and the platform reported 回答错误 while nothing had been selected (measured:
    bogus neighbourhood brightness 222 / 169 on white page, real discs 18 / 13
    inside the dimmed overlay).

      * y0 / y1      -- stay inside the quiz overlay's row band (below the 判断题
                        banner, above the submit button);
      * max_surround -- the disc must sit on the DIMMED overlay, not on a bright
                        page background.
    """
    from PIL import Image
    import mouse_click
    mouse_click.set_dpi_aware()
    w, h = mouse_click.screen_size()
    if min_gap is None:
        min_gap = int(h * 0.05)
    if max_gap is None:
        max_gap = int(h * 0.22)

    im = Image.open(img_path).convert("L")
    W, H = im.size
    px = im.load()

    found = []
    for cx in range(int(W * 0.02), int(W * 0.30), 4):
        vals = [min(px[x, y] for x in range(max(0, cx - 1), min(W, cx + 2)))
                for y in range(0, H)]
        for a, b in bright_runs(vals, thresh=150, min_len=min_dia, max_len=max_dia):
            cy = (a + b) // 2
            if y0 is not None and cy < y0:
                continue
            if y1 is not None and cy > y1:
                continue
            if max_surround is not None:
                near = [px[cx, y]
                        for y in list(range(max(0, cy - 45), max(1, cy - 14)))
                        + list(range(min(H, cy + 14), min(H, cy + 45)))]
                if near and sum(near) / len(near) > max_surround:
                    continue
            found.append({"x": cx, "y": cy, "dia": b - a})

    if not found:
        return [], None

    # keep the widest run per approximate y (a disc spans several x columns)
    by_y = {}
    for f in found:
        key = round(f["y"] / 12)
        if key not in by_y or f["dia"] > by_y[key]["dia"]:
            by_y[key] = f
    items = sorted(by_y.values(), key=lambda f: f["y"])

    best = None
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            gap = b["y"] - a["y"]
            if not (min_gap <= gap <= max_gap):
                continue
            if abs(a["dia"] - b["dia"]) > 10:
                continue
            if abs(a["x"] - b["x"]) > 20:
                continue
            score = a["dia"] + b["dia"]
            if best is None or score > best[0]:
                best = (score, [a["y"], b["y"]], (a["x"] + b["x"]) // 2)
    if not best:
        return [], None
    _score, centres, x = best
    return centres, x


def find_option_discs(img_path, disc_x=None, y0=None, y1=None, autoscale=True,
                      anchor_y=None, min_dia=18, max_dia=46):
    """Return (row_centres, disc_x) for the option radio circles, top to bottom.

    `anchor_y` is the 判断题/单选题 banner line: the options always sit BELOW it, so
    it bounds the search. Passing it to the pair detector is essential -- the
    detector used to ignore it and picked page badges above the banner instead.

    Tries size-based pair detection first (robust), then falls back to the older
    bright-run chain probe.
    """
    import mouse_click
    mouse_click.set_dpi_aware()
    w, h = mouse_click.screen_size()

    if anchor_y is not None:
        y0 = max(0, int(anchor_y))
        y1 = min(h, int(anchor_y + h * 0.45))
    else:
        y0 = max(0 if y0 is None else y0, int(h * 0.10))
        y1 = min(h if y1 is None else y1, int(h * 0.98))

    centres, x = find_disc_pair(img_path, min_dia=min_dia, max_dia=max_dia,
                                y0=y0, y1=y1)
    if len(centres) >= 2:
        return centres, x

    if autoscale and disc_x is not None:
        disc_x = int(disc_x * (w / 1920.0))

    im = Image.open(img_path).convert("L")
    W, H = im.size
    px = im.load()

    def probe(cx):
        vals = [min(px[x, y] for x in range(max(0, cx - 2), min(W, cx + 3)))
                for y in range(y0, min(y1, H))]
        return [y0 + (a + b) // 2 for a, b in
                (r for r in bright_runs(vals) if 10 <= r[1] - r[0] <= 26)]

    if disc_x is not None:
        candidates = [disc_x]
    else:
        candidates = [int(w * f) for f in (0.045, 0.055, 0.065, 0.075, 0.085,
                                           0.095, 0.105, 0.115, 0.13, 0.15,
                                           0.17, 0.20)]
    best = ([], None)
    for cx in candidates:
        chain = _best_chain(probe(cx))
        if len(chain) > len(best[0]):
            best = (chain, cx)
    return best


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:] or ["quiz1.png", "_auto_s3.png"]:
        rows, x = find_option_discs(path)
        print(f"{path}: disc_x={x} rows={rows}")
