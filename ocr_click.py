"""Full closed loop: screenshot -> OCR boxes -> pick element -> click -> verify.

Usage:
  python ocr_click.py --find "输入" [--index 0] [--grab out.png] [--ocr-json ocr.json]
                      [--dry-run] [--verify] [--keys "text to type"]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import grab
import mouse_click
import ocr_find

HERE = os.path.dirname(os.path.abspath(__file__))


def run_ocr(image_path: str, out_json: str, timeout: float = 60.0) -> dict:
    """Run the WinRT OCR script text in a pwsh session (see ocr_win.ps1 header).

    The timeout is essential: if the child exits but a grandchild still holds the
    stdout pipe, subprocess.run() blocks forever waiting for EOF -- that froze a
    long autonomous run with the process sitting at 0% CPU and one thread.
    """
    script = os.path.join(HERE, "ocr_win.ps1")
    ps = (
        f"$ImagePath = '{image_path}'; $OutJson = '{out_json}'; $Quiet = $true; "
        f"Invoke-Expression (Get-Content -LiteralPath '{script}' -Raw -Encoding UTF8)"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"OCR timed out after {timeout}s on {image_path}") from e
    if proc.returncode != 0:
        raise RuntimeError(f"OCR failed ({proc.returncode}): {proc.stderr[-800:]}")
    with open(out_json, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    p = argparse.ArgumentParser(description="OCR an element and click it.")
    p.add_argument("--find", required=True, help="text of the element to click")
    p.add_argument("--index", type=int, default=0, help="which hit to use")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--grab", default=os.path.join(HERE, "loop_before.png"))
    p.add_argument("--after", default=os.path.join(HERE, "loop_after.png"))
    p.add_argument("--ocr-json", default=os.path.join(HERE, "loop_ocr.json"))
    p.add_argument("--max-gap", type=int, default=14)
    p.add_argument("--button", default="left")
    p.add_argument("--clicks", type=int, default=1)
    p.add_argument("--offset-x", type=int, default=0)
    p.add_argument("--offset-y", type=int, default=0)
    p.add_argument("--region", default="", help="prefer matches inside x,y,w,h of the screen")
    p.add_argument("--near", default="", help="prefer matches near x,y on the screen")
    p.add_argument("--fuzzy", type=float, default=0.8, help="text similarity threshold (0-1)")
    p.add_argument("--dry-run", action="store_true", help="locate but do not click")
    p.add_argument("--verify", action="store_true", help="screenshot after the click and diff")
    p.add_argument("--keys", default="", help="type this text after clicking (then it is reported)")
    args = p.parse_args()

    # 1) capture
    mouse_click.set_dpi_aware()
    img = grab.grab(args.grab)
    print(f"[1] capture      : {args.grab} {img.size[0]}x{img.size[1]}")

    # 2) OCR -> boxes
    data = run_ocr(os.path.abspath(args.grab), os.path.abspath(args.ocr_json))
    words = data.get("words", [])
    segments = ocr_find.merge_segments(words, max_gap=args.max_gap)
    print(f"[2] ocr          : lang={data.get('lang')} words={len(words)} segments={len(segments)}")

    # 3) pick the element (contiguous-phrase match over the raw word boxes)
    near = tuple(int(v) for v in args.near.split(",")) if args.near else None
    region = tuple(int(v) for v in args.region.split(",")) if args.region else None
    hits = ocr_find.find_phrase(words, args.find, exact=args.exact, max_gap=args.max_gap,
                                fuzzy=args.fuzzy, near=near, region=region)
    if not hits:
        print(f"[3] target       : NOT FOUND for {args.find!r}")
        loose = [w for w in words if args.find[:1] in w["text"]][:8]
        for w in loose:
            print(f"      near-miss: {w['text']!r} center=({w['x'] + w['w'] // 2},"
                  f"{w['y'] + w['h'] // 2})")
        return 2
    tgt = hits[min(args.index, len(hits) - 1)]
    x, y = tgt.cx + args.offset_x, tgt.cy + args.offset_y
    print(f"[3] target       : {tgt.text!r} box=[{tgt.x},{tgt.y} {tgt.w}x{tgt.h}] "
          f"center=({tgt.cx},{tgt.cy}) -> click=({x},{y})  ({len(hits)} hit(s))")

    if args.dry_run:
        print("[4] click        : skipped (--dry-run)")
        return 0

    # 4) click
    injectable, why = mouse_click.can_inject_into(x, y)
    before_pos = mouse_click.get_cursor_pos()
    landed = mouse_click.click(x, y, button=args.button, clicks=args.clicks)
    print(f"[4] click        : {args.button} x{args.clicks} landed={landed} "
          f"(from {before_pos}, screen={mouse_click.screen_size()})")
    print(f"    uipi         : {'ok' if injectable else 'BLOCKED'} - {why}")

    if args.keys:
        import keyboard_type
        keyboard_type.type_text(args.keys)
        print(f"[5] keys         : typed {args.keys!r}")

    # 5) verify
    if args.verify:
        import time
        time.sleep(0.6)
        grab.grab(args.after)
        d = mouse_click.pixel_diff(args.grab, args.after)
        print(f"[6] verify       : changed={d.get('changed_pixels')} px "
              f"({d.get('changed_ratio')}) bbox={d.get('diff_bbox')}")
        print(f"      before={args.grab}\n      after ={args.after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
